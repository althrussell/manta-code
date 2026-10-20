"""Event-log middleware: the observability feed behind ``manta status``.

Appends a lightweight event per tool call to the task store, attributed to the
agent it wraps and (when running inside a background task) the task id from
``MANTA_TASK_ID``. Outcomes include:

- ``tool`` — an ordinary tool call ran.
- ``approved`` — an active per-call human gate returned an approval. A gate
  declaration or interactive session alone is never evidence of approval.
- ``auto_approved`` — an approval-gated tool ran **unattended** (headless /
  background-task runs set the server's auto-approve flag, and no human was
  in the loop). Recorded distinctly so the audit trail never claims a human
  approval that didn't happen.
- ``denied`` — the tool-policy middleware blocked the call (recognizable by
  the error ``ToolMessage`` it returns).
- ``tool_error``, ``cancelled``, ``interrupted`` — a tool failed or execution
  stopped before completion. Arguments, results and exception text are omitted.

Fully guarded: event recording can never break a tool call.
"""

from __future__ import annotations

import os
import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from .store import EventRecord, record_event

#: Marker the tool-policy middleware puts in its denial ToolMessages.
_POLICY_BLOCK_MARKER = "Blocked by Manta tool policy"

#: Server env flag set by upstream when HITL is bypassed (headless runs).
_AUTO_APPROVE_ENV = "DEEPAGENTS_CODE_SERVER_AUTO_APPROVE"
_CONFIRMED_APPROVAL: ContextVar[frozenset[str]] = ContextVar("manta_confirmed_tool_approval", default=frozenset())
logger = logging.getLogger("manta.tasks.events")


def _finish_inline(task_id: str, result: Any = None, exc: BaseException | None = None) -> None:
    from . import inline
    try:
        inline.finish(task_id, result=result, exc=exc)
    except Exception:
        # Never replace a graph interruption or replay a completed effect just
        # because its reporting write failed. The durable running row remains
        # recoverable through its parent conversation.
        logger.error("Inline task result could not be persisted; inspect its parent conversation")


@contextmanager
def confirmed_tool_approval(request: Any):
    """Process-local proof set only after a real per-call gate has returned."""
    call_id = str(request.tool_call.get("id") or "")
    token = _CONFIRMED_APPROVAL.set(_CONFIRMED_APPROVAL.get() | {call_id})
    try:
        yield
    finally:
        _CONFIRMED_APPROVAL.reset(token)


def _current_task_id() -> str | None:
    from .inline import CURRENT_INLINE_TASK
    return CURRENT_INLINE_TASK.get() or os.environ.get("MANTA_TASK_ID") or None


#: Explicit unattended marker exported by every ``run_headless`` invocation
#: (ADR 0011). Upstream's auto-approve flag alone is not reliable: a headless
#: run with a restricted shell allow-list runs with auto-approve *off* while
#: still auto-approving non-shell HITL requests.
_UNATTENDED_ENV = "MANTA_UNATTENDED"


def unattended_run() -> bool:
    """Whether this run has no human in the approval loop (ADR 0011).

    True inside background tasks, any ``run_headless`` invocation, or when
    upstream's server auto-approve flag is set. Shared by the audit layer and
    the ASK policy tier so they can never disagree.
    """
    if os.environ.get(_UNATTENDED_ENV, "").strip() == "1":
        return True
    if os.environ.get("MANTA_TASK_ID"):
        return True
    return os.environ.get(_AUTO_APPROVE_ENV, "").strip().lower() == "true"


def _hitl_active() -> bool:
    """Whether a human is actually in the approval loop for this run."""
    return not unattended_run()


def _is_policy_denial(result: Any) -> bool:
    if getattr(result, "status", None) != "error":
        return False
    content = getattr(result, "content", "")
    return isinstance(content, str) and _POLICY_BLOCK_MARKER in content


def _call_result(request: Any, result: Any) -> Any:
    """Unwrap dcode's hook-state Command without losing backend artifacts."""
    from langgraph.types import Command
    if isinstance(result, Command) and isinstance(result.update, dict):
        call_id = getattr(request, "tool_call", {}).get("id")
        matches = [message for message in result.update.get("messages", [])
                   if getattr(message, "tool_call_id", None) == call_id]
        return matches[0] if len(matches) == 1 else None
    return result


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

    def _record(self, request: Any, result: Any, *, outcome: str | None = None) -> None:
        try:
            call = self._tool_call(request)
            result = _call_result(request, result)
            tool = call.get("name", "") or "?"
            # Neither arguments/results nor exception messages belong in the
            # activity feed: each may contain credentials or private content.
            detail = tool
            if outcome is not None:
                kind = outcome
            elif _is_policy_denial(result):
                kind = "denied"
            elif getattr(result, "status", None) == "error":
                kind = "tool_error"
            elif str(call.get("id") or "") in _CONFIRMED_APPROVAL.get():
                kind = "approved"
            elif tool in self._approval and unattended_run() and not _managed_human_loop():
                kind = "auto_approved"
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
            self._command_receipt(request, result)
        except Exception:  # noqa: BLE001 - observability must never break a call
            pass

    def _command_receipt(self, request: Any, result: Any) -> None:
        """Use backend exit metadata, never stdout or an agent's pass claim."""
        import shlex
        from pathlib import PurePath
        from . import store
        call = self._tool_call(request)
        task_id = _current_task_id()
        artifact = getattr(result, "artifact", None)
        metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
        if not task_id or call.get("name") != "execute" or metadata.get("_deepagents_code_mcp_server"):
            return
        if not isinstance(artifact, dict) or type(artifact.get("exit_code")) is not int:
            return
        command = (call.get("args") or {}).get("command", "")
        if not isinstance(command, str):
            return
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            args = list(lexer)
        except ValueError:
            return
        if "\n" in command or "`" in command or "$(" in command or any(
            token and all(char in "();<>|&" for char in token) for token in args
        ):
            return  # A shell group's exit code is not the test runner's exit.
        # A small explicit runner set labels observed test commands. Other
        # commands remain ordinary tool events; a printed 'tests passed' is
        # never promoted to a test receipt.
        names = [PurePath(arg).name for arg in args]
        if names[:2] == ["uv", "run"]:
            names = names[2:]
        is_test = bool(names) and (names[0] in {"pytest", "jest", "vitest"}
            or names[:2] in (["npm", "test"], ["pnpm", "test"], ["yarn", "test"], ["cargo", "test"], ["go", "test"])
            or (names[0].startswith("python") and len(names) > 2 and names[1] == "-m" and names[2] in {"pytest", "unittest"}))
        if is_test:
            store.record_test_result(task_id, command, artifact["exit_code"], str(getattr(result, "content", "")))

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        from . import inline
        task_id = inline.begin(request)
        token = inline.CURRENT_INLINE_TASK.set(task_id) if task_id else None
        try:
            result = handler(request)
        except BaseException as exc:
            self._record(request, None, outcome=_exception_outcome(exc))
            if task_id:
                _finish_inline(task_id, exc=exc)
            raise
        finally:
            if token is not None:
                inline.CURRENT_INLINE_TASK.reset(token)
        if task_id:
            _finish_inline(task_id, result)
        self._record(request, result)
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        from . import inline
        task_id = inline.begin(request)
        token = inline.CURRENT_INLINE_TASK.set(task_id) if task_id else None
        try:
            result = await handler(request)
        except BaseException as exc:
            self._record(request, None, outcome=_exception_outcome(exc))
            if task_id:
                _finish_inline(task_id, exc=exc)
            raise
        finally:
            if token is not None:
                inline.CURRENT_INLINE_TASK.reset(token)
        if task_id:
            _finish_inline(task_id, result)
        self._record(request, result)
        return result


def _exception_outcome(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return "cancelled"
    if type(exc).__name__ == "GraphInterrupt":
        return "interrupted"
    return "tool_error"


def _managed_human_loop() -> bool:
    from .approvals import managed_task
    return managed_task()


def agent_event_middleware(defn: Any) -> AgentMiddleware:
    """Event-log middleware for an :class:`AgentDef`.

    Ask-gated tools (ADR 0011) audit through the same approved/auto_approved
    distinction as upstream-HITL approval tools.
    """
    gated = set(getattr(defn, "approval", None) or ()) | set(
        getattr(defn, "tools_ask", None) or ()
    )
    return EventLogMiddleware(agent=getattr(defn, "name", "agent"), approval=gated)


def orchestrator_event_middleware() -> AgentMiddleware:
    """Event-log middleware for the base orchestrator."""
    return EventLogMiddleware(agent="orchestrator")
