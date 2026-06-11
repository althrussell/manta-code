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

#: Env var carrying the task-nesting depth (a task submitting a task).
TASK_DEPTH_ENV = "MANTA_TASK_DEPTH"

#: Maximum task-nesting depth. Depth 0 = an interactive session submitting a
#: task; depth 1 = that task's agent submitting one more (the chief fanning
#: out from the background). Beyond that is runaway recursion, refused.
MAX_TASK_DEPTH = 2

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

    # Refuse runaway recursion: a task spawning a task spawning a task…
    try:
        depth = int(os.environ.get(TASK_DEPTH_ENV, "0"))
    except ValueError:
        depth = 0
    if depth >= MAX_TASK_DEPTH:
        raise TaskError(
            f"task nesting limit reached (depth {depth}); a background task's "
            "tasks may not submit further tasks"
        )

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
    env[TASK_DEPTH_ENV] = str(depth + 1)
    if profile:
        env["DATABRICKS_CONFIG_PROFILE"] = profile

    argv = [sys.executable, "-m", "manta_code.tasks.runner", task_id]
    try:
        with log_path.open("ab") as log:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
    except OSError as exc:
        # Never leave a forever-queued row behind a failed spawn.
        store.update_task(
            task_id,
            state="failed",
            finished_at=time.time(),
            result=f"(failed to spawn runner: {exc})",
            path=db_path,
        )
        raise TaskError(f"could not start the task runner: {exc}") from exc
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
    # Compare-and-set on the observed state so a runner that finished between
    # our read and this write keeps its done/failed outcome.
    store.update_task(
        task_id,
        state="cancelled",
        finished_at=time.time(),
        expect_state=record.state,
        path=db_path,
    )
    store.record_event(
        store.EventRecord(agent=record.agent, kind="task_cancelled", task_id=task_id),
        path=db_path,
    )
    refreshed = store.get_task(task_id, path=db_path)
    assert refreshed is not None
    return refreshed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


#: Age below which an active task is never reconciled — a submission is mid
#: flight for a moment before its runner pid lands and reports running.
RECONCILE_GRACE_SECONDS = 30.0


def reconcile_stale_tasks(*, db_path: Path | None = None) -> list[store.TaskRecord]:
    """Mark active tasks whose runner process is dead as failed.

    A runner that crashes before its guarded section (or is SIGKILLed) can
    strand a task in ``queued``/``running``. Called from the task CLI paths so
    the user always sees truthful states; returns the tasks it repaired.
    """
    repaired: list[store.TaskRecord] = []
    now = time.time()
    for record in store.list_tasks(limit=100, path=db_path):
        if record.state not in store.ACTIVE_STATES:
            continue
        if not record.pid:
            continue  # submission still in flight; nothing to probe
        if now - record.created_at < RECONCILE_GRACE_SECONDS:
            continue
        if _pid_alive(record.pid):
            continue
        changed = store.update_task(
            record.id,
            state="failed",
            finished_at=time.time(),
            result=record.result or "(runner process died without recording an outcome)",
            expect_state=record.state,
            path=db_path,
        )
        if changed:
            store.record_event(
                store.EventRecord(
                    agent=record.agent,
                    kind="task_failed",
                    detail="runner died",
                    task_id=record.id,
                ),
                path=db_path,
            )
            refreshed = store.get_task(record.id, path=db_path)
            if refreshed is not None:
                repaired.append(refreshed)
    return repaired


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
