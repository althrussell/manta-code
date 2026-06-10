from __future__ import annotations

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
    assert event.detail == "read_file(/tmp/x.py)"
    assert event.task_id is None


def test_approval_gated_tool_recorded_as_approved():
    # interrupt_on pauses BEFORE the call; the call running means the human
    # approved it — this is the audit record.
    mw = EventLogMiddleware(agent="swe", approval={"execute"})
    req = _ToolRequest({"name": "execute", "args": {}, "id": "1"})
    mw.wrap_tool_call(req, lambda r: ToolMessage(content="ran", tool_call_id="1"))
    (event,) = _events()
    assert event.kind == "approved"


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
