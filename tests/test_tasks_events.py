from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from manta_code.tasks import store
from manta_code.tasks.events import EventLogMiddleware, agent_event_middleware


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("MANTA_TASK_ID", raising=False)
    monkeypatch.delenv("MANTA_UNATTENDED", raising=False)
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_AUTO_APPROVE", raising=False)


@dataclass
class _ToolRequest:
    tool_call: dict[str, Any] = field(default_factory=dict)


def _events() -> list[store.EventRecord]:
    return store.recent_events(limit=20)


def test_ordinary_tool_call_recorded():
    mw = EventLogMiddleware(agent="swe")
    req = _ToolRequest({"name": "read_file", "args": {"file_path": "/tmp/x.py"}, "id": "1"})
    result = mw.wrap_tool_call(req, lambda r: ToolMessage(content="ok", tool_call_id="1"))
    assert result.content == "ok"
    (event,) = _events()
    assert event.agent == "swe"
    assert event.kind == "tool"
    assert event.detail == "read_file"  # arguments may contain private content
    assert event.task_id is None


def test_declared_gate_alone_does_not_claim_human_approval():
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    req = _ToolRequest({"name": "execute", "args": {}, "id": "1"})
    mw.wrap_tool_call(req, lambda r: ToolMessage(content="ran", tool_call_id="1"))
    (event,) = _events()
    assert event.kind == "tool"


def test_policy_denial_recorded_as_denied():
    mw = EventLogMiddleware(agent="review")
    req = _ToolRequest({"name": "write_file", "args": {"file_path": "/x"}, "id": "1"})
    denial = ToolMessage(
        content="Blocked by Manta tool policy: 'write_file' is blocked",
        tool_call_id="1",
        status="error",
    )
    result = mw.wrap_tool_call(req, lambda r: denial)
    assert result is denial
    (event,) = _events()
    assert event.kind == "denied"


def test_failed_tool_result_never_claims_approval_or_records_private_content():
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    req = _ToolRequest({"name": "execute", "args": {"command": "private argument"}, "id": "1"})
    result = ToolMessage(content="private failure output", tool_call_id="1", status="error")
    assert mw.wrap_tool_call(req, lambda _: result) is result
    (event,) = _events()
    assert event.kind == "tool_error"
    assert event.detail == "execute"


@pytest.mark.parametrize("exception,outcome", [
    (RuntimeError("private exception"), "tool_error"),
    (KeyboardInterrupt(), "cancelled"),
])
def test_tool_exception_audited_and_reraised(exception, outcome):
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    req = _ToolRequest({"name": "execute", "args": {"command": "private argument"}, "id": "1"})

    def fail(_):
        raise exception

    with pytest.raises(type(exception)):
        mw.wrap_tool_call(req, fail)
    (event,) = _events()
    assert event.kind == outcome
    assert event.detail == "execute"


def test_async_cancellation_audited_and_reraised():
    mw = EventLogMiddleware(agent="swe")
    req = _ToolRequest({"name": "execute", "args": {}, "id": "1"})

    async def cancelled(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mw.awrap_tool_call(req, cancelled))
    (event,) = _events()
    assert event.kind == "cancelled"


def test_checkpoint_interrupt_audited_and_reraised():
    from langgraph.errors import GraphInterrupt

    mw = EventLogMiddleware(agent="swe")
    req = _ToolRequest({"name": "execute", "args": {}, "id": "1"})

    def interrupt(_):
        raise GraphInterrupt()

    with pytest.raises(GraphInterrupt):
        mw.wrap_tool_call(req, interrupt)
    (event,) = _events()
    assert event.kind == "interrupted"


def test_task_id_attributed_from_env(monkeypatch):
    monkeypatch.setenv("MANTA_TASK_ID", "task9999")
    mw = EventLogMiddleware(agent="swe")
    mw.wrap_tool_call(
        _ToolRequest({"name": "ls", "args": {}, "id": "1"}),
        lambda r: ToolMessage(content="ok", tool_call_id="1"),
    )
    (event,) = _events()
    assert event.task_id == "task9999"


def test_recording_failure_never_breaks_the_call(monkeypatch):
    from manta_code.tasks import events as E

    monkeypatch.setattr(E, "record_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    mw = EventLogMiddleware(agent="swe")
    result = mw.wrap_tool_call(
        _ToolRequest({"name": "ls", "args": {}, "id": "1"}),
        lambda r: ToolMessage(content="ok", tool_call_id="1"),
    )
    assert result.content == "ok"


def test_agent_event_middleware_reads_defn():
    from manta_code.agents.defaults import SWE

    mw = agent_event_middleware(SWE)
    assert mw is not None
    assert mw._approval == {"write_file", "edit_file", "execute"}


def test_unattended_approval_recorded_as_auto_approved(monkeypatch):
    # Background tasks / headless runs bypass HITL (auto-approve); the audit
    # trail must not claim a human approved anything (review finding).
    monkeypatch.setenv("MANTA_TASK_ID", "task1234")
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    mw.wrap_tool_call(
        _ToolRequest({"name": "execute", "args": {}, "id": "1"}),
        lambda r: ToolMessage(content="ran", tool_call_id="1"),
    )
    (event,) = [e for e in store.recent_events(limit=5)]
    assert event.kind == "auto_approved"


def test_auto_approve_env_recorded_as_auto_approved(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_AUTO_APPROVE", "true")
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    mw.wrap_tool_call(
        _ToolRequest({"name": "execute", "args": {}, "id": "1"}),
        lambda r: ToolMessage(content="ran", tool_call_id="1"),
    )
    (event,) = [e for e in store.recent_events(limit=5)]
    assert event.kind == "auto_approved"
