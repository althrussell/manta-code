from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from manta_code.middleware import address as A


# --- parse_address ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "agent", "task", "background"),
    [
        ("@swe land this refactor", "swe", "land this refactor", False),
        ("@swe land this refactor &", "swe", "land this refactor", True),
        ("  @review: look at the diff", "review", "look at the diff", False),
        ("@chief, status of everything &", "chief", "status of everything", True),
        ("@data-checker run the audit", "data-checker", "run the audit", False),
        ("@swe fix a & b in the parser", "swe", "fix a & b in the parser", False),
    ],
)
def test_parse_address_positive(text, agent, task, background):
    assert A.parse_address(text) == (agent, task, background)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "fix the tests",
        "email me @ noon",
        "@swe",          # no task text -> let the model ask
        "@swe   &",      # background marker but nothing to run
        "mention @swe mid-sentence",
    ],
)
def test_parse_address_negative(text):
    assert A.parse_address(text) is None


# --- middleware -------------------------------------------------------------------


class _TaskTool:
    name = "task"
    description = "Delegate to: planning, swe, review, chief"


@dataclass
class _Request:
    messages: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=lambda: [_TaskTool()])


def _mw(submitted: dict | None = None) -> A.AgentAddressMiddleware:
    def submit(agent: str, prompt: str) -> str:
        if submitted is not None:
            submitted.update(agent=agent, prompt=prompt)
        return f"Submitted background task abc12345 to @{agent}."

    return A.AgentAddressMiddleware(
        known_agents=lambda: {"planning", "swe", "review", "chief"},
        submit=submit,
    )


def test_inline_address_synthesizes_task_call():
    mw = _mw()
    req = _Request(messages=[HumanMessage(content="@swe land this refactor")])
    result = mw.wrap_model_call(req, lambda r: "MODEL_CALLED")
    assert isinstance(result, AIMessage)
    (call,) = result.tool_calls
    assert call["name"] == "task"
    assert call["args"]["subagent_type"] == "swe"
    assert call["args"]["description"] == "land this refactor"


def test_background_address_submits_and_short_circuits():
    submitted: dict = {}
    mw = _mw(submitted)
    req = _Request(messages=[HumanMessage(content="@review audit the auth module &")])
    result = mw.wrap_model_call(req, lambda r: "MODEL_CALLED")
    assert isinstance(result, AIMessage)
    assert not result.tool_calls  # receipt only: the turn ends immediately
    assert "abc12345" in result.content
    assert submitted == {"agent": "review", "prompt": "audit the auth module"}


def test_unknown_agent_falls_through_to_model():
    mw = _mw()
    req = _Request(messages=[HumanMessage(content="@alice can you check this")])
    assert mw.wrap_model_call(req, lambda r: "MODEL_CALLED") == "MODEL_CALLED"


def test_does_not_refire_after_tool_message():
    mw = _mw()
    req = _Request(
        messages=[
            HumanMessage(content="@swe land this refactor"),
            AIMessage(content="", tool_calls=[]),
            ToolMessage(content="done", tool_call_id="x"),
        ]
    )
    assert mw.wrap_model_call(req, lambda r: "MODEL_CALLED") == "MODEL_CALLED"


def test_inline_without_task_tool_falls_through():
    mw = _mw()
    req = _Request(messages=[HumanMessage(content="@swe do it")], tools=[])
    assert mw.wrap_model_call(req, lambda r: "MODEL_CALLED") == "MODEL_CALLED"


def test_background_without_task_tool_still_submits():
    submitted: dict = {}
    mw = _mw(submitted)
    req = _Request(messages=[HumanMessage(content="@swe do it &")], tools=[])
    result = mw.wrap_model_call(req, lambda r: "MODEL_CALLED")
    assert isinstance(result, AIMessage)
    assert submitted["agent"] == "swe"


def test_errors_fall_back_to_model():
    mw = A.AgentAddressMiddleware(
        known_agents=lambda: (_ for _ in ()).throw(RuntimeError("registry broken")),
        submit=lambda a, p: "",
    )
    req = _Request(messages=[HumanMessage(content="@swe do it")])
    assert mw.wrap_model_call(req, lambda r: "MODEL_CALLED") == "MODEL_CALLED"


def test_factory_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MANTA_AGENT_ADDRESSING", "0")
    assert A.agent_address_middleware() is None
    monkeypatch.setenv("MANTA_AGENT_ADDRESSING", "1")
    assert A.agent_address_middleware() is not None
