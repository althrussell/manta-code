"""Submit and cancel detached background tasks (ADR 0010, Phase B).

Execution model — **detached subprocess per task**, no daemon: ``submit_task``
spawns ``python -m manta_code.tasks.runner <task-id>`` in its own session
(``start_new_session=True``) with stdout/stderr appended to the task's log
file, then returns immediately with the task id. The runner survives the
session (TUI or shell) that submitted it; outcome lands in the task store.

``cancel_task`` signals the runner's whole process group (the runner is a
session leader, so its langgraph/server children die with it) and marks the
task cancelled — the runner's own final update is compare-and-set on
``state="running"`` so a cancel always wins.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import store

#: Env var the runner/middleware use to attribute events + usage to a task.
TASK_ID_ENV = "MANTA_TASK_ID"

#: Default wall-clock timeout for a background task (seconds). More generous
#: than interactive ``manta run``: background work is expected to take a while.
DEFAULT_TASK_TIMEOUT = 1800

#: Default agentic-turn cap for a background task.
DEFAULT_TASK_MAX_TURNS = 80


class TaskError(RuntimeError):
    """Raised when a task cannot be submitted or cancelled."""


def known_agent_names() -> set[str]:
    """Names of all addressable agents (built-ins + user registry)."""
    from ..agents.defaults import merged_agents
    from ..agents.registry import list_agents

    return {a.name for a in merged_agents(list_agents())}


def submit_task(
    agent: str,
    prompt: str,
    *,
    timeout: int = DEFAULT_TASK_TIMEOUT,
    max_turns: int = DEFAULT_TASK_MAX_TURNS,
    profile: str | None = None,
    db_path: Path | None = None,
) -> store.TaskRecord:
    """Create a task and spawn its detached runner; returns the queued record."""
    agent = (agent or "").strip().lstrip("@")
    if not agent:
        raise TaskError("an agent name is required")
    known = known_agent_names()
    if agent not in known:
        raise TaskError(
            f"no Manta agent named '{agent}' (known: {', '.join(sorted(known))})"
        )
    if not prompt or not prompt.strip():
        raise TaskError("a non-empty task prompt is required")

    task_id = store.new_task_id()
    log_dir = store.task_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.log"

    record = store.create_task(
        store.TaskRecord(
            id=task_id,
            agent=agent,
            prompt=prompt.strip(),
            log_path=str(log_path),
            timeout=timeout,
            max_turns=max_turns,
        ),
        path=db_path,
    )

    env = dict(os.environ)
    env[TASK_ID_ENV] = task_id
    if profile:
        env["DATABRICKS_CONFIG_PROFILE"] = profile

    argv = [sys.executable, "-m", "manta_code.tasks.runner", task_id]
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    store.update_task(task_id, pid=process.pid, path=db_path)
    store.record_event(
        store.EventRecord(
            agent=agent,
            kind="task_submitted",
            detail=prompt.strip()[:200],
            task_id=task_id,
        ),
        path=db_path,
    )
    record.pid = process.pid
    return record


def cancel_task(task_id: str, *, db_path: Path | None = None) -> store.TaskRecord:
    """Cancel a queued/running task, signalling its process group."""
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    if record.state not in store.ACTIVE_STATES:
        raise TaskError(f"task '{task_id}' is already {record.state}")

    if record.pid:
        try:
            os.killpg(record.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # already gone (or not ours); state update below still applies
    store.update_task(
        task_id,
        state="cancelled",
        finished_at=time.time(),
        path=db_path,
    )
    store.record_event(
        store.EventRecord(agent=record.agent, kind="task_cancelled", task_id=task_id),
        path=db_path,
    )
    refreshed = store.get_task(task_id, path=db_path)
    assert refreshed is not None
    return refreshed


def task_output(task_id: str, *, db_path: Path | None = None) -> str:
    """Return a finished task's result (or the log tail while it runs)."""
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    if record.result:
        return record.result
    log = Path(record.log_path) if record.log_path else None
    if log and log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        return text[-8000:]
    return ""
