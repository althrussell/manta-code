"""In-session task-management tools (chief-of-staff surface, ADR 0010 Phase B).

LangChain tools over the task store/executor so background work is managed
*inside* a session — the orchestrator gets them (injected by the build hook)
and the ``chief`` built-in agent leans on them. The same data backs the
``manta task`` / ``manta status`` CLI, so there is one source of truth either
way.

All functions return plain strings (the agent reads them); failures are
reported as text rather than raised, so a store hiccup never crashes a turn.
"""

from __future__ import annotations

import time
from typing import Any


def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def manta_task_submit(agent: str, prompt: str) -> str:
    """Hand a long-running task to a named Manta agent in the background.

    The task runs detached (it survives this session) with the agent's
    enforced permissions, model pin, and budget. Returns the task id to poll.
    """
    from .executor import TaskError, submit_task

    try:
        record = submit_task(agent, prompt)
    except TaskError as exc:
        return f"Could not submit task: {exc}"
    return (
        f"Submitted background task {record.id} to @{record.agent}. "
        f"Check progress with manta_task_status('{record.id}') and collect the "
        f"result with manta_task_output('{record.id}')."
    )


def manta_task_status(task_id: str) -> str:
    """Check the state of one background task by id."""
    from .store import get_task

    record = get_task(task_id.strip())
    if record is None:
        return f"No task '{task_id}'."
    bits = [
        f"task {record.id}: @{record.agent} — {record.state}",
        f"submitted {_age(record.created_at)} ago",
    ]
    if record.started_at:
        bits.append(f"started {_age(record.started_at)} ago")
    if record.exit_code is not None:
        bits.append(f"exit={record.exit_code}")
    bits.append(f"prompt: {record.prompt[:160]}")
    return "; ".join(bits)


def manta_task_output(task_id: str) -> str:
    """Collect a background task's output (final result, or log tail if running)."""
    from .executor import TaskError, task_output
    from .store import get_task

    record = get_task(task_id.strip())
    if record is None:
        return f"No task '{task_id}'."
    try:
        output = task_output(task_id.strip())
    except TaskError as exc:
        return str(exc)
    header = f"[task {record.id} @{record.agent} — {record.state}]"
    return f"{header}\n{output or '(no output yet)'}"


def manta_task_list(state: str = "") -> str:
    """List recent background tasks (optionally filtered by state).

    States: queued, running, done, failed, cancelled. Empty = all.
    """
    from .store import STATES, list_tasks

    state = state.strip().lower()
    if state and state not in STATES:
        return f"Unknown state '{state}'. States: {', '.join(STATES)}."
    tasks = list_tasks(state=state or None, limit=20)
    if not tasks:
        return "No background tasks yet."
    lines = [
        f"{t.id}  @{t.agent:<10} {t.state:<9} {_age(t.created_at):>6} ago  "
        f"{t.prompt[:80]}"
        for t in tasks
    ]
    return "\n".join(lines)


def manta_task_cancel(task_id: str) -> str:
    """Cancel a queued or running background task by id."""
    from .executor import TaskError, cancel_task

    try:
        record = cancel_task(task_id.strip())
    except TaskError as exc:
        return str(exc)
    return f"Cancelled task {record.id} (@{record.agent})."


def build_task_tools() -> list[Any]:
    """Compile the task-management functions into LangChain tools.

    Guarded: returns ``[]`` if langchain is unavailable, so the build hook can
    call it unconditionally.
    """
    try:
        from langchain_core.tools import StructuredTool
    except Exception:  # noqa: BLE001 - optional extra missing
        return []
    functions = (
        manta_task_submit,
        manta_task_status,
        manta_task_output,
        manta_task_list,
        manta_task_cancel,
    )
    return [StructuredTool.from_function(fn) for fn in functions]
