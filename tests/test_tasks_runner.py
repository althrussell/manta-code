from __future__ import annotations

import pytest

from manta_code.tasks import runner, store


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


def _queued_task(tmp_path, prompt="do it") -> store.TaskRecord:
    log = tmp_path / "out.log"
    return store.create_task(
        store.TaskRecord(
            id=store.new_task_id(),
            agent="swe",
            prompt=prompt,
            log_path=str(log),
            timeout=120,
            max_turns=5,
        )
    )


def test_run_task_success_records_done(tmp_path, monkeypatch):
    record = _queued_task(tmp_path)

    seen = {}

    def fake_run_headless(**kwargs):
        seen.update(kwargs)
        # Simulate the headless agent writing its answer to our (inherited) log.
        from pathlib import Path

        Path(record.log_path).write_text("the agent's final answer\n", encoding="utf-8")
        return 0

    from manta_code import dcode

    monkeypatch.setattr(dcode, "run_headless", fake_run_headless)
    exit_code = runner.run_task(record.id)

    assert exit_code == 0
    loaded = store.get_task(record.id)
    assert loaded.state == "done"
    assert loaded.exit_code == 0
    assert "final answer" in loaded.result
    # The runner drove the headless path *as the addressed agent*.
    assert seen["passthrough"] == ["-a", "swe"]
    assert seen["message"] == "do it"
    assert seen["timeout"] == 120
    assert seen["max_turns"] == 5
    kinds = [e.kind for e in store.recent_events(task_id=record.id)]
    assert "task_started" in kinds
    assert "task_done" in kinds


def test_run_task_failure_records_failed(tmp_path, monkeypatch):
    record = _queued_task(tmp_path)
    from manta_code import dcode

    monkeypatch.setattr(dcode, "run_headless", lambda **kw: 1)
    assert runner.run_task(record.id) == 1
    loaded = store.get_task(record.id)
    assert loaded.state == "failed"
    assert loaded.exit_code == 1


def test_run_task_exception_records_failed(tmp_path, monkeypatch):
    record = _queued_task(tmp_path)
    from manta_code import dcode

    def _boom(**kw):
        raise RuntimeError("launcher exploded")

    monkeypatch.setattr(dcode, "run_headless", _boom)
    assert runner.run_task(record.id) == 1
    assert store.get_task(record.id).state == "failed"


def test_run_task_does_not_overwrite_cancellation(tmp_path, monkeypatch):
    record = _queued_task(tmp_path)
    from manta_code import dcode

    def fake_run_headless(**kwargs):
        # A cancel lands while the agent is running.
        store.update_task(record.id, state="cancelled")
        return 0

    monkeypatch.setattr(dcode, "run_headless", fake_run_headless)
    runner.run_task(record.id)
    assert store.get_task(record.id).state == "cancelled"


def test_run_task_requires_queued_state(tmp_path):
    record = _queued_task(tmp_path)
    store.update_task(record.id, state="running")
    assert runner.run_task(record.id) == 2


def test_run_task_unknown_id():
    assert runner.run_task("missing1") == 2


def test_runner_recovers_from_deleted_cwd(tmp_path, monkeypatch):
    # A task submitted from inside a session can inherit an ephemeral cwd that
    # is deleted before the runner boots; it must recover, not crash.
    import subprocess
    import sys

    doomed = tmp_path / "ephemeral"
    doomed.mkdir()
    code = (
        "import os, sys\n"
        f"os.chdir({str(doomed)!r})\n"
        f"__import__('shutil').rmtree({str(doomed)!r})\n"
        "from manta_code.tasks.runner import _ensure_valid_cwd\n"
        "_ensure_valid_cwd()\n"
        "print(os.getcwd())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip()  # a valid cwd again
