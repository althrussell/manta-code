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


def _result_from_log(log_path: str) -> str:
    try:
        log = Path(log_path)
        if log.is_file():
            return log.read_text(encoding="utf-8", errors="replace")[-8000:].strip()
    except Exception:  # noqa: BLE001 - result capture is best-effort
        pass
    return ""


def _ensure_valid_cwd() -> None:
    """Recover when our inherited working directory has been deleted.

    A task submitted from *inside* a session (the ``@agent … &`` path or the
    chief's task tools) inherits that session's server cwd — which can be an
    ephemeral directory that is deleted moments later when the session ends.
    The first ``Path.cwd()`` (config loading) then dies with
    ``FileNotFoundError`` before the task even starts. Fall back to home.
    """
    try:
        os.getcwd()
    except OSError:
        home = str(Path.home())
        os.chdir(home)
        print(f"manta task runner: working directory vanished; using {home}", file=sys.stderr)


def run_task(task_id: str) -> int:
    """Execute one stored task to completion; returns the exit code."""
    _ensure_valid_cwd()
    record = store.get_task(task_id)
    if record is None:
        print(f"manta task runner: no task '{task_id}'", file=sys.stderr)
        return 2
    if record.state != "queued":
        print(
            f"manta task runner: task '{task_id}' is {record.state}, not queued",
            file=sys.stderr,
        )
        return 2

    store.update_task(
        task_id,
        state="running",
        started_at=time.time(),
        pid=os.getpid(),
        expect_state="queued",
    )
    store.record_event(
        store.EventRecord(agent=record.agent, kind="task_started", task_id=task_id)
    )

    # Everything after the running-transition is guarded: an unexpected crash
    # must record `failed`, never strand the task in `running`.
    try:
        from .. import dcode
        from ..auth import databricks_configured
        from ..config import interactive_endpoints, load_config

        cfg = load_config()
        configured = databricks_configured()
        exit_code = dcode.run_headless(
            profile=None,  # the executor exported the profile into our env
            default_endpoint=cfg.interactive.default_endpoint if configured else None,
            endpoints=interactive_endpoints(cfg) if configured else [],
            message=record.prompt,
            passthrough=["-a", record.agent],
            timeout=record.timeout,
            max_turns=record.max_turns,
        )
    except Exception as exc:  # noqa: BLE001 - record the failure, don't vanish
        print(f"manta task runner: {exc}", file=sys.stderr)
        exit_code = 1

    result = _result_from_log(record.log_path)
    final_state = "done" if exit_code == 0 else "failed"
    updated = store.update_task(
        task_id,
        state=final_state,
        finished_at=time.time(),
        exit_code=exit_code,
        result=result,
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
    return exit_code


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m manta_code.tasks.runner <task-id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run_task(sys.argv[1]))


if __name__ == "__main__":
    main()
