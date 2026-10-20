"""Durable identities for actual in-session delegation calls.

Inline children share their parent's checkout and checkpoint. Their board row
does not claim worktree isolation or permit a detached runner to resume them.
"""
from __future__ import annotations

import hashlib
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from . import store

CURRENT_INLINE_TASK: ContextVar[str | None] = ContextVar("manta_inline_task", default=None)


def begin(request: Any) -> str | None:
    call = getattr(request, "tool_call", {})
    if call.get("name") != "task" or not call.get("id"):
        return None
    args = call.get("args") or {}
    if not args.get("subagent_type"):
        return None
    config = getattr(getattr(request, "runtime", None), "config", {}) or {}
    configurable = config.get("configurable", {})
    thread = str(configurable.get("thread_id") or "")
    # No durable thread identity means no safe replay identity. Keep ordinary
    # event logging, rather than inventing duplicate task rows on a restart.
    if not thread:
        return None
    parent = CURRENT_INLINE_TASK.get() or os.environ.get("MANTA_TASK_ID", "")
    identity = hashlib.sha256(f"{thread}\0{parent}\0{call['id']}".encode()).hexdigest()[:24]
    task_id = "inline-" + identity
    record = store.get_task(task_id)
    if record is None:
        store.create_task(store.TaskRecord(
            id=task_id, agent=str(args["subagent_type"]), prompt=str(args.get("description", "")),
            state="running", started_at=time.time(), pid=os.getpid(), thread_id=thread,
            parent_task_id=parent if store.get_task(parent) else "", execution_kind="inline",
            manifest={"execution_kind": "inline", "parent_thread_id": thread,
                      "tool_call_id": call["id"], "shared_checkout": str(Path.cwd())},
        ))
        store.record_event(store.EventRecord(agent=str(args["subagent_type"]), kind="task_started", task_id=task_id))
    elif record.state in (*store.PENDING_STATES, "interrupted"):
        store.update_task(task_id, state="running", pending_reason="", checkpoint={},
                          pid=os.getpid(), expect_state=record.state)
    return task_id


def _text(result: Any) -> str:
    from langgraph.types import Command
    if isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages", [])
        return "\n".join(str(getattr(message, "content", "")) for message in messages)[-16000:]
    return str(getattr(result, "content", result) or "")[-16000:]


def finish(task_id: str, result: Any = None, exc: BaseException | None = None) -> None:
    record = store.get_task(task_id)
    if record is None:
        return
    if exc is not None and type(exc).__name__ == "GraphInterrupt":
        from .approvals import checkpoint_requests
        requests = {item.id: item.value for item in exc.args[0]}
        checkpoint_requests(task_id, record.thread_id, requests)
        return
    cancelled = exc is not None and type(exc).__name__ in {"CancelledError", "KeyboardInterrupt"}
    failed = exc is not None or getattr(result, "status", None) == "error"
    state = "cancelled" if cancelled else "failed" if failed else "done"
    summary = _text(result) if exc is None else "Execution stopped; inspect the parent conversation."
    evidence = {**record.structured_result, "summary": summary,
                "execution_kind": "inline", "shared_checkout": record.manifest.get("shared_checkout"),
                "parent_thread_id": record.thread_id}
    if store.update_task(task_id, state=state, result=summary, finished_at=time.time(),
                         pid=None, checkpoint={}, structured_result=evidence, expect_state="running"):
        store.record_event(store.EventRecord(agent=record.agent, kind=f"task_{state}", task_id=task_id))


def interrupt_orphans(thread_id: str | None = None) -> None:
    """Retain recovery guidance when a parent server disappeared."""
    for record in store.list_tasks(state="running", limit=-1):
        if record.execution_kind != "inline" or (thread_id and record.thread_id != thread_id):
            continue
        try:
            if record.pid:
                os.kill(record.pid, 0)
                continue
        except PermissionError:
            continue  # Lack of permission is not proof that a process exited.
        except ProcessLookupError:
            pass
        store.update_task(record.id, state="interrupted", pid=None,
                          pending_reason=f"Resume parent conversation {record.thread_id} to recover this inline delegation.",
                          expect_state="running")
