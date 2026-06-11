"""Local task + event store (ADR 0010, Phase B).

Same design rules as the usage ledger (:mod:`manta_code.agents.usage`):
local-only SQLite under ``~/.manta/.state/tasks.db`` (honoring ``MANTA_HOME``),
stdlib-only, and **never raises into a hot path** — event recording is
best-effort, while task CRUD (driven by the CLI and runner, not the model loop)
raises normally so callers see real errors.

Task lifecycle: ``queued`` → ``running`` → ``done`` | ``failed`` | ``cancelled``.
State transitions by the runner are conditional on the current state so a
``cancel`` issued while the runner is finishing is never overwritten.

Events are deliberately lightweight rows (agent, kind, detail, optional
task id) appended by the event middleware and the executor; ``manta status``
joins them with tasks and the usage ledger into the single pane of glass.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import user_manta_dir

#: Where the task store lives (relative to the Manta home dir).
TASKS_DB_PATH = Path(".state") / "tasks.db"

#: Where detached task logs live (relative to the Manta home dir).
TASK_LOG_DIR = Path(".state") / "task-logs"

#: Valid task states.
STATES = ("queued", "running", "done", "failed", "cancelled")

#: States in which a task is still alive (cancellable, not collectable).
ACTIVE_STATES = ("queued", "running")


@dataclass
class TaskRecord:
    """One submitted task."""

    id: str
    agent: str
    prompt: str
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    pid: int | None = None
    exit_code: int | None = None
    log_path: str = ""
    result: str = ""
    timeout: int | None = None
    max_turns: int | None = None


@dataclass(frozen=True)
class EventRecord:
    """One lightweight observability event."""

    agent: str
    kind: str
    detail: str = ""
    task_id: str | None = None
    ts: float = field(default_factory=time.time)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    prompt TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    pid INTEGER,
    exit_code INTEGER,
    log_path TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    timeout INTEGER,
    max_turns INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
"""


def tasks_db_path(path: Path | None = None) -> Path:
    return path or (user_manta_dir() / TASKS_DB_PATH)


def task_log_dir(path: Path | None = None) -> Path:
    return path or (user_manta_dir() / TASK_LOG_DIR)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and migrate) the task store, returning a connection."""
    db_path = tasks_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def new_task_id() -> str:
    """Short, human-typable task id."""
    return uuid.uuid4().hex[:8]


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        agent=row["agent"],
        prompt=row["prompt"],
        state=row["state"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        pid=row["pid"],
        exit_code=row["exit_code"],
        log_path=row["log_path"],
        result=row["result"],
        timeout=row["timeout"],
        max_turns=row["max_turns"],
    )


def create_task(record: TaskRecord, *, path: Path | None = None) -> TaskRecord:
    """Insert a new task row."""
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, agent, prompt, state, created_at, started_at, "
            "finished_at, pid, exit_code, log_path, result, timeout, max_turns) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.id,
                record.agent,
                record.prompt,
                record.state,
                record.created_at,
                record.started_at,
                record.finished_at,
                record.pid,
                record.exit_code,
                record.log_path,
                record.result,
                record.timeout,
                record.max_turns,
            ),
        )
        conn.commit()
        return record
    finally:
        conn.close()


def get_task(task_id: str, *, path: Path | None = None) -> TaskRecord | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None
    finally:
        conn.close()


def list_tasks(
    *,
    state: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[TaskRecord]:
    conn = connect(path)
    try:
        if state:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()


def update_task(
    task_id: str,
    *,
    path: Path | None = None,
    expect_state: str | None = None,
    **fields: object,
) -> bool:
    """Update ``fields`` on a task; returns ``True`` when a row changed.

    ``expect_state`` makes the update conditional (compare-and-set): the runner
    finishing a task uses ``expect_state="running"`` so it never resurrects a
    task the user cancelled in the meantime.
    """
    allowed = {
        "state",
        "started_at",
        "finished_at",
        "pid",
        "exit_code",
        "log_path",
        "result",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown task fields: {sorted(unknown)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"invalid task state: {fields['state']!r}")
    if not fields:
        return False
    sets = ", ".join(f"{name} = ?" for name in fields)
    values: list[object] = list(fields.values())
    where = "id = ?"
    values.append(task_id)
    if expect_state is not None:
        where += " AND state = ?"
        values.append(expect_state)
    conn = connect(path)
    try:
        cursor = conn.execute(f"UPDATE tasks SET {sets} WHERE {where}", values)  # noqa: S608 - column names validated against allow-list
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# --- events ----------------------------------------------------------------


def record_event(event: EventRecord, *, path: Path | None = None) -> None:
    """Append one event row. Best-effort: never raises into the agent loop."""
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001 - observability must not break a run
        return
    try:
        conn.execute(
            "INSERT INTO events (ts, agent, kind, detail, task_id) VALUES (?,?,?,?,?)",
            (event.ts, event.agent, event.kind, event.detail, event.task_id),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def recent_events(
    *,
    task_id: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[EventRecord]:
    conn = connect(path)
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY ts DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            EventRecord(
                agent=r["agent"],
                kind=r["kind"],
                detail=r["detail"],
                task_id=r["task_id"],
                ts=r["ts"],
            )
            for r in rows
        ]
    finally:
        conn.close()
