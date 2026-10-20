from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from manta_code.tasks import executor, inline, isolation, scheduler, store
from manta_code.tasks.scheduler import ensure_scheduler
from manta_code.tasks.context import (
    TaskContextError,
    execution_context,
    validate_execution_manifest,
)


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    home = tmp_path / "state"
    monkeypatch.setenv("MANTA_HOME", str(home))
    config = tmp_path / "databricks.ini"
    config.write_text("[DEFAULT]\nhost=https://workspace.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    for key in (
        "MANTA_TASK_CONFIG_ROOT",
        "MANTA_TASK_DB_PATH",
        "MANTA_TASK_ID",
        "MANTA_TASK_DEPTH",
        "MANTA_PROJECT_ROOT",
        "MANTA_PROFILE",
        "DATABRICKS_HOST",
        "DATABRICKS_CONFIG_PROFILE",
        "DEEPAGENTS_CODE_SERVER_CWD",
        "DEEPAGENTS_CODE_SERVER_PROJECT_ROOT",
        "MANTA_ORIGINAL_CWD",
    ):
        monkeypatch.delenv(key, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(executor, "_spawn_task", lambda record, **kwargs: record)
    monkeypatch.setattr("manta_code.tasks.scheduler.ensure_scheduler", lambda **kwargs: None)
    return project


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def repository(project):
    git(project, "init", "-q")
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.com")
    (project / "main.py").write_text("answer = 41\n")
    (project / "other.txt").write_text("original\n")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial")
    return project


def complete(task):
    store.update_task(
        task.id, state="done", pid=None, result="Changed the answer", expect_state="starting"
    )


def test_git_task_isolated_review_integrate_preserves_staged_unstaged_and_untracked(environment):
    project = repository(environment)
    (project / "other.txt").write_text("caller staged change\n")
    git(project, "add", "other.txt")
    (project / "other.txt").write_text("caller staged and unstaged change\n")
    (project / "private.txt").write_text("caller untracked\n")
    original_index = git(project, "diff", "--cached")
    task = executor.submit_task("swe", "fix answer")
    work = Path(task.manifest["execution_root"])
    assert task.manifest["source_dirty"] is True
    assert (work / "other.txt").read_text() == "original\n"
    assert work != project
    (work / "main.py").write_text("answer = 42\n")
    (work / "new.txt").write_text("task file\n")
    complete(task)
    review = executor.review_task(task.id)
    assert review["changed_files"] == ["main.py", "new.txt"]
    assert "answer = 42" in review["diff"]
    assert not review["spend"]["cost_complete"]
    assert executor.integrate_task(task.id).integration_state == "integrated"
    assert (project / "main.py").read_text() == "answer = 42\n"
    assert (project / "other.txt").read_text() == "caller staged and unstaged change\n"
    assert (project / "private.txt").read_text() == "caller untracked\n"
    assert git(project, "diff", "--cached") == original_index
    assert (project / "new.txt").read_text() == "task file\n"


def test_git_conflict_is_atomic_and_can_discard_without_source_changes(environment):
    project = repository(environment)
    task = executor.submit_task("swe", "fix answer")
    work = Path(task.manifest["execution_root"])
    (work / "main.py").write_text("answer = 42\n")
    (work / "other.txt").write_text("task change\n")
    complete(task)
    executor.review_task(task.id)
    (project / "main.py").write_text("caller conflict\n")
    with pytest.raises(executor.TaskError, match="Integration blocked"):
        executor.integrate_task(task.id)
    assert (project / "other.txt").read_text() == "original\n"
    assert (project / "main.py").read_text() == "caller conflict\n"
    discarded = executor.discard_task(task.id)
    assert discarded.integration_state == "discarded"
    assert not work.exists()
    assert "answer = 42" in executor.review_task(task.id)["diff"]
    assert (project / "main.py").read_text() == "caller conflict\n"


def test_git_committed_task_changes_and_binary_new_file_are_reviewed(environment):
    project = repository(environment)
    task = executor.submit_task("swe", "fix answer")
    work = Path(task.manifest["execution_root"])
    (work / "main.py").write_text("answer = 42\n")
    git(work, "commit", "-qam", "agent fix")
    (work / "blob.bin").write_bytes(b"\x00\xff\x10")
    validate_execution_manifest(task.manifest)
    complete(task)
    review = executor.review_task(task.id)
    assert review["changed_files"] == ["blob.bin", "main.py"]
    executor.integrate_task(task.id)
    assert (project / "blob.bin").read_bytes() == b"\x00\xff\x10"


def test_review_required_and_post_review_modification_rejected(environment):
    repository(environment)
    task = executor.submit_task("swe", "fix")
    work = Path(task.manifest["execution_root"])
    (work / "main.py").write_text("answer = 42\n")
    complete(task)
    with pytest.raises(executor.TaskError, match="Review"):
        executor.integrate_task(task.id)
    executor.review_task(task.id)
    (work / "main.py").write_text("answer = 43\n")
    with pytest.raises(executor.TaskError, match="changed after review"):
        executor.integrate_task(task.id)
    assert (environment / "main.py").read_text() == "answer = 41\n"


def test_non_git_snapshot_integrate_deletion_and_new_binary_preserves_unrelated(environment):
    (environment / "old.txt").write_text("old")
    (environment / "other.txt").write_text("other")
    task = executor.submit_task("swe", "change")
    work = Path(task.manifest["execution_root"])
    assert task.manifest["isolation"] == "directory"
    (work / "old.txt").unlink()
    (work / "new.bin").write_bytes(b"\0binary")
    (environment / "other.txt").write_text("user change")
    complete(task)
    executor.review_task(task.id)
    executor.integrate_task(task.id)
    assert not (environment / "old.txt").exists()
    assert (environment / "new.bin").read_bytes() == b"\0binary"
    assert (environment / "other.txt").read_text() == "user change"


def test_non_git_conflict_and_symlink_guard(environment, tmp_path):
    (environment / "a.txt").write_text("base")
    task = executor.submit_task("swe", "change")
    work = Path(task.manifest["execution_root"])
    (work / "a.txt").write_text("task")
    complete(task)
    executor.review_task(task.id)
    (environment / "a.txt").write_text("caller")
    with pytest.raises(executor.TaskError, match="changed since submission"):
        executor.integrate_task(task.id)
    (environment / "a.txt").write_text("base")
    (environment / "a.txt").unlink()
    outside = tmp_path / "outside"
    outside.write_text("base")
    (environment / "a.txt").symlink_to(outside)
    with pytest.raises(executor.TaskError, match="symbolic link"):
        executor.integrate_task(task.id)
    assert outside.read_text() == "base"


@pytest.mark.parametrize("directory_to_file", [False, True])
def test_non_git_structural_replacement_is_rejected_before_any_source_write(
    environment, monkeypatch, directory_to_file,
):
    (environment / "a.txt").write_text("keep original")
    replaced = environment / "replaced"
    if directory_to_file:
        replaced.mkdir()
        (replaced / "child.txt").write_text("keep child")
    else:
        replaced.write_text("keep original file")
    task = executor.submit_task("swe", "change paths")
    work = Path(task.manifest["execution_root"])
    (work / "a.txt").write_text("proposed edit")
    if directory_to_file:
        (work / "replaced" / "child.txt").unlink()
        (work / "replaced").rmdir()
        (work / "replaced").write_text("replacement file")
    else:
        (work / "replaced").unlink()
        (work / "replaced").mkdir()
        (work / "replaced" / "child.txt").write_text("replacement child")
    complete(task)
    executor.review_task(task.id)

    def forbidden_copy(*_args, **_kwargs):
        pytest.fail("Structural replacement must be rejected before the first source write")

    monkeypatch.setattr(isolation.shutil, "copy2", forbidden_copy)
    with pytest.raises(executor.TaskError, match="file/directory replacements manually"):
        executor.integrate_task(task.id)
    assert (environment / "a.txt").read_text() == "keep original"
    if directory_to_file:
        assert (replaced / "child.txt").read_text() == "keep child"
    else:
        assert replaced.read_text() == "keep original file"
    assert store.get_task(task.id).integration_state == "unreviewed"


def test_task_context_enters_isolated_subdirectory_and_restores_environment(
    environment, monkeypatch
):
    (environment / "sub").mkdir()
    monkeypatch.chdir(environment / "sub")
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_PROJECT_ROOT", str(environment))
    task = executor.submit_task("swe", "change")
    with execution_context(task.manifest):
        assert Path.cwd() == Path(task.manifest["execution_root"]) / "sub"
        assert os.environ["DEEPAGENTS_CODE_SERVER_PROJECT_ROOT"] == task.manifest["execution_root"]
    assert Path.cwd() == environment / "sub"
    assert os.environ["DEEPAGENTS_CODE_SERVER_PROJECT_ROOT"] == str(environment)


def test_concurrent_claims_obey_global_cap_and_pending_releases_slot():
    records = [
        store.create_task(
            store.TaskRecord(id=f"task{i}", agent="swe", prompt="work", max_concurrency=2)
        )
        for i in range(8)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(lambda task: store.claim_ready_task(task.id, concurrency_limit=2), records)
        )
    assert sum(outcomes) == 2
    running = next(record for record in store.list_tasks() if record.state == "starting")
    store.update_task(running.id, state="waiting_approval")
    assert len(executor.schedule_tasks(concurrency_limit=2)) == 1
    assert len([record for record in store.list_tasks() if record.state == "starting"]) == 2


def test_reconciliation_finds_old_dead_worker_behind_newer_finished_history(monkeypatch):
    import time

    stale = store.create_task(store.TaskRecord(
        id="old-worker", agent="swe", prompt="Old attempt", state="running",
        created_at=1, started_at=time.time() - 100, pid=987654, max_concurrency=1,
    ))
    for index in range(105):
        store.create_task(store.TaskRecord(id=f"finished-{index}", agent="review", prompt="History", state="done"))
    queued = store.create_task(store.TaskRecord(id="ready", agent="swe", prompt="New work", max_concurrency=1))
    assert not store.claim_ready_task(queued.id, concurrency_limit=1)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    assert [record.id for record in executor.reconcile_stale_tasks()] == [stale.id]
    assert store.get_task(stale.id).state == "interrupted"
    assert store.claim_ready_task(queued.id, concurrency_limit=1)


def test_dependency_wait_failure_cycle_and_restart_from_sqlite():
    a = store.create_task(store.TaskRecord(id="a", agent="swe", prompt="A"))
    b = store.create_task(
        store.TaskRecord(
            id="b", agent="review", prompt="B", dependencies=[a.id], parent_task_id=a.id
        )
    )
    assert not store.claim_ready_task(b.id)
    assert store.get_task(b.id).state == "waiting_dependencies"
    with pytest.raises(ValueError, match="cycle"):
        store.set_dependencies(a.id, [b.id])
    store.update_task(a.id, state="done")
    assert store.claim_ready_task(b.id)
    loaded = store.get_task(b.id)
    assert loaded.parent_task_id == a.id
    assert loaded.dependencies == [a.id]
    c = store.create_task(store.TaskRecord(id="c", agent="swe", prompt="C", dependencies=[b.id]))
    store.update_task(b.id, state="failed")
    assert not store.claim_ready_task(c.id)
    assert store.get_task(c.id).state == "blocked"


def test_inline_tasks_never_dispatched_and_unknown_dependencies_rejected():
    task = store.create_task(
        store.TaskRecord(id="inline", agent="swe", prompt="A", execution_kind="inline")
    )
    assert not store.claim_ready_task(task.id)
    with pytest.raises(ValueError, match="already exist"):
        store.create_task(
            store.TaskRecord(id="unknown", agent="swe", prompt="A", dependencies=["absent"])
        )


def test_fork_requires_snapshot_and_parent_links_bound_depth(environment):
    with pytest.raises(executor.TaskError, match="snapshot"):
        executor.submit_task("swe", "task", context_mode="fork")
    parent = executor.submit_task(
        "swe",
        "task",
        context_mode="fork",
        conversation_context="User: preserve API",
        parent_thread_id="session1",
    )
    assert "preserve API" in parent.prompt
    assert parent.manifest["parent_thread_id"] == "session1"
    child = executor.submit_task("review", "review", parent_task_id=parent.id)
    assert child.parent_task_id == parent.id
    with pytest.raises(executor.TaskError, match="nesting limit"):
        executor.submit_task("review", "too deep", parent_task_id=child.id)


def test_pending_decision_compare_and_set_rejects_old_identity():
    task = store.create_task(
        store.TaskRecord(
            id="pause",
            agent="swe",
            prompt="A",
            state="waiting_approval",
            checkpoint={"pending_id": "new"},
        )
    )
    assert not store.update_task(
        task.id, state="queued", expected_pending_id="old", expect_state="waiting_approval"
    )
    assert store.get_task(task.id).state == "waiting_approval"
    assert store.update_task(
        task.id, state="queued", expected_pending_id="new", expect_state="waiting_approval"
    )


def test_observed_receipts_append_without_lost_updates():
    store.create_task(store.TaskRecord(id="receipt", agent="swe", prompt="A"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda i: store.record_test_result("receipt", f"pytest case{i}", i % 2, "observed"),
                range(8),
            )
        )
    store.record_artifact("receipt", "/tmp/report.txt", "report")
    result = store.get_task("receipt").structured_result
    assert len(result["tests"]) == 8
    assert {r["exit_code"] for r in result["tests"]} == {0, 1}
    assert result["artifacts"][0]["label"] == "report"


def test_resume_requires_explicit_non_budget_decision_and_exact_pending_identity(
    environment, monkeypatch
):
    task = executor.submit_task("swe", "task")
    store.update_task(
        task.id,
        state="waiting_approval",
        thread_id="thread",
        checkpoint={"pending_id": "new", "interrupts": {"i": {"action_requests": []}}},
    )
    with pytest.raises(executor.TaskError, match="explicit decision"):
        executor.resume_task(task.id)
    with pytest.raises(executor.TaskError, match="changed while resuming"):
        executor.resume_task(
            task.id,
            resume_value={"i": {"decisions": [{"type": "approve"}]}},
            expected_pending_id="old",
        )
    assert store.get_task(task.id).state == "waiting_approval"


def test_resume_with_busy_slots_preserves_the_stopped_attempt_evidence(environment):
    task = executor.submit_task("swe", "task", max_concurrency=1)
    store.update_task(
        task.id, state="interrupted", thread_id="thread", result="Stopped before decision",
        pending_reason="Runner stopped", finished_at=123.0, exit_code=7,
        checkpoint={"pending_id": "current", "interrupts": {"i": {"action_requests": []}}},
    )
    executor.submit_task("swe", "occupy the process slot", max_concurrency=1)
    with pytest.raises(executor.TaskError, match="slots are busy"):
        executor.resume_task(
            task.id, resume_value={"i": {"decisions": [{"type": "approve"}]}},
            expected_pending_id="current",
        )
    current = store.get_task(task.id)
    assert current.state == "interrupted"
    assert current.result == "Stopped before decision" and current.pending_reason == "Runner stopped"
    assert current.finished_at == 123.0 and current.exit_code == 7
    assert current.checkpoint["pending_id"] == "current"


def test_isolated_runtime_uses_reviewed_ignored_source_config_and_blocks_drift(environment):
    project = repository(environment)
    (project / ".gitignore").write_text(".manta/\n")
    (project / ".manta").mkdir()
    config = project / ".manta" / "config.toml"
    config.write_text(
        '[interactive]\ndefault_endpoint = "system.ai.gpt-6-1-sol"\n[databricks]\ndiscovery_timeout_seconds = 9\n'
    )
    task = executor.submit_task("swe", "change")
    assert not (Path(task.manifest["execution_root"]) / ".manta" / "config.toml").exists()
    from manta_code.config import load_config, project_manta_dir

    with execution_context(task.manifest):
        assert load_config().databricks.discovery_timeout_seconds == 9
        assert project_manta_dir().parent == Path(task.manifest["execution_root"])
    config.write_text('[interactive]\ndefault_endpoint = "system.ai.gpt-6-luna"\n')
    with pytest.raises(TaskContextError, match="runtime configuration changed"):
        validate_execution_manifest(task.manifest)


def test_runner_captures_actual_isolated_changes_and_receipts(environment, monkeypatch):
    from manta_code import dcode
    from manta_code.tasks import runner
    (environment / "a.txt").write_text("source")
    task = executor.submit_task("swe", "update and test")
    def run(**kwargs):
        assert Path.cwd() == Path(task.manifest["execution_root"])
        (Path.cwd() / "a.txt").write_text("agent change")
        store.record_test_result(task.id, "python -m pytest", 0, "1 passed")
        Path(task.log_path).write_text("Verified the change\n")
        return 0
    monkeypatch.setattr(dcode, "run_headless", run)
    assert runner.run_task(task.id) == 0
    result = store.get_task(task.id).structured_result
    assert result["status"] == "done"
    assert result["changed_files"] == ["a.txt"]
    assert result["tests"] == [{"command": "python -m pytest", "exit_code": 0, "output": "1 passed",
                                "observed_at": result["tests"][0]["observed_at"], "attempt": 1}]
    assert (environment / "a.txt").read_text() == "source"
    executor.integrate_task(task.id)
    assert (environment / "a.txt").read_text() == "agent change"


def test_retry_preserves_identity_and_pending_checkpoint_requires_resume(environment):
    task = executor.submit_task("swe", "work")
    store.update_task(task.id, state="failed", pid=None)
    retried = executor.retry_task(task.id)
    assert retried.id == task.id
    assert retried.attempt == 2
    assert retried.manifest == task.manifest
    store.update_task(task.id, state="interrupted", thread_id="thread", checkpoint={"pending_id": "i"})
    with pytest.raises(executor.TaskError, match="checkpoint"):
        executor.retry_task(task.id)


def test_integrated_or_discarded_task_cannot_retry(environment):
    task = executor.submit_task("swe", "work")
    complete(task)
    executor.review_task(task.id)
    executor.integrate_task(task.id)
    store.update_task(task.id, state="failed")
    with pytest.raises(executor.TaskError, match="integrated or discarded"):
        executor.retry_task(task.id)


def test_child_checkout_preserved_until_child_integrated_or_discarded(environment):
    parent = executor.submit_task("swe", "parent")
    child = executor.submit_task("review", "child", parent_task_id=parent.id)
    complete(parent)
    complete(child)
    executor.review_task(parent.id)
    with pytest.raises(executor.TaskError, match="child tasks first"):
        executor.discard_task(parent.id)
    executor.discard_task(child.id)
    executor.discard_task(parent.id)
    assert not Path(parent.manifest["execution_root"]).exists()


def add_newer_task_history(created_after: float, count: int = 10001, state: str = "done"):
    connection = store.connect()
    try:
        connection.executemany(
            "INSERT INTO tasks (id, agent, prompt, state, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (f"history-{index}", "review", "Newer history", state, created_after + index + 1)
                for index in range(count)
            ],
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("action", ["integrate_task", "discard_task"])
def test_old_unreviewed_child_blocks_parent_changes_behind_newer_finished_history(
    environment, action,
):
    parent = executor.submit_task("swe", "parent")
    child = executor.submit_task("review", "child", parent_task_id=parent.id)
    complete(parent)
    complete(child)
    executor.review_task(parent.id)
    add_newer_task_history(max(parent.created_at, child.created_at))
    # Reproduce the pagination boundary using the real durable task store.
    assert child.id not in {record.id for record in store.list_tasks(limit=10000)}
    with pytest.raises(executor.TaskError, match="child tasks first"):
        getattr(executor, action)(parent.id)
    assert Path(parent.manifest["execution_root"]).exists()
    assert Path(child.manifest["execution_root"]).exists()
    assert store.get_task(parent.id).integration_state == "unreviewed"


def test_scheduler_starts_old_queued_task_behind_newer_finished_history(environment):
    queued = store.create_task(store.TaskRecord(id="old-queued", agent="swe", prompt="Work"))
    add_newer_task_history(queued.created_at)
    assert queued.id not in {record.id for record in store.list_tasks(limit=10000)}
    assert [record.id for record in executor.schedule_tasks()] == [queued.id]
    assert store.get_task(queued.id).state == "starting"


def test_supervisor_finds_old_queue_and_stays_active_behind_newer_finished_history(
    environment, monkeypatch,
):
    queued = store.create_task(store.TaskRecord(id="old-queued", agent="swe", prompt="Work"))
    add_newer_task_history(queued.created_at)
    spawned = []
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))
    ensure_scheduler()
    assert len(spawned) == 1
    pumps = []

    def schedule(**_kwargs):
        pumps.append(True)
        if len(pumps) == 2:
            store.update_task(queued.id, state="done")

    monkeypatch.setattr(executor, "schedule_tasks", schedule)
    monkeypatch.setattr(executor, "reconcile_stale_tasks", lambda: [])
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    scheduler.run()
    assert len(pumps) == 2
    assert store.get_task(queued.id).state == "done"


def test_inline_orphan_recovery_finds_old_running_child_behind_newer_running_rows(
    environment, monkeypatch,
):
    orphan = store.create_task(store.TaskRecord(
        id="old-inline", agent="review", prompt="Inspect", state="running", pid=987654,
        thread_id="parent-session", execution_kind="inline",
    ))
    add_newer_task_history(orphan.created_at, count=1001, state="running")
    assert orphan.id not in {record.id for record in store.list_tasks(state="running", limit=1000)}

    def dead_process(pid, _signal):
        assert pid == orphan.pid
        raise ProcessLookupError

    monkeypatch.setattr(inline.os, "kill", dead_process)
    inline.interrupt_orphans()
    restored = store.get_task(orphan.id)
    assert restored.state == "interrupted"
    assert restored.pid is None
    assert "parent-session" in restored.pending_reason
    assert len(store.list_tasks(state="running", limit=-1)) == 1001


def test_snapshot_does_not_erase_concurrently_appended_receipts():
    store.create_task(store.TaskRecord(id="merge", agent="swe", prompt="work"))
    old_snapshot = {"tests": [], "changed_files": ["a.py"]}
    store.record_test_result("merge", "pytest", 0, "1 passed")
    assert store.save_result_snapshot("merge", old_snapshot, expect_state="queued")
    assert store.get_task("merge").structured_result["tests"][0]["command"] == "pytest"


def test_pending_runner_frees_slot_and_unblocks_queue(environment, monkeypatch):
    from manta_code import dcode
    from manta_code.tasks import runner
    first = executor.submit_task("swe", "first", max_concurrency=1)
    second = executor.submit_task("swe", "second", max_concurrency=1)
    assert second.state == "queued"
    def pause(**kwargs):
        store.pause_task(first.id, "Approve", thread_id="thread", state="waiting_approval",
                         checkpoint={"pending_id": "pending", "interrupts": {"i": {"action_requests": []}}})
        return 0
    monkeypatch.setattr(dcode, "run_headless", pause)
    assert runner.run_task(first.id) == 0
    assert store.get_task(first.id).state == "waiting_approval"
    assert store.get_task(first.id).pid is None
    assert store.get_task(second.id).state == "starting"


def test_supervisor_recovers_queue_without_ui(environment, monkeypatch):
    from manta_code.tasks import scheduler
    dependency = store.create_task(store.TaskRecord(id="dep", agent="swe", prompt="dep", state="running", pid=999999))
    queued = store.create_task(store.TaskRecord(id="ready", agent="swe", prompt="ready", dependencies=[dependency.id]))
    ticks = []
    monkeypatch.setattr(executor, "RECONCILE_GRACE_SECONDS", 0)
    monkeypatch.setattr(executor, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(scheduler.time, "sleep", lambda interval: ticks.append(interval))
    scheduler.run()
    assert store.get_task(dependency.id).state == "interrupted"
    assert store.get_task(queued.id).state == "blocked"
    assert not ticks


def test_non_git_integration_rolls_back_earlier_files_on_write_failure(environment, monkeypatch):
    (environment / "a").write_text("base a")
    (environment / "b").write_text("base b")
    task = executor.submit_task("swe", "change")
    work = Path(task.manifest["execution_root"])
    (work / "a").write_text("task a")
    (work / "b").write_text("task b")
    complete(task)
    executor.review_task(task.id)
    original_replace = isolation.os.replace
    def replace(source, target):
        if Path(target).name == "b":
            raise OSError("simulated disk failure")
        return original_replace(source, target)
    monkeypatch.setattr(isolation.os, "replace", replace)
    with pytest.raises(executor.TaskError, match="disk failure"):
        executor.integrate_task(task.id)
    assert (environment / "a").read_text() == "base a"
    assert (environment / "b").read_text() == "base b"
    assert store.get_task(task.id).integration_state == "unreviewed"


@pytest.mark.parametrize("action", ["cancel_task", "retry_task", "resume_task", "send_to_task"])
def test_public_process_controls_reject_inline_tasks(action):
    store.create_task(store.TaskRecord(id="inline-control", agent="swe", prompt="work", state="running", pid=os.getpid(),
                                      execution_kind="inline", manifest={"parent_thread_id": "parent-session"}))
    args = ("inline-control", "steer") if action == "send_to_task" else ("inline-control",)
    with pytest.raises(executor.TaskError, match="parent thread parent-session"):
        getattr(executor, action)(*args)
    assert store.get_task("inline-control").state == "running"


def test_reconcile_never_signals_or_changes_inline_parent_process(monkeypatch):
    store.create_task(store.TaskRecord(id="inline-dead", agent="swe", prompt="work", state="running", pid=999999,
                                      execution_kind="inline", created_at=0))
    monkeypatch.setattr(executor, "_pid_alive", lambda pid: False)
    assert executor.reconcile_stale_tasks() == []
    assert store.get_task("inline-dead").state == "running"


def test_stopped_inline_child_does_not_block_parent_result_lifecycle(environment):
    parent = executor.submit_task("swe", "parent")
    store.create_task(store.TaskRecord(id="inline-child", agent="review", prompt="review", state="done",
                                      parent_task_id=parent.id, execution_kind="inline"))
    complete(parent)
    executor.review_task(parent.id)
    executor.discard_task(parent.id)
    assert store.get_task(parent.id).integration_state == "discarded"


def test_legacy_unknown_usage_is_incomplete_not_zero_cost(environment):
    from manta_code.agents import usage
    task = executor.submit_task("swe", "work")
    usage.record_usage(usage.UsageRecord(agent="swe", model="fake", task=task.id,
                                        input_tokens=12, output_tokens=3, usage_complete=None, cost_usd=None))
    spend = executor.review_task(task.id)["spend"]
    assert spend["calls"] == 1
    assert spend["input_tokens"] == 12
    assert spend["known_cost_usd"] is None
    assert spend["usage_complete"] is False
    assert spend["cost_complete"] is False


def test_stale_starting_or_running_without_pid_is_interrupted(monkeypatch):
    store.create_task(store.TaskRecord(id="dead-no-pid", agent="swe", prompt="work", state="running", started_at=0, created_at=0))
    assert executor.reconcile_stale_tasks()[0].state == "interrupted"


def test_cancel_finishing_race_does_not_record_false_cancellation(environment, monkeypatch):
    task = executor.submit_task("swe", "work")
    store.update_task(task.id, state="running", pid=999999)
    def finish_before_signal(pid, sig):
        store.update_task(task.id, state="done", pid=None)
    monkeypatch.setattr(executor.os, "killpg", finish_before_signal)
    assert executor.cancel_task(task.id).state == "done"
    assert "task_cancelled" not in {event.kind for event in store.recent_events(task_id=task.id)}


def test_integration_rejects_stale_displayed_review_even_after_another_review(environment):
    (environment / "a").write_text("base")
    task = executor.submit_task("swe", "work")
    work = Path(task.manifest["execution_root"])
    (work / "a").write_text("first review")
    complete(task)
    first = executor.review_task(task.id)
    (work / "a").write_text("second review")
    second = executor.review_task(task.id)
    assert first["patch_sha256"] != second["patch_sha256"]
    with pytest.raises(executor.TaskError, match="reviewed diff changed"):
        executor.integrate_task(task.id, expected_patch_sha256=first["patch_sha256"])
    assert (environment / "a").read_text() == "base"
    executor.integrate_task(task.id, expected_patch_sha256=second["patch_sha256"])
    assert (environment / "a").read_text() == "second review"


def test_parent_spend_distinguishes_own_calls_from_team_total(environment):
    from manta_code.agents import usage
    parent = executor.submit_task("swe", "parent")
    store.create_task(store.TaskRecord(id="child-cost", agent="review", prompt="review", parent_task_id=parent.id,
                                      execution_kind="inline"))
    usage.record_usage(usage.UsageRecord(agent="swe", model="fake", task=parent.id,
                                        input_tokens=12, output_tokens=3, usage_complete=True, cost_usd=None))
    usage.record_usage(usage.UsageRecord(agent="review", model="fake", task="child-cost",
                                        input_tokens=22, output_tokens=4, usage_complete=True, cost_usd=0.5))
    result = executor.review_task(parent.id)
    assert result["spend"]["scope"] == "task_only"
    assert not result["spend"]["includes_children"]
    assert result["spend"]["calls"] == 1
    assert result["team_spend"]["scope"] == "task_and_descendants"
    assert result["team_spend"]["calls"] == 2
    assert result["team_spend"]["input_tokens"] == 34
    assert result["team_spend"]["known_cost_usd"] == 0.5
    assert result["team_spend"]["cost_complete"] is False
    assert result["team_spend"]["usage_complete"] is True
