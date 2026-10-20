"""Detached queue supervisor, active only while unstarted tasks remain.

Each submission may request a supervisor. An installation-local nonblocking
lock elects one process; all dependency decisions and reservations remain in
SQLite. A crashed worker is marked interrupted rather than silently replayed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import store


def ensure_scheduler(*, db_path: Path | None = None) -> None:
    if not any(
        task.state in {"queued", "waiting_dependencies"} and task.execution_kind == "detached"
        for task in store.list_tasks(limit=-1, path=db_path)
    ):
        return
    log = store.task_log_dir() / "scheduler.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    from ..config import user_manta_dir
    env["MANTA_HOME"] = str(user_manta_dir().expanduser().resolve())
    env["MANTA_TASK_DB_PATH"] = str(store.tasks_db_path(db_path).resolve())
    # Scheduler is a control-plane process, never an agent task or continuation.
    for key in (
        "MANTA_TASK_ID",
        "MANTA_RESUME_THREAD_ID",
        "MANTA_RESUME_VALUE",
        "MANTA_RESUME_PENDING",
    ):
        env.pop(key, None)
    with log.open("ab") as output:
        subprocess.Popen(
            [sys.executable, "-m", "manta_code.tasks.scheduler"],
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent),
        )  # noqa: S603


def run() -> None:
    # POSIX is required by detached process groups elsewhere in this executor.
    import fcntl
    from .executor import reconcile_stale_tasks, schedule_tasks

    lock = store.tasks_db_path().with_suffix(".supervisor.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            reconcile_stale_tasks()
            schedule_tasks(concurrency_limit=16)
            if not any(
                task.state in {"queued", "waiting_dependencies"}
                and task.execution_kind == "detached"
                for task in store.list_tasks(limit=-1)
            ):
                return
            time.sleep(1)


if __name__ == "__main__":
    run()
