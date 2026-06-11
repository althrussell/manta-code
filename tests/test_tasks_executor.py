from __future__ import annotations

import pytest

from manta_code.tasks import executor, store


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


class _FakeProcess:
    def __init__(self):
        self.pid = 9999


@pytest.fixture()
def _fake_spawn(monkeypatch):
    spawned = {}

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    return spawned


def test_submit_creates_task_and_spawns_detached_runner(_fake_spawn):
    record = executor.submit_task("swe", "fix the flaky test")
    assert record.state == "queued"
    assert record.pid == 9999

    argv = _fake_spawn["argv"]
    assert argv[1:3] == ["-m", "manta_code.tasks.runner"]
    assert argv[3] == record.id
    kwargs = _fake_spawn["kwargs"]
    assert kwargs["start_new_session"] is True  # detached: survives the session
    assert kwargs["env"][executor.TASK_ID_ENV] == record.id

    stored = store.get_task(record.id)
    assert stored.pid == 9999
    assert stored.timeout == executor.DEFAULT_TASK_TIMEOUT
    assert stored.max_turns == executor.DEFAULT_TASK_MAX_TURNS
    # Submission is audited as an event.
    kinds = {e.kind for e in store.recent_events(task_id=record.id)}
    assert "task_submitted" in kinds


def test_submit_accepts_at_prefixed_agent(_fake_spawn):
    record = executor.submit_task("@review", "look at the diff")
    assert record.agent == "review"


def test_submit_rejects_unknown_agent(_fake_spawn):
    with pytest.raises(executor.TaskError, match="no Manta agent named 'nope'"):
        executor.submit_task("nope", "anything")


def test_submit_rejects_empty_prompt(_fake_spawn):
    with pytest.raises(executor.TaskError, match="non-empty"):
        executor.submit_task("swe", "   ")


def test_cancel_running_task_signals_group(monkeypatch, _fake_spawn):
    record = executor.submit_task("swe", "long thing")
    store.update_task(record.id, state="running")

    killed = {}
    monkeypatch.setattr(
        executor.os, "killpg", lambda pid, sig: killed.update(pid=pid, sig=sig)
    )
    cancelled = executor.cancel_task(record.id)
    assert cancelled.state == "cancelled"
    assert killed["pid"] == 9999
    assert store.get_task(record.id).state == "cancelled"


def test_cancel_finished_task_errors(_fake_spawn):
    record = executor.submit_task("swe", "quick thing")
    store.update_task(record.id, state="done")
    with pytest.raises(executor.TaskError, match="already done"):
        executor.cancel_task(record.id)


def test_cancel_survives_already_dead_process(monkeypatch, _fake_spawn):
    record = executor.submit_task("swe", "thing")

    def _gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(executor.os, "killpg", _gone)
    cancelled = executor.cancel_task(record.id)
    assert cancelled.state == "cancelled"


def test_task_output_prefers_result_then_log(tmp_path, _fake_spawn):
    record = executor.submit_task("swe", "thing")
    # While running with no result: log tail.
    log = tmp_path / "task.log"
    log.write_text("streamed output so far", encoding="utf-8")
    store.update_task(record.id, log_path=str(log))
    assert executor.task_output(record.id) == "streamed output so far"
    # Once a result is recorded, it wins.
    store.update_task(record.id, result="the final answer")
    assert executor.task_output(record.id) == "the final answer"


def test_reconcile_marks_dead_runner_failed(monkeypatch, _fake_spawn):
    record = executor.submit_task("swe", "long thing")
    store.update_task(record.id, state="running", pid=999999)
    monkeypatch.setattr(executor, "RECONCILE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(executor, "_pid_alive", lambda pid: False)
    (repaired,) = executor.reconcile_stale_tasks()
    assert repaired.id == record.id
    assert repaired.state == "failed"
    assert "runner process died" in repaired.result


def test_reconcile_leaves_live_runner_alone(monkeypatch, _fake_spawn):
    record = executor.submit_task("swe", "long thing")
    store.update_task(record.id, state="running", pid=999999)
    monkeypatch.setattr(executor, "RECONCILE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(executor, "_pid_alive", lambda pid: True)

    assert executor.reconcile_stale_tasks() == []

    # And fresh submissions are protected by the grace window even if their
    # pid cannot be probed.
    monkeypatch.setattr(executor, "RECONCILE_GRACE_SECONDS", 30.0)
    monkeypatch.setattr(executor, "_pid_alive", lambda pid: False)
    assert executor.reconcile_stale_tasks() == []
    assert store.get_task(record.id).state == "running"


def test_submit_refuses_runaway_nesting(monkeypatch, _fake_spawn):
    # Depth 0 (interactive) and 1 (inside a task) may submit; depth 2 may not.
    monkeypatch.setenv(executor.TASK_DEPTH_ENV, "1")
    record = executor.submit_task("swe", "nested once is fine")
    assert _fake_spawn["kwargs"]["env"][executor.TASK_DEPTH_ENV] == "2"
    assert record.state == "queued"
    monkeypatch.setenv(executor.TASK_DEPTH_ENV, "2")
    with pytest.raises(executor.TaskError, match="nesting limit"):
        executor.submit_task("swe", "too deep")


def test_submit_spawn_failure_marks_task_failed(monkeypatch):
    def _no_spawn(*a, **k):
        raise OSError("fork failed")

    monkeypatch.setattr(executor.subprocess, "Popen", _no_spawn)
    with pytest.raises(executor.TaskError, match="could not start"):
        executor.submit_task("swe", "doomed")
    (task,) = store.list_tasks(limit=1)
    assert task.state == "failed"  # never stranded in queued
    assert "failed to spawn" in task.result
