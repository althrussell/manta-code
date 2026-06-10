from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from manta_code.middleware import delegation as D


# --- plan_intent: positives -------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "create a plan for a tic tac toe game",
        "make a plan",
        "give me a plan for the migration",
        "I need a plan to ship auth",
        "draft a plan for the refactor",
        "plan out the data pipeline",
        "plan this out for me",
        "design a REST API for orders",
        "architect the ingestion layer",
        "come up with a plan to reduce cost",
    ],
)
def test_plan_intent_positive(text):
    assert D.plan_intent(text) is True


# --- plan_intent: negatives -------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "fix the failing test",
        "read the file and summarize it",
        "implement the plan from the file",
        "implement the plan for auth",
        "execute the approved plan",
        "follow the plan we discussed",
        "what does this function do?",
        "add a button to the page",
    ],
)
def test_plan_intent_negative(text):
    assert D.plan_intent(text) is False


# --- middleware behavior ----------------------------------------------------
def _task_tool(desc="Available agents: planning, swe, review"):
    return SimpleNamespace(name="task", description=desc)


def _request(messages, tools):
    return SimpleNamespace(messages=messages, tools=tools)


def _boom_handler(_request):
    raise AssertionError("handler should not be called when delegating")


def test_delegates_plan_request_to_planning():
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="create a plan for tic tac toe")], [_task_tool()])
    out = mw.wrap_model_call(req, _boom_handler)
    assert isinstance(out, AIMessage)
    assert len(out.tool_calls) == 1
    call = out.tool_calls[0]
    assert call["name"] == "task"
    assert call["args"]["subagent_type"] == "planning"
    assert "tic tac toe" in call["args"]["description"]


def test_custom_target():
    mw = D.PlanDelegationMiddleware(target="architect")
    req = _request(
        [HumanMessage(content="design a payments service")],
        [_task_tool(desc="agents: architect, swe")],
    )
    out = mw.wrap_model_call(req, _boom_handler)
    assert out.tool_calls[0]["args"]["subagent_type"] == "architect"


def test_passthrough_when_not_plan_intent():
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="fix the bug in parser.py")], [_task_tool()])
    sentinel = object()
    assert mw.wrap_model_call(req, lambda r: sentinel) is sentinel


def test_passthrough_when_last_message_not_human():
    # After the task tool runs, the last message is a ToolMessage -> never re-fire.
    mw = D.PlanDelegationMiddleware()
    req = _request(
        [
            HumanMessage(content="create a plan for tic tac toe"),
            AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "x", "type": "tool_call"}]),
            ToolMessage(content="here is the plan", tool_call_id="x"),
        ],
        [_task_tool()],
    )
    sentinel = object()
    assert mw.wrap_model_call(req, lambda r: sentinel) is sentinel


def test_passthrough_when_no_task_tool():
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="create a plan")], [])
    sentinel = object()
    assert mw.wrap_model_call(req, lambda r: sentinel) is sentinel


def test_passthrough_when_target_not_available():
    mw = D.PlanDelegationMiddleware(target="planning")
    req = _request(
        [HumanMessage(content="create a plan")],
        [_task_tool(desc="Available agents: swe, review")],  # no planning
    )
    sentinel = object()
    assert mw.wrap_model_call(req, lambda r: sentinel) is sentinel


def test_fires_when_task_description_absent():
    # Unknown availability -> proceed (the task tool errors gracefully if needed).
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="create a plan")], [_task_tool(desc="")])
    out = mw.wrap_model_call(req, _boom_handler)
    assert isinstance(out, AIMessage)


def test_async_delegates():
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="make a plan for X")], [_task_tool()])

    async def boom(_r):
        raise AssertionError("handler should not be called")

    out = asyncio.run(mw.awrap_model_call(req, boom))
    assert isinstance(out, AIMessage)
    assert out.tool_calls[0]["args"]["subagent_type"] == "planning"


def test_async_passthrough():
    mw = D.PlanDelegationMiddleware()
    req = _request([HumanMessage(content="fix the bug")], [_task_tool()])
    sentinel = object()

    async def handler(_r):
        return sentinel

    assert asyncio.run(mw.awrap_model_call(req, handler)) is sentinel


# --- factory + env toggle ---------------------------------------------------
def test_factory_enabled_by_default(monkeypatch):
    monkeypatch.delenv(D._ENV_TOGGLE, raising=False)
    assert isinstance(D.plan_delegation_middleware(), D.PlanDelegationMiddleware)


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_factory_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv(D._ENV_TOGGLE, value)
    assert D.plan_delegation_middleware() is None
