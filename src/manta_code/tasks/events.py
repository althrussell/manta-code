"""Event-log middleware: the observability feed behind ``manta status``.

Appends a lightweight event per tool call to the task store, attributed to the
agent it wraps and (when running inside a background task) the task id from
``MANTA_TASK_ID``. Four kinds:

- ``tool`` — an ordinary tool call ran.
- ``approved`` — an approval-gated tool ran in an *interactive* session:
  deepagents' ``interrupt_on`` HITL pauses before the call, so the call
  executing means the human approved it. This is the approval audit record
  ADR 0009's fleet story needs later.
- ``auto_approved`` — an approval-gated tool ran **unattended** (headless /
  background-task runs set the server's auto-approve flag, and no human was
  in the loop). Recorded distinctly so the audit trail never claims a human
  approval that didn't happen.
- ``denied`` — the tool-policy middleware blocked the call (recognizable by
  the error ``ToolMessage`` it returns).

Fully guarded: event recording can never break a tool call.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from .store import EventRecord, record_event

#: Marker the tool-policy middleware puts in its denial ToolMessages.
_POLICY_BLOCK_MARKER = "Blocked by Manta tool policy"

#: Server env flag set by upstream when HITL is bypassed (headless runs).
_AUTO_APPROVE_ENV = "DEEPAGENTS_CODE_SERVER_AUTO_APPROVE"


def _current_task_id() -> str | None:
    return os.environ.get("MANTA_TASK_ID") or None


def _hitl_active() -> bool:
    """Whether a human is actually in the approval loop for this run.

    Upstream's headless path sets the server's auto-approve flag; background
    tasks always run that way. When auto-approve is on (or we're inside a
    background task), an approval-gated tool executing proves nothing about a
    human decision.
    """
    if os.environ.get("MANTA_TASK_ID"):
        return False
    return os.environ.get(_AUTO_APPROVE_ENV, "").strip().lower() != "true"


def _is_policy_denial(result: Any) -> bool:
    if getattr(result, "status", None) != "error":
        return False
    content = getattr(result, "content", "")
    return isinstance(content, str) and _POLICY_BLOCK_MARKER in content


class EventLogMiddleware(AgentMiddleware):
    """Record one event per tool call for the wrapped agent."""

    def __init__(self, *, agent: str, approval: set[str] | None = None) -> None:
        super().__init__()
        self._agent = agent
        self._approval = approval or set()

    @property
    def name(self) -> str:
        return f"Manta.Events.{self._agent}"

    def _tool_call(self, request: Any) -> dict[str, Any]:
        call = getattr(request, "tool_call", None)
        return call if isinstance(call, dict) else {}

    def _record(self, request: Any, result: Any) -> None:
        try:
            call = self._tool_call(request)
            tool = call.get("name", "") or "?"
            args = call.get("args") or {}
            path = args.get("file_path") if isinstance(args, dict) else None
            detail = f"{tool}({path})" if isinstance(path, str) and path else tool
            if _is_policy_denial(result):
                kind = "denied"
            elif tool in self._approval:
                kind = "approved" if _hitl_active() else "auto_approved"
            else:
                kind = "tool"
            record_event(
                EventRecord(
                    agent=self._agent,
                    kind=kind,
                    detail=detail,
                    task_id=_current_task_id(),
                )
            )
        except Exception:  # noqa: BLE001 - observability must never break a call
            pass

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        result = handler(request)
        self._record(request, result)
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        result = await handler(request)
        self._record(request, result)
        return result


def agent_event_middleware(defn: Any) -> AgentMiddleware | None:
    """Event-log middleware for an :class:`AgentDef`; ``None`` on failure."""
    try:
        return EventLogMiddleware(
            agent=getattr(defn, "name", "agent"),
            approval=set(getattr(defn, "approval", None) or ()),
        )
    except Exception:  # noqa: BLE001
        return None


def orchestrator_event_middleware() -> AgentMiddleware | None:
    """Event-log middleware for the base orchestrator; ``None`` on failure."""
    try:
        return EventLogMiddleware(agent="orchestrator")
    except Exception:  # noqa: BLE001
        return None
