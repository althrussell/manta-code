"""Composed model-control regressions using harmless stateful models."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import Field

from manta_code.agents.registry import AgentDef
from manta_code.runtime import RuntimeBuildError, RuntimeDispatchMiddleware, model_for_agent


class RecordingModel(FakeMessagesListChatModel):
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    seen: list[dict] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs) if kwargs else self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append({"reasoning_effort": kwargs.get("reasoning_effort", self.reasoning_effort),
                          "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                          "metadata": dict(self.metadata or {})})
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)



def model(*, responses=None):
    return RecordingModel(responses=responses or [AIMessage(content="done")],
                          profile={"reasoning_effort_levels": ["low", "high"], "max_output_tokens": 1000})


@dataclass
class Request:
    model: Any
    tools: list = field(default_factory=list)

    def override(self, **kwargs):
        return replace(self, **kwargs)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_dispatch_reuses_stateful_configured_model_between_turns(asynchronous):
    @tool
    def constant() -> str:
        """Return a harmless fixture value."""
        return "constant"
    original = model(responses=[
        AIMessage(content="", tool_calls=[{"name": "constant", "args": {}, "id": "call-one"}]),
        AIMessage(content="FINISHED"),
    ])
    dispatch = RuntimeDispatchMiddleware(agent="fixture")
    dispatch.definition = AgentDef(name="fixture", reasoning_effort="high", max_output_tokens=128, memory=False)
    graph = create_agent(original, [constant], middleware=[dispatch])
    dispatch.bind(graph)
    invocation = {"messages": [HumanMessage(content="Use constant once") ]}
    result = asyncio.run(graph.ainvoke(invocation)) if asynchronous else graph.invoke(invocation)
    assert result["messages"][-1].content == "FINISHED"
    assert len(original.seen) == 2
    assert all(call["reasoning_effort"] == "high" and call["max_tokens"] == 128 for call in original.seen)
    assert original.reasoning_effort is None
    assert original.max_tokens is None
    assert "manta_agent" not in (original.metadata or {})


def test_agent_controls_do_not_mutate_a_peer_using_same_model():
    original = model()
    low = model_for_agent(original, "low", definition=AgentDef(name="low", reasoning_effort="low", max_output_tokens=10))
    high = model_for_agent(original, "high", definition=AgentDef(name="high", reasoning_effort="high", max_output_tokens=100))
    low.invoke("low")
    high.invoke("high")
    assert [call["max_tokens"] for call in original.seen] == [10, 100]
    assert [call["metadata"]["manta_agent"] for call in original.seen] == ["low", "high"]
    assert original.max_tokens is None
    assert "manta_agent" not in (original.metadata or {})


def test_explicit_agent_controls_override_outer_model_binding_kwargs():
    original = model()
    bound = original.bind(reasoning_effort="low", max_tokens=10)
    configured = model_for_agent(bound, "fixture", definition=AgentDef(name="fixture", reasoning_effort="high", max_output_tokens=128))
    configured.invoke("bound fixture")
    assert original.seen[-1]["reasoning_effort"] == "high"
    assert original.seen[-1]["max_tokens"] == 128
    assert bound.kwargs == {"reasoning_effort": "low", "max_tokens": 10}
    assert original.reasoning_effort is None


def test_dynamic_model_replacement_revalidates_controls_and_keeps_inherited_model():
    dispatch = RuntimeDispatchMiddleware(agent="fixture")
    dispatch.definition = AgentDef(name="fixture", reasoning_effort="high", max_output_tokens=128)
    first = model()
    prepared = dispatch._prepare_model(Request(first))
    assert prepared.model.reasoning_effort == "high"
    assert prepared.model.max_tokens == 128
    replacement = model()
    replacement.profile = {"reasoning_effort_levels": ["low"], "max_output_tokens": 1000}
    with pytest.raises(RuntimeBuildError, match="does not support"):
        dispatch._prepare_model(Request(replacement))
    # Switching back uses the same configured stateful instance and peer is clean.
    assert dispatch._prepare_model(Request(first)).model is prepared.model
    assert first.reasoning_effort is None


def test_same_model_profile_changes_revalidate_capability_limits():
    dispatch = RuntimeDispatchMiddleware(agent="fixture")
    dispatch.definition = AgentDef(name="fixture", reasoning_effort="high", max_output_tokens=128)
    original = model()
    dispatch._prepare_model(Request(original))
    original.profile = {"reasoning_effort_levels": ["low"], "max_output_tokens": 1000}
    with pytest.raises(RuntimeBuildError, match="does not support"):
        dispatch._prepare_model(Request(original))


def test_same_inherited_model_option_changes_invalidate_configured_copy():
    dispatch = RuntimeDispatchMiddleware(agent="fixture")
    dispatch.definition = AgentDef(name="fixture")
    original = model()
    first = dispatch._prepare_model(Request(original)).model
    original.max_tokens = 256
    original.reasoning_effort = "high"
    second = dispatch._prepare_model(Request(original)).model
    assert second.max_tokens == 256
    assert second.reasoning_effort == "high"
    assert second is not first


def test_control_cache_accepts_opaque_metadata_without_copying_runtime_objects():
    import threading
    lock = threading.Lock()
    original = model()
    original.metadata = {"runtime_lock": lock, "labels": ["original"]}
    dispatch = RuntimeDispatchMiddleware(agent="fixture")
    dispatch.definition = AgentDef(name="fixture")
    first = dispatch._prepare_model(Request(original)).model
    assert first.metadata["runtime_lock"] is lock
    assert dispatch._prepare_model(Request(original)).model is first
    original.metadata["labels"].append("changed")
    assert dispatch._prepare_model(Request(original)).model is not first
