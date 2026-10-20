"""Local task + event store (ADR 0010, Phase B).

Same design rules as the usage ledger (:mod:`manta_code.agents.usage`):
local-only SQLite under ``~/.manta/.state/tasks.db`` (honoring ``MANTA_HOME``),
stdlib-only, and **never raises into a hot path** — event recording is
best-effort, while task CRUD (driven by the CLI and runner, not the model loop)
raises normally so callers see real errors.

Task lifecycle: ``queued`` → ``running`` → ``done`` | ``failed`` | ``cancelled``.
Budget interrupts persist as ``paused_budget`` until explicitly resumed, and
unverifiable execution context becomes ``blocked``. State transitions are
conditional so a cancellation or budget pause is never overwritten at exit.

Events are deliberately lightweight rows (agent, kind, detail, optional
task id) appended by the event middleware and the executor; ``manta status``
joins them with tasks and the usage ledger into the single pane of glass.
"""

from __future__ import annotations

import os
import sqlite3
import json
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
STATES = (
    "queued",
    "starting",
    "running",
    "waiting_dependencies",
    "waiting_input",
    "waiting_approval",
    "paused_budget",
    "blocked",
    "interrupted",
    "done",
    "failed",
    "cancelled",
)
PENDING_STATES = ("waiting_input", "waiting_approval", "paused_budget")
TERMINAL_STATES = ("done", "failed", "cancelled")

#: States in which a task is still alive (cancellable, not collectable).
ACTIVE_STATES = ("queued", "starting", "running", "waiting_dependencies")


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
    #: Human pre-approval of ask-gated tools, granted at submission
    #: (ADR 0011): unattended runs deny `tools_ask` tools unless set.
    allow_asks: bool = False
    #: Immutable, nonsecret execution context. Empty denotes a legacy job whose
    #: project identity is unknown; the runner must not guess it.
    manifest: dict = field(default_factory=dict)
    pending_reason: str = ""
    thread_id: str = ""
    checkpoint: dict = field(default_factory=dict)
    parent_task_id: str = ""
    dependencies: list[str] = field(default_factory=list)
    attempt: int = 1
    structured_result: dict = field(default_factory=dict)
    integration_state: str = "unreviewed"
    execution_kind: str = "detached"
    max_concurrency: int = 3


@dataclass(frozen=True)
class InboxMessage:
    """One steering message queued for a running task (ADR 0011)."""

    id: int
    task_id: str
    message: str
    ts: float
    consumed_at: float | None = None


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

CREATE TABLE IF NOT EXISTS task_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    message TEXT NOT NULL,
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_inbox_task ON task_inbox(task_id, consumed_at);
"""

#: Columns added after the initial schema shipped; applied as guarded ALTERs
#: so existing databases migrate in place.
_MIGRATIONS = (
    "ALTER TABLE tasks ADD COLUMN allow_asks INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE tasks ADD COLUMN pending_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN checkpoint_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE tasks ADD COLUMN structured_result_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE tasks ADD COLUMN integration_state TEXT NOT NULL DEFAULT 'unreviewed'",
    "ALTER TABLE tasks ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'detached'",
    "ALTER TABLE tasks ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 3",
)


def tasks_db_path(path: Path | None = None) -> Path:
    return path or (
        Path(os.environ["MANTA_TASK_DB_PATH"])
        if os.environ.get("MANTA_TASK_DB_PATH")
        else user_manta_dir() / TASKS_DB_PATH
    )


def task_log_dir(path: Path | None = None) -> Path:
    return path or (user_manta_dir() / TASK_LOG_DIR)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and migrate) the task store, returning a connection."""
    db_path = tasks_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(_SCHEMA)
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
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
        allow_asks=bool(row["allow_asks"]) if "allow_asks" in row.keys() else False,
        manifest=json.loads(row["manifest_json"]),
        pending_reason=row["pending_reason"],
        thread_id=row["thread_id"],
        checkpoint=json.loads(row["checkpoint_json"]),
        parent_task_id=row["parent_task_id"],
        dependencies=json.loads(row["dependencies_json"]),
        attempt=row["attempt"],
        structured_result=json.loads(row["structured_result_json"]),
        integration_state=row["integration_state"],
        execution_kind=row["execution_kind"],
        max_concurrency=row["max_concurrency"],
    )


def create_task(record: TaskRecord, *, path: Path | None = None) -> TaskRecord:
    """Insert a new task row."""
    conn = connect(path)
    try:
        if record.state not in STATES:
            raise ValueError(f"invalid task state: {record.state!r}")
        if record.execution_kind not in {"detached", "inline"}:
            raise ValueError("execution_kind must be detached or inline")
        if not 1 <= record.max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
        conn.execute("BEGIN IMMEDIATE")
        _validate_links(conn, record.id, record.parent_task_id, record.dependencies)
        values = {
            "id": record.id,
            "agent": record.agent,
            "prompt": record.prompt,
            "state": record.state,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "pid": record.pid,
            "exit_code": record.exit_code,
            "log_path": record.log_path,
            "result": record.result,
            "timeout": record.timeout,
            "max_turns": record.max_turns,
            "allow_asks": int(record.allow_asks),
            "manifest_json": json.dumps(record.manifest, sort_keys=True),
            "pending_reason": record.pending_reason,
            "thread_id": record.thread_id,
            "checkpoint_json": json.dumps(record.checkpoint),
            "parent_task_id": record.parent_task_id,
            "dependencies_json": json.dumps(record.dependencies),
            "attempt": record.attempt,
            "structured_result_json": json.dumps(record.structured_result),
            "integration_state": record.integration_state,
            "execution_kind": record.execution_kind,
            "max_concurrency": record.max_concurrency,
        }
        columns = ",".join(values)
        marks = ",".join("?" for _ in values)
        conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({marks})", list(values.values()))  # noqa: S608
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
    expected_pending_id: str | None = None,
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
        "pending_reason",
        "thread_id",
        "checkpoint",
        "attempt",
        "structured_result",
        "integration_state",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown task fields: {sorted(unknown)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"invalid task state: {fields['state']!r}")
    if not fields:
        return False
    if "integration_state" in fields and fields["integration_state"] not in {
        "unreviewed",
        "integrated",
        "discarded",
    }:
        raise ValueError("invalid integration state")
    for name in ("checkpoint", "structured_result"):
        if name in fields:
            fields[name + "_json"] = json.dumps(fields.pop(name))
    sets = ", ".join(f"{name} = ?" for name in fields)
    values: list[object] = list(fields.values())
    where = "id = ?"
    values.append(task_id)
    if expect_state is not None:
        where += " AND state = ?"
        values.append(expect_state)
    conn = connect(path)
    try:
        if expected_pending_id is not None:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT checkpoint_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None or json.loads(row[0]).get("pending_id") != expected_pending_id:
                return False
        cursor = conn.execute(f"UPDATE tasks SET {sets} WHERE {where}", values)  # noqa: S608 - column names validated against allow-list
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def pause_task(
    task_id: str,
    reason: str,
    *,
    thread_id: str,
    checkpoint: dict | None = None,
    path: Path | None = None,
    state: str = "paused_budget",
) -> bool:
    """Persist a budget interrupt before the headless client exits.

    The checkpoint contains nonsecret resume/checkpoint identifiers, never a
    transcript or credentials. A concurrent cancellation wins.
    """
    if state not in PENDING_STATES:
        raise ValueError("invalid pending state")
    if not thread_id:
        raise ValueError("a persistent thread id is required for task pause")
    return update_task(
        task_id,
        state=state,
        pending_reason=reason,
        thread_id=thread_id,
        checkpoint=checkpoint or {},
        expect_state="running",
        path=path,
    )


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


# --- task inbox (steerable tasks, ADR 0011) ----------------------------------


def add_inbox_message(task_id: str, message: str, *, path: Path | None = None) -> InboxMessage:
    """Queue a steering message for a task."""
    conn = connect(path)
    try:
        ts = time.time()
        cursor = conn.execute(
            "INSERT INTO task_inbox (task_id, ts, message) VALUES (?,?,?)",
            (task_id, ts, message),
        )
        conn.commit()
        return InboxMessage(id=int(cursor.lastrowid), task_id=task_id, message=message, ts=ts)
    finally:
        conn.close()


def unconsumed_inbox(task_id: str, *, path: Path | None = None) -> list[InboxMessage]:
    """Steering messages not yet delivered to the task, oldest first."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM task_inbox WHERE task_id = ? AND consumed_at IS NULL ORDER BY ts ASC",
            (task_id,),
        ).fetchall()
        return [
            InboxMessage(id=r["id"], task_id=r["task_id"], message=r["message"], ts=r["ts"])
            for r in rows
        ]
    finally:
        conn.close()


def mark_inbox_consumed(ids: list[int], *, path: Path | None = None) -> None:
    """Mark specific inbox rows consumed (per-row, never blanket: a message
    inserted between read and mark must wait for the next delivery, not be
    silently swallowed)."""
    if not ids:
        return
    conn = connect(path)
    try:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE task_inbox SET consumed_at = ? WHERE id IN ({placeholders})",  # noqa: S608 - placeholders only
            [time.time(), *ids],
        )
        conn.commit()
    finally:
        conn.close()


def inbox_count(task_id: str, *, path: Path | None = None) -> int:
    """Total steering messages ever sent to a task."""
    conn = connect(path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM task_inbox WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(count)
    finally:
        conn.close()


def _validate_links(
    conn: sqlite3.Connection, task_id: str, parent: str, dependencies: list[str]
) -> None:
    """Validate the entire dependency graph under the caller's write lock."""
    if task_id in dependencies or parent == task_id:
        raise ValueError("a task cannot depend on or parent itself")
    if len(dependencies) != len(set(dependencies)):
        raise ValueError("duplicate task dependencies")
    rows = conn.execute("SELECT id, dependencies_json FROM tasks").fetchall()
    graph = {row["id"]: json.loads(row["dependencies_json"]) for row in rows}
    if any(link not in graph for link in [*dependencies, *([parent] if parent else [])]):
        raise ValueError("task parent and dependencies must already exist")
    graph[task_id] = dependencies
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("task dependencies cannot form a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def set_dependencies(task_id: str, dependencies: list[str], *, path: Path | None = None) -> None:
    """Change only unstarted dependencies, atomically rejecting cycles."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["state"] not in {"queued", "waiting_dependencies"} or row["pid"]:
            raise ValueError("only unstarted tasks can change dependencies")
        _validate_links(conn, task_id, row["parent_task_id"], dependencies)
        conn.execute(
            "UPDATE tasks SET dependencies_json=?,state='queued',pending_reason='' WHERE id=?",
            (json.dumps(dependencies), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def claim_ready_task(task_id: str, *, concurrency_limit: int = 3, path: Path | None = None) -> bool:
    """Reserve one process slot and dependency decision in a single transaction."""
    if not 1 <= concurrency_limit <= 16:
        raise ValueError("concurrency limit must be between 1 and 16")
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (
            row is None
            or row["state"] not in {"queued", "waiting_dependencies"}
            or row["execution_kind"] != "detached"
        ):
            return False
        deps = json.loads(row["dependencies_json"])
        for dependency in deps:
            dep = conn.execute("SELECT state FROM tasks WHERE id=?", (dependency,)).fetchone()
            if dep is None or dep["state"] in {"failed", "cancelled", "blocked", "interrupted"}:
                conn.execute(
                    "UPDATE tasks SET state='blocked',pending_reason=? WHERE id=?",
                    (f"Dependency {dependency} needs attention", task_id),
                )
                conn.commit()
                return False
            if dep["state"] != "done":
                conn.execute(
                    "UPDATE tasks SET state='waiting_dependencies',pending_reason=? WHERE id=?",
                    (f"Waiting for dependency {dependency}", task_id),
                )
                conn.commit()
                return False
        active = conn.execute(
            "SELECT max_concurrency FROM tasks WHERE state IN ('starting','running') AND execution_kind='detached'"
        ).fetchall()
        limit = min(
            concurrency_limit, row["max_concurrency"], *(r["max_concurrency"] for r in active)
        )
        if len(active) >= limit:
            return False
        conn.execute(
            "UPDATE tasks SET state='starting',pending_reason='',started_at=? WHERE id=?",
            (time.time(), task_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def record_test_result(
    task_id: str, command: str, exit_code: int, output: str = "", *, path: Path | None = None
) -> None:
    """Append an observed command receipt; output is bounded and local only."""
    _append_evidence(
        task_id,
        "tests",
        {
            "command": command,
            "exit_code": exit_code,
            "output": output[-4000:],
            "observed_at": time.time(),
        },
        path=path,
    )


def record_artifact(
    task_id: str, artifact_path: str, label: str = "", *, path: Path | None = None
) -> None:
    _append_evidence(task_id, "artifacts", {"path": artifact_path, "label": label}, path=path)


def _append_evidence(task_id: str, key: str, value: dict, *, path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT structured_result_json,attempt FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no task '{task_id}'")
        evidence = json.loads(row[0])
        value = {**value, "attempt": row["attempt"]}
        evidence.setdefault(key, []).append(value)
        conn.execute(
            "UPDATE tasks SET structured_result_json=? WHERE id=?", (json.dumps(evidence), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def save_result_snapshot(task_id: str, snapshot: dict, *, expect_state: str, path: Path | None = None) -> bool:
    """Merge an inspection without losing receipts concurrently appended by tools."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT state,structured_result_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["state"] != expect_state:
            return False
        latest = json.loads(row["structured_result_json"])
        merged = dict(snapshot)
        for key in ("tests", "artifacts"):
            entries = list(latest.get(key, []))
            for entry in snapshot.get(key, []):
                if entry not in entries:
                    entries.append(entry)
            merged[key] = entries
        conn.execute("UPDATE tasks SET structured_result_json=? WHERE id=?", (json.dumps(merged), task_id))
        conn.commit()
        return True
    finally:
        conn.close()


def descendant_task_ids(task_id: str, *, path: Path | None = None) -> list[str]:
    """Return the durable family including its root, with cycle-safe SQL."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "WITH RECURSIVE family(id) AS (SELECT ? UNION SELECT tasks.id FROM tasks JOIN family ON tasks.parent_task_id=family.id) SELECT id FROM family",
            (task_id,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()
