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
import json
from pathlib import Path

from . import store
from .context import (
    TaskContextError, build_execution_manifest, resolve_profile as resolve_profile,
    validate_execution_manifest,
)

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
    allow_asks: bool = False,
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
    try:
        manifest = build_execution_manifest(agent, profile)
    except (TaskContextError, OSError, ValueError) as exc:
        raise TaskError(str(exc)) from exc

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
    manifest["task_depth"] = depth + 1

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
            allow_asks=allow_asks,
            manifest=manifest,
        ),
        path=db_path,
    )

    return _spawn_task(record, depth=depth + 1, db_path=db_path)


def _spawn_task(
    record: store.TaskRecord, *, depth: int,
    resume_value: dict | None = None, db_path: Path | None = None,
) -> store.TaskRecord:
    task_id = record.id
    log_path = Path(record.log_path)
    env = dict(os.environ)
    env[TASK_ID_ENV] = task_id
    env[TASK_DEPTH_ENV] = str(depth)
    env.pop("MANTA_RESUME_THREAD_ID", None)
    env.pop("MANTA_RESUME_VALUE", None)
    if resume_value is not None:
        env["MANTA_RESUME_THREAD_ID"] = record.thread_id
        env["MANTA_RESUME_VALUE"] = json.dumps(resume_value)
    if record.allow_asks:
        # Human pre-approval of ask-gated tools, granted at submission
        # (ADR 0011) — the policy layer reads this in the detached runner.
        env["MANTA_ALLOW_ASKS"] = "1"
    else:
        env.pop("MANTA_ALLOW_ASKS", None)
    profile = record.manifest.get("profile")
    if profile:
        env["MANTA_PROFILE"] = profile
        env["DATABRICKS_CONFIG_PROFILE"] = profile
    else:
        env.pop("MANTA_PROFILE", None)
        env.pop("DATABRICKS_CONFIG_PROFILE", None)
    env["DATABRICKS_CONFIG_FILE"] = record.manifest["config_file"]
    env["MANTA_ORIGINAL_CWD"] = record.manifest["original_cwd"]
    env["MANTA_PROJECT_ROOT"] = record.manifest["project_root"]

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
                cwd=record.manifest["original_cwd"],
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
    store.update_task(task_id, pid=process.pid, expect_state="queued", path=db_path)
    store.record_event(
        store.EventRecord(
            agent=record.agent,
            kind="task_resumed" if resume_value is not None else "task_submitted",
            task_id=task_id,
        ),
        path=db_path,
    )
    record.pid = process.pid
    return record


def resume_task(
    task_id: str, *, resume_value: dict | None = None, db_path: Path | None = None,
) -> store.TaskRecord:
    """Continue a persisted budget checkpoint in its original execution context."""
    record = store.get_task(task_id, path=db_path)
    if record is None or record.state != "paused_budget":
        raise TaskError("Only a task paused at a budget checkpoint can be resumed.")
    value = resume_value if resume_value is not None else record.checkpoint.get("resume_value")
    if not record.thread_id or not isinstance(value, dict) or not value:
        raise TaskError("The task has no resumable checkpoint; inspect its output and submit a new task.")
    if record.pid and _pid_alive(record.pid):
        raise TaskError("The paused runner is still stopping; retry resume after it exits.")
    try:
        validate_execution_manifest(record.manifest)
    except (TaskContextError, OSError, ValueError) as exc:
        raise TaskError(str(exc)) from exc
    if not store.update_task(task_id, state="queued", pid=None, finished_at=None,
                             exit_code=None, result="", pending_reason="", expect_state="paused_budget", path=db_path):
        raise TaskError("The task changed while resuming; inspect its current state.")
    record.state = "queued"
    return _spawn_task(record, depth=record.manifest.get("task_depth", 1), resume_value=value, db_path=db_path)


def cancel_task(task_id: str, *, db_path: Path | None = None) -> store.TaskRecord:
    """Cancel a queued/running task, signalling its process group."""
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    if record.state not in (*store.ACTIVE_STATES, "paused_budget", "blocked"):
        raise TaskError(f"task '{task_id}' is already {record.state}")

    if record.pid and record.state in store.ACTIVE_STATES:
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


def send_to_task(
    task_id: str, message: str, *, db_path: Path | None = None
) -> store.InboxMessage:
    """Queue a steering message for a queued/running task (ADR 0011).

    The task's InboxMiddleware delivers it as a checkpointed user note before
    the task's next model call. Finished tasks reject with their terminal
    state.
    """
    if not message or not message.strip():
        raise TaskError("a non-empty steering message is required")
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    if record.state not in store.ACTIVE_STATES:
        raise TaskError(
            f"task '{task_id}' is already {record.state}; steering only "
            "reaches queued or running tasks"
        )
    queued = store.add_inbox_message(task_id, message.strip(), path=db_path)
    store.record_event(
        store.EventRecord(
            agent=record.agent,
            kind="task_steered",
            detail=message.strip()[:200],
            task_id=task_id,
        ),
        path=db_path,
    )
    return queued
