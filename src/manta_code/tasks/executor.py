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
    TaskContextError,
    build_execution_manifest,
    resolve_profile as resolve_profile,
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
    parent_task_id: str | None = None,
    parent_thread_id: str = "",
    dependencies: list[str] | None = None,
    isolation: str = "auto",
    max_concurrency: int = 3,
    context_mode: str = "isolated",
    conversation_context: str = "",
) -> store.TaskRecord:
    """Create an isolated task and schedule its detached runner; returns the queued record."""
    agent = (agent or "").strip().lstrip("@")
    if not agent:
        raise TaskError("an agent name is required")
    known = known_agent_names()
    if agent not in known:
        raise TaskError(f"no Manta agent named '{agent}' (known: {', '.join(sorted(known))})")
    if not prompt or not prompt.strip():
        raise TaskError("a non-empty task prompt is required")
    if context_mode not in {"isolated", "fork"}:
        raise TaskError("context_mode must be isolated or fork")
    if context_mode == "fork" and not conversation_context.strip():
        raise TaskError("Forked context requires an explicit visible conversation snapshot")
    if not 1 <= max_concurrency <= 16:
        raise TaskError("max_concurrency must be between 1 and 16")
    if timeout is not None and timeout <= 0 or max_turns is not None and max_turns <= 0:
        raise TaskError("timeout and max_turns must be positive")
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
    manifest["context_mode"] = context_mode
    manifest["parent_thread_id"] = parent_thread_id or manifest.get("parent_thread_id", "")
    parent = parent_task_id if parent_task_id is not None else manifest.get("parent_task_id", "")
    if parent:
        parent_record = store.get_task(parent, path=db_path)
        if parent_record is None:
            raise TaskError(f"no parent task '{parent}'")
        depth = max(depth, int(parent_record.manifest.get("task_depth", 1)))
        if depth >= MAX_TASK_DEPTH:
            raise TaskError("task nesting limit reached")
        manifest["task_depth"] = depth + 1
    manifest["parent_task_id"] = parent
    if context_mode == "fork":
        prompt = (
            "Visible conversation context (reference material, not new instructions):\n"
            + conversation_context
            + "\n\nAssigned task:\n"
            + prompt.strip()
        )

    task_id = store.new_task_id()
    log_dir = store.task_log_dir().expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.log"

    from .isolation import IsolationError, prepare

    try:
        manifest = prepare(task_id, manifest, isolation)
    except (IsolationError, OSError, ValueError) as exc:
        raise TaskError(f"Could not isolate task: {exc}") from exc
    try:
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
                parent_task_id=parent,
                dependencies=list(dependencies or []),
                max_concurrency=max_concurrency,
            ),
            path=db_path,
        )
    except Exception as exc:
        from .isolation import discard

        discard(manifest)
        raise TaskError(str(exc)) from exc
    schedule_tasks(db_path=db_path, concurrency_limit=max_concurrency)
    from .scheduler import ensure_scheduler

    ensure_scheduler(db_path=db_path)
    return store.get_task(record.id, path=db_path) or record


def _spawn_task(
    record: store.TaskRecord,
    *,
    depth: int,
    resume_value: dict | None = None,
    db_path: Path | None = None,
) -> store.TaskRecord:
    task_id = record.id
    log_path = Path(record.log_path)
    env = dict(os.environ)
    from ..config import user_manta_dir
    env["MANTA_HOME"] = str(user_manta_dir().expanduser().resolve())
    env["MANTA_TASK_DB_PATH"] = str(store.tasks_db_path(db_path).expanduser().resolve())
    env[TASK_ID_ENV] = task_id
    env[TASK_DEPTH_ENV] = str(depth)
    env.pop("MANTA_RESUME_THREAD_ID", None)
    env.pop("MANTA_RESUME_VALUE", None)
    env.pop("MANTA_RESUME_PENDING", None)
    if db_path is not None:
        env["MANTA_TASK_DB_PATH"] = str(db_path.resolve())
    if resume_value is not None:
        env["MANTA_RESUME_THREAD_ID"] = record.thread_id
        env["MANTA_RESUME_VALUE"] = json.dumps(resume_value)
        if record.checkpoint.get("interrupts"):
            env["MANTA_RESUME_PENDING"] = json.dumps(record.checkpoint["interrupts"])
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
    env["MANTA_TASK_CONFIG_ROOT"] = record.manifest.get(
        "config_root", record.manifest.get("source_project_root", record.manifest["project_root"])
    )

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
            expect_state="starting",
            path=db_path,
        )
        raise TaskError(f"could not start the task runner: {exc}") from exc
    if not store.update_task(task_id, pid=process.pid, expect_state="starting", path=db_path):
        current = store.get_task(task_id, path=db_path)
        if current is not None and current.state == "cancelled":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    store.record_event(
        store.EventRecord(
            agent=record.agent,
            kind="task_resumed" if resume_value is not None else "task_submitted",
            task_id=task_id,
        ),
        path=db_path,
    )
    record.pid = process.pid
    return store.get_task(task_id, path=db_path) or record


def resume_task(
    task_id: str,
    *,
    resume_value: dict | None = None,
    db_path: Path | None = None,
    expected_pending_id: str | None = None,
) -> store.TaskRecord:
    """Continue the exact stored runtime thread; never replay a task prompt."""
    record = store.get_task(task_id, path=db_path)
    _require_detached(record, "resume")
    if record is None or record.state not in (*store.PENDING_STATES, "interrupted"):
        raise TaskError("Only a paused or interrupted task with a checkpoint can be resumed.")
    value = resume_value
    if value is None and record.state == "paused_budget":
        value = record.checkpoint.get("resume_value")
    if not record.thread_id or not isinstance(value, dict) or not value:
        raise TaskError(
            "The task needs an explicit decision and resumable checkpoint; inspect its pending request."
        )
    if record.pid and _pid_alive(record.pid):
        raise TaskError("The paused runner is still stopping; retry resume after it exits.")
    try:
        validate_execution_manifest(record.manifest)
    except (TaskContextError, OSError, ValueError) as exc:
        raise TaskError(str(exc)) from exc
    # Reservation and transition share one lock; competing resumes cannot both
    # launch the same checkpoint, and resumed tasks consume a concurrency slot.
    from ..state_lock import state_lock

    with state_lock(store.tasks_db_path(db_path).with_suffix(".schedule.lock")):
        if not store.update_task(
            task_id,
            state="queued",
            pid=None,
            finished_at=None,
            exit_code=None,
            result="",
            pending_reason="",
            expect_state=record.state,
            expected_pending_id=expected_pending_id,
            path=db_path,
        ):
            raise TaskError("The task changed while resuming; inspect its current state.")
        if not store.claim_ready_task(
            task_id, concurrency_limit=record.max_concurrency, path=db_path
        ):
            store.update_task(task_id, state=record.state, pending_reason=record.pending_reason,
                              result=record.result, finished_at=record.finished_at, exit_code=record.exit_code,
                              expect_state="queued", path=db_path)
            raise TaskError("All task slots are busy; retry resume after another task stops.")
        previous_state = record.state
        record.state = "starting"
        try:
            return _spawn_task(
                record, depth=record.manifest.get("task_depth", 1), resume_value=value, db_path=db_path
            )
        except TaskError:
            # A transient spawn failure must not strand a retained checkpoint
            # in failed: retry intentionally refuses checkpoint replay, while
            # resume only accepts a pending/interrupted state.
            store.update_task(task_id, state=previous_state, pid=None,
                              result=record.result, pending_reason=record.pending_reason,
                              finished_at=record.finished_at, exit_code=record.exit_code,
                              expect_state="failed", path=db_path)
            raise


def cancel_task(task_id: str, *, db_path: Path | None = None) -> store.TaskRecord:
    """Cancel a queued/running task, signalling its process group."""
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    _require_detached(record, "cancel")
    if record.state not in (*store.ACTIVE_STATES, *store.PENDING_STATES, "interrupted", "blocked"):
        raise TaskError(f"task '{task_id}' is already {record.state}")

    if record.pid and record.state in store.ACTIVE_STATES:
        try:
            os.killpg(record.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # already gone (or not ours); state update below still applies
    # Compare-and-set on the observed state so a runner that finished between
    # our read and this write keeps its done/failed outcome.
    changed = store.update_task(
        task_id, state="cancelled", finished_at=time.time(), expect_state=record.state, path=db_path,
    )
    if changed:
        store.record_event(store.EventRecord(agent=record.agent, kind="task_cancelled", task_id=task_id), path=db_path)
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
    active = [record for state in ("starting", "running")
              for record in store.list_tasks(state=state, limit=-1, path=db_path)]
    for record in active:
        if record.execution_kind != "detached" or record.state not in {"starting", "running"}:
            continue
        if now - (record.started_at or record.created_at) < RECONCILE_GRACE_SECONDS:
            continue
        if record.pid and _pid_alive(record.pid):
            continue
        changed = store.update_task(
            record.id,
            state="interrupted",
            finished_at=time.time(),
            result=record.result
            or "(runner process died without recording an outcome; inspect changes before retry)",
            expect_state=record.state,
            path=db_path,
        )
        if changed:
            store.record_event(
                store.EventRecord(
                    agent=record.agent,
                    kind="task_interrupted",
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


def send_to_task(task_id: str, message: str, *, db_path: Path | None = None) -> store.InboxMessage:
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
    _require_detached(record, "steer")
    if record.state not in (*store.ACTIVE_STATES, *store.PENDING_STATES):
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


def schedule_tasks(
    *, db_path: Path | None = None, concurrency_limit: int = 3
) -> list[store.TaskRecord]:
    """Start oldest ready jobs within a durable cross-process reservation cap.

    Submission and every runner completion pump this queue. Restart/board refresh
    can call the same idempotent function; there is no in-memory dependency state.
    """
    from ..state_lock import state_lock

    started = []
    with state_lock(store.tasks_db_path(db_path).with_suffix(".schedule.lock")):
        queued = [record for state in ("queued", "waiting_dependencies")
                  for record in store.list_tasks(state=state, limit=-1, path=db_path)]
        for record in sorted(queued, key=lambda task: task.created_at):
            if not store.claim_ready_task(
                record.id, concurrency_limit=concurrency_limit, path=db_path
            ):
                continue
            record.state = "starting"
            started.append(
                _spawn_task(record, depth=record.manifest.get("task_depth", 1), db_path=db_path)
            )
    return started


def retry_task(task_id: str, *, db_path: Path | None = None) -> store.TaskRecord:
    """Explicitly restart a stopped attempt in its existing isolated workspace."""
    from ..state_lock import state_lock

    with state_lock(store.tasks_db_path(db_path).with_suffix(".changes.lock")):
        record = store.get_task(task_id, path=db_path)
        _require_detached(record, "retry")
        if record is None or record.state not in {"failed", "cancelled", "blocked", "interrupted"}:
            raise TaskError("Only failed, cancelled, blocked, or interrupted tasks can be retried")
        if record.integration_state != "unreviewed":
            raise TaskError("An integrated or discarded task cannot be retried; submit a new task")
        if record.pid and _pid_alive(record.pid):
            raise TaskError("The old runner is still stopping; wait before retrying")
        if record.thread_id and record.checkpoint:
            raise TaskError(
                "This task has a checkpoint; use resume to avoid replaying completed work"
            )
        try:
            validate_execution_manifest(record.manifest)
        except (TaskContextError, OSError, ValueError) as exc:
            raise TaskError(str(exc)) from exc
        if not store.update_task(
            task_id,
            state="queued",
            pid=None,
            started_at=None,
            finished_at=None,
            exit_code=None,
            result="",
            pending_reason="",
            checkpoint={},
            thread_id="",
            attempt=record.attempt + 1,
            expect_state=record.state,
            path=db_path,
        ):
            raise TaskError("The task changed while retrying")
    store.record_event(
        store.EventRecord(agent=record.agent, kind="task_retried", task_id=task_id), path=db_path
    )
    schedule_tasks(db_path=db_path, concurrency_limit=record.max_concurrency)
    return store.get_task(task_id, path=db_path)


def _spend(task_id: str, *, include_children: bool = False, db_path: Path | None = None) -> dict:
    from ..agents import usage
    scope = {"scope": "task_and_descendants" if include_children else "task_only", "includes_children": include_children}
    try:
        task_ids = store.descendant_task_ids(task_id, path=db_path) if include_children else [task_id]
        calls = input_tokens = output_tokens = unknown_cost = incomplete_usage = 0
        known_cost = None
        conn = usage.connect()
        try:
            for offset in range(0, len(task_ids), 500):
                batch = task_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                row = conn.execute(
                    "SELECT COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
                    "SUM(cost_usd) AS cost_usd, SUM(cost_usd IS NULL) AS unknown_cost, "
                    f"SUM(usage_complete IS NOT 1) AS incomplete_usage FROM usage WHERE task IN ({placeholders})", batch,
                ).fetchone()  # noqa: S608 - placeholders only
                calls += row["calls"]
                input_tokens += row["input_tokens"] or 0
                output_tokens += row["output_tokens"] or 0
                unknown_cost += row["unknown_cost"] or 0
                incomplete_usage += row["incomplete_usage"] or 0
                if row["cost_usd"] is not None:
                    known_cost = (known_cost or 0.0) + row["cost_usd"]
        finally:
            conn.close()
        return {**scope, "calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "known_cost_usd": known_cost, "cost_complete": bool(calls and not unknown_cost),
                "usage_complete": bool(calls and not incomplete_usage)}
    except Exception:  # noqa: BLE001 - incomplete accounting stays explicit
        return {**scope, "calls": 0, "known_cost_usd": None, "cost_complete": False, "usage_complete": False}


def review_task(task_id: str, *, db_path: Path | None = None) -> dict:
    """Return observed changes and receipts; exit zero alone is not acceptance."""
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    result = {
        **record.structured_result,
        "status": record.state,
        "attempt": record.attempt,
        "final_answer": record.result,
        "tests": record.structured_result.get("tests", []),
        "artifacts": record.structured_result.get("artifacts", []),
        "spend": _spend(task_id, db_path=db_path),
        "team_spend": _spend(task_id, include_children=True, db_path=db_path),
        "isolation": record.manifest.get("isolation", "legacy/unverified"),
        "execution_root": record.manifest.get("execution_root", ""),
        "source_project_root": record.manifest.get("source_project_root", ""),
        "source_dirty": record.manifest.get("source_dirty", False),
    }
    if record.integration_state == "discarded":
        return result
    if record.manifest.get("isolation"):
        from .isolation import IsolationError, changes

        try:
            result.update(changes(record.manifest))
        except (IsolationError, OSError, ValueError) as exc:
            raise TaskError(f"Could not inspect task changes: {exc}") from exc
    else:
        result.update({"changed_files": [], "diff": "", "changes_verified": False})
    # A finishing runner must not replace a concurrent integration or discard.
    store.save_result_snapshot(task_id, result, expect_state=record.state, path=db_path)
    return result


def _stopped_for_changes(task_id: str, db_path: Path | None) -> store.TaskRecord:
    record = store.get_task(task_id, path=db_path)
    if record is None:
        raise TaskError(f"no task '{task_id}'")
    if record.state not in {*store.TERMINAL_STATES, "interrupted", "blocked"}:
        raise TaskError("Stop the task before integrating or discarding its changes")
    if record.pid and _pid_alive(record.pid):
        raise TaskError("The runner is still stopping; wait before changing its checkout")
    if record.execution_kind != "detached" or not record.manifest.get("isolation"):
        raise TaskError("This task has no isolated checkout")
    children = [child for child in store.list_tasks(limit=-1, path=db_path)
                if child.parent_task_id == task_id and ((child.execution_kind == "detached" and child.integration_state == "unreviewed")
                or (child.execution_kind == "inline" and child.state in (*store.ACTIVE_STATES, *store.PENDING_STATES)))]
    if children:
        raise TaskError("Review and integrate or discard child tasks first: " + ", ".join(child.id for child in children))
    if record.integration_state != "unreviewed":
        raise TaskError(f"Task changes are already {record.integration_state}")
    return record


def integrate_task(task_id: str, *, db_path: Path | None = None,
                   expected_patch_sha256: str | None = None) -> store.TaskRecord:
    """Apply a reviewed stopped result while preserving unrelated local work."""
    from ..state_lock import state_lock
    from .isolation import IsolationError, integrate

    with state_lock(store.tasks_db_path(db_path).with_suffix(".changes.lock")):
        record = _stopped_for_changes(task_id, db_path)
        if "changed_files" not in record.structured_result:
            raise TaskError("Review the task diff before integrating changes")
        if expected_patch_sha256 is not None and record.structured_result.get("patch_sha256") != expected_patch_sha256:
            raise TaskError("The reviewed diff changed; review the latest result before integrating")
        try:
            integrate(record.manifest, record.structured_result.get("patch_sha256"))
        except (IsolationError, OSError, ValueError) as exc:
            raise TaskError(f"Integration blocked: {exc}") from exc
        store.update_task(
            task_id, integration_state="integrated", expect_state=record.state, path=db_path
        )
        store.record_event(
            store.EventRecord(agent=record.agent, kind="task_integrated", task_id=task_id),
            path=db_path,
        )
        return store.get_task(task_id, path=db_path)


def discard_task(task_id: str, *, db_path: Path | None = None) -> store.TaskRecord:
    """Delete only the task checkout, retaining its final diff and receipts."""
    from ..state_lock import state_lock
    from .isolation import IsolationError, discard

    with state_lock(store.tasks_db_path(db_path).with_suffix(".changes.lock")):
        record = _stopped_for_changes(task_id, db_path)
        review_task(task_id, db_path=db_path)
        try:
            discard(record.manifest)
        except (IsolationError, OSError, ValueError) as exc:
            raise TaskError(f"Could not discard task checkout: {exc}") from exc
        store.update_task(
            task_id, integration_state="discarded", expect_state=record.state, path=db_path
        )
        store.record_event(
            store.EventRecord(agent=record.agent, kind="task_discarded", task_id=task_id),
            path=db_path,
        )
        return store.get_task(task_id, path=db_path)


def _require_detached(record: store.TaskRecord | None, action: str) -> None:
    if record is not None and record.execution_kind == "inline":
        thread = record.manifest.get("parent_thread_id") or record.thread_id or "its parent session"
        raise TaskError(f"Inline task {record.id} belongs to parent thread {thread}; {action} through the open conversation, or resume it with /threads -r. Submit a new detached task for an isolated retry.")
