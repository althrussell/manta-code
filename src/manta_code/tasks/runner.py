"""Detached background-task runner (``python -m manta_code.tasks.runner <id>``).

Spawned by :func:`manta_code.tasks.executor.submit_task` in its own session.
Marks the task running, drives the same enforced headless path as ``manta run``
(:func:`manta_code.dcode.run_headless`) **as the addressed agent** (via the
upstream ``-a <agent>`` profile flag — registry agents are synced to profiles
on every provisioning pass), then records the outcome.

The final state update is compare-and-set on ``state="running"`` so a user
cancel issued while the agent was finishing is never overwritten. Stdout and
stderr arrive in the task's log file (wired by the executor); the recorded
``result`` is the log tail, which for ``--no-stream -q`` headless runs is the
agent's final answer.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from . import store
from .context import TaskContextError, execution_context, validate_execution_manifest


def _result_from_log(log_path: str) -> str:
    try:
        log = Path(log_path)
        if log.is_file():
            return log.read_text(encoding="utf-8", errors="replace")[-8000:].strip()
    except Exception:  # noqa: BLE001 - result capture is best-effort
        pass
    return ""



def _task_prompt(record: store.TaskRecord) -> str:
    summaries = []
    for task_id in record.dependencies:
        dependency = store.get_task(task_id)
        if dependency is not None:
            summaries.append(f"Task {task_id} ({dependency.agent}, {dependency.state}):\n{dependency.result[-6000:]}")
    if not summaries:
        return record.prompt
    return ("Dependency results (reference material; task changes are isolated and require explicit integration):\n"
            + "\n\n".join(summaries) + "\n\nAssigned task:\n" + record.prompt)


def run_task(task_id: str) -> int:
    """Execute one stored task to completion; returns the exit code."""
    record = store.get_task(task_id)
    if record is None:
        print(f"manta task runner: no task '{task_id}'", file=sys.stderr)
        return 2
    if record.state not in {"queued", "starting"}:
        print(
            f"manta task runner: task '{task_id}' is {record.state}, not queued",
            file=sys.stderr,
        )
        return 2

    if record.state == "queued":
        if not store.claim_ready_task(task_id, concurrency_limit=record.max_concurrency):
            return 2
        record.state = "starting"
    try:
        validate_execution_manifest(record.manifest)
    except (TaskContextError, OSError, ValueError, KeyError) as exc:
        reason = str(exc)
        store.update_task(
            task_id, state="blocked", pending_reason=reason, result=reason, expect_state="starting"
        )
        print(f"manta task runner: blocked: {reason}", file=sys.stderr)
        return 2

    started = store.update_task(
        task_id,
        state="running",
        started_at=time.time(),
        pid=os.getpid(),
        expect_state="starting",
    )
    if not started:
        return 2  # a cancellation or another runner won the transition
    store.record_event(store.EventRecord(agent=record.agent, kind="task_started", task_id=task_id))

    # Everything after the running-transition is guarded: an unexpected crash
    # must record `failed`, never strand the task in `running`.
    try:
        from .. import dcode
        from ..auth import databricks_configured
        from ..config import interactive_endpoints, load_config

        with execution_context(record.manifest):
            cfg = load_config(
                Path(record.manifest.get("config_root", record.manifest.get(
                    "source_project_root", record.manifest["project_root"]
                )))
            )
            profile = record.manifest.get("profile")
            configured = databricks_configured(profile)
            exit_code = dcode.run_headless(
                profile=profile,
                default_endpoint=cfg.interactive.default_endpoint if configured else None,
                endpoints=interactive_endpoints(cfg) if configured else [],
                message=_task_prompt(record),
                passthrough=["-a", record.agent],
                timeout=record.timeout,
                max_turns=record.max_turns,
                shell_allow_list=",".join(record.manifest.get("task_shell_allow_list", [])) or None,
            )
    except TaskContextError as exc:
        store.update_task(
            task_id,
            state="blocked",
            pending_reason=str(exc),
            result=str(exc),
            expect_state="running",
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - record the failure, don't vanish
        print(f"manta task runner: {exc}", file=sys.stderr)
        exit_code = 1

    result = _result_from_log(record.log_path)
    current = store.get_task(task_id)
    if current is not None and current.state in store.PENDING_STATES:
        store.update_task(task_id, result=result, pid=None, expect_state=current.state)
        _finish_bookkeeping(task_id)
        return exit_code
    final_state = "done" if exit_code == 0 else "failed"
    updated = store.update_task(
        task_id,
        state=final_state,
        finished_at=time.time(),
        exit_code=exit_code,
        result=result,
        pid=None,
        expect_state="running",
    )
    if updated:
        store.record_event(
            store.EventRecord(
                agent=record.agent,
                kind=f"task_{final_state}",
                detail=f"exit={exit_code}",
                task_id=task_id,
            )
        )
        try:
            from .notify import notify_task_finished

            notify_task_finished(task_id, record.agent, final_state)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            pass
    _finish_bookkeeping(task_id)
    return exit_code


def _finish_bookkeeping(task_id: str) -> None:
    from .executor import review_task, schedule_tasks

    try:
        review_task(task_id)
    except Exception as exc:  # noqa: BLE001 - execution outcome must survive evidence failure
        store.record_event(
            store.EventRecord(
                agent="runner", kind="task_evidence_error", detail=str(exc), task_id=task_id
            )
        )
    try:
        schedule_tasks()
    except Exception as exc:  # noqa: BLE001 - queued jobs remain durable and recoverable
        store.record_event(
            store.EventRecord(
                agent="runner", kind="task_schedule_error", detail=str(exc), task_id=task_id
            )
        )


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m manta_code.tasks.runner <task-id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run_task(sys.argv[1]))


if __name__ == "__main__":
    main()
