from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from manta_code.agents import memory
from manta_code.agents.profiles import MANTA_PROFILE_SENTINEL, ROLE_FILENAME, sync_agent_profiles
from manta_code.agents.registry import AgentDef, save_agent
from manta_code.tasks import executor, runner, store
from manta_code.tasks.context import TaskContextError, build_execution_manifest, execution_context, resolve_profile, validate_execution_manifest


@pytest.fixture(autouse=True)
def isolated_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    config = tmp_path / "databricks.ini"
    config.write_text("[DEFAULT]\nhost=https://default.example.com\n[labs]\nhost=https://labs.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    for key in ("DATABRICKS_HOST", "MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE", "MANTA_TASK_ID",
                "DEEPAGENTS_CODE_SERVER_CWD", "DEEPAGENTS_CODE_SERVER_PROJECT_ROOT", "MANTA_ORIGINAL_CWD"):
        monkeypatch.delenv(key, raising=False)


def test_task_subdirectory_uses_project_root_model_and_detects_changes(tmp_path, monkeypatch):
    from manta_code.config import load_config
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".manta").mkdir()
    config = project / ".manta/config.toml"
    config.write_text('[interactive]\ndefault_endpoint="custom.team.first"\n')
    subdir = project / "src"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    monkeypatch.delenv("MANTA_PROJECT_ROOT", raising=False)
    save_agent(AgentDef(name="unbound"))
    manifest = build_execution_manifest("unbound", "labs")
    assert manifest["model"] == "databricks:custom.team.first"
    with execution_context(manifest):
        assert load_config().interactive.default_endpoint == "custom.team.first"
    config.write_text('[interactive]\ndefault_endpoint="custom.team.changed"\n')
    with pytest.raises(TaskContextError, match="model/policy changed|runtime configuration changed"):
        validate_execution_manifest(manifest)


def test_bound_memory_tools_isolate_children_and_honor_shared_namespace(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "chief")
    shared = AgentDef(name="first", memory_namespace="shared")
    (first,) = memory.build_memory_tools(shared)
    (second,) = memory.build_memory_tools(AgentDef(name="second", memory_namespace="shared"))
    (private,) = memory.build_memory_tools(AgentDef(name="private"))
    first.invoke({"note": "shared convention"})
    second.invoke({"note": "another shared convention"})
    private.invoke({"note": "private convention"})
    db = memory.shared_memory_store()
    assert len(memory.read_memories(db, memory.memory_namespace(shared))) == 2
    assert memory.read_memories(db, ("memories", "private")) == ["private convention"]
    assert memory.read_memories(db, ("memories", "chief")) == []
    assert db.search(("memories", "shared"))[0].value["_manta"]["scope"] == "shared-agent"


def test_memory_disabled_has_no_tool_and_public_helper_does_not_write(monkeypatch):
    disabled = AgentDef(name="ephemeral", memory=False)
    save_agent(disabled)
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", disabled.name)
    assert memory.build_memory_tools(disabled) == []
    assert memory.agent_memory_middleware(disabled) is None
    assert "disabled" in memory.manta_remember("do not retain")
    assert memory.read_memories(memory.shared_memory_store(), ("memories", disabled.name)) == []


def test_global_memory_disabled_prevents_public_helper_write(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ENABLE_MEMORY", "false")
    assert "disabled" in memory.manta_remember("do not retain globally")
    assert memory.read_memories(memory.shared_memory_store(), ("memories", "orchestrator")) == []


def test_legacy_sqlite_migration_is_backed_up_unassigned_and_idempotent(tmp_path):
    path = tmp_path / "old.db"
    from langgraph.store.sqlite import SqliteStore

    old = SqliteStore(sqlite3.connect(path, isolation_level=None))
    old.setup()
    old.put(("memories", "swe"), "old-note", {"text": "unknown original repository"})
    migrated = memory.open_store(path)
    assert memory.read_memories(migrated, ("memories", "swe")) == ["unknown original repository"]
    assert memory.read_memories(migrated, ("memories", "swe"), include_legacy=False) == []
    assert memory.legacy_memory_count(migrated, ("memories", "swe")) == 1
    backup = path.with_name(path.name + ".pre-v2.bak")
    assert path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    before = backup.read_bytes()
    reopened = memory.open_store(path)
    item = reopened.get(("memories", "swe"), "old-note")
    assert item.value["_manta"]["scope"] == "legacy-unassigned"
    assert item.value["_manta"]["source"] == "legacy-sqlite"
    assert backup.read_bytes() == before
    memory.write_memory(reopened, ("memories", "swe"), "assigned", "explicit agent note")
    assert memory.read_memories(reopened, ("memories", "swe"), include_legacy=False) == ["explicit agent note"]


def test_non_note_values_do_not_break_recall_or_migration_notice():
    from types import SimpleNamespace

    class Store:
        def search(self, *args, **kwargs):
            return [SimpleNamespace(value=value) for value in [
                ["other stored data"], "string", {"text": "legacy", "_manta": "old metadata"},
                {"text": "current", "_manta": {"scope": "agent"}},
            ]]

    assert memory.read_memories(Store(), ("memories", "swe"), include_legacy=False) == ["current"]
    assert memory.legacy_memory_count(Store(), ("memories", "swe")) == 1


def test_profile_split_preserves_learning_and_archives_legacy_without_guessing_scope(tmp_path):
    profiles = tmp_path / "profiles"
    state = profiles / ".state" / "managed.json"
    old = profiles / "swe" / "AGENTS.md"
    old.parent.mkdir(parents=True)
    original = MANTA_PROFILE_SENTINEL + "\nOld role\nUnassigned learning\n"
    old.write_text(original)
    defn = AgentDef(name="swe", system_prompt="New role")
    migrated = sync_agent_profiles([defn], deepagents_dir=profiles, state_path=state)
    assert migrated.migrated == ["swe"]
    (backup,) = (state.parent / "manta-profile-backups").glob("swe/*/AGENTS.md")
    assert backup.read_text() == original
    provenance = json.loads(backup.with_name("provenance.json").read_text())
    assert provenance["scope"] == "legacy-unassigned"
    old.write_text(old.read_text() + "\nNew explicit learning\n")
    defn.system_prompt = "Updated role"
    sync_agent_profiles([defn], deepagents_dir=profiles, state_path=state)
    assert "New explicit learning" in old.read_text()
    assert "Updated role" not in old.read_text()
    assert "Updated role" in old.with_name(ROLE_FILENAME).read_text()
    assert len(list((state.parent / "manta-profile-backups").glob("swe/*/AGENTS.md"))) == 1
    sync_agent_profiles([], deepagents_dir=profiles, state_path=state)
    (archived,) = (state.parent / "manta-profile-archive").glob("swe-*/AGENTS.md")
    assert "New explicit learning" in archived.read_text()


def test_profile_precedence_and_context_restores_user_subdirectory(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_PROFILE", "environment")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "sdk")
    assert resolve_profile("child", "root") == "child"
    assert resolve_profile(root="root") == "root"
    assert resolve_profile() == "environment"
    monkeypatch.delenv("MANTA_PROFILE")
    assert resolve_profile() == "sdk"
    project = tmp_path / "repo"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_CWD", str(cwd))
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_PROJECT_ROOT", str(project))
    manifest = build_execution_manifest("swe", "labs")
    monkeypatch.setenv("DATABRICKS_TOKEN", "private credential never persisted")
    assert manifest["original_cwd"] == str(cwd)
    assert manifest["project_root"] == str(project)
    assert manifest["workspace"] == "https://labs.example.com"
    assert "private credential" not in json.dumps(manifest)
    monkeypatch.chdir(tmp_path)
    with execution_context(manifest):
        assert Path.cwd() == cwd
        assert __import__("os").environ["MANTA_PROFILE"] == "labs"
    assert Path.cwd() == tmp_path


def test_task_context_rejects_changed_workspace_policy_and_missing_project(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    custom = AgentDef(name="custom", model="databricks:system.ai.test")
    save_agent(custom)
    manifest = build_execution_manifest("custom", "labs", cwd=str(project))
    monkeypatch.setenv("DATABRICKS_HOST", "https://elsewhere.example.com")
    with pytest.raises(TaskContextError, match="different workspace"):
        validate_execution_manifest(manifest)
    monkeypatch.delenv("DATABRICKS_HOST")
    custom.read_only = True
    save_agent(custom)
    with pytest.raises(TaskContextError, match="agent/model/policy changed"):
        validate_execution_manifest(manifest)
    custom.read_only = False
    save_agent(custom)
    project.rmdir()
    with pytest.raises(TaskContextError, match="unavailable"):
        validate_execution_manifest(manifest)


def test_legacy_task_blocks_instead_of_running_in_home(monkeypatch):
    task = store.create_task(store.TaskRecord(id="legacy", agent="swe", prompt="old task"))
    from manta_code import dcode

    def must_not_run(**kwargs):
        raise AssertionError("must not launch a task without verified context")

    monkeypatch.setattr(dcode, "run_headless", must_not_run)
    assert runner.run_task(task.id) == 2
    assert store.get_task(task.id).state == "blocked"
    assert "Legacy task" in store.get_task(task.id).pending_reason


def test_incomplete_manifest_blocks_with_recoverable_error():
    with pytest.raises(TaskContextError, match="incomplete"):
        validate_execution_manifest({"schema_version": 1})


def test_task_database_additive_migration_preserves_old_rows(tmp_path):
    path = tmp_path / "legacy-tasks.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(store._SCHEMA)
        connection.execute(
            "INSERT INTO tasks(id, agent, prompt, state, created_at) VALUES (?, ?, ?, ?, ?)",
            ("legacy", "swe", "original task", "queued", 1.0),
        )
    legacy = store.get_task("legacy", path=path)
    assert legacy.prompt == "original task"
    assert legacy.manifest == {}
    assert legacy.checkpoint == {}
    assert legacy.thread_id == legacy.pending_reason == ""
    # Reopening and repeating all additive migrations remains safe.
    assert store.get_task("legacy", path=path) == legacy


def test_budget_pause_and_resume_preserves_manifest_and_checkpoint(tmp_path, monkeypatch):
    manifest = build_execution_manifest("swe", "labs", cwd=str(tmp_path))
    log = tmp_path / "task.log"
    record = store.create_task(store.TaskRecord(id="paused", agent="swe", prompt="finish it",
                               state="running", log_path=str(log), manifest=manifest))
    checkpoint = {"resume_value": {"interrupt-1": {"decisions": [{"type": "approve"}]}}}
    assert store.pause_task(record.id, "Budget reached", thread_id="thread-1", checkpoint=checkpoint)
    spawned = {}

    class Process:
        pid = 9876

    def fake_spawn(*args, **kwargs):
        spawned.update(kwargs)
        return Process()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_spawn)
    executor.resume_task(record.id)
    restored = store.get_task(record.id)
    assert restored.state == "starting"
    assert restored.manifest == manifest
    assert spawned["cwd"] == str(tmp_path)
    assert spawned["env"]["MANTA_RESUME_THREAD_ID"] == "thread-1"
    assert json.loads(spawned["env"]["MANTA_RESUME_VALUE"]) == checkpoint["resume_value"]


def test_resume_refuses_changed_context_without_losing_checkpoint(tmp_path, monkeypatch):
    manifest = build_execution_manifest("swe", "labs", cwd=str(tmp_path))
    checkpoint = {"resume_value": {"interrupt-1": {"decisions": [{"type": "approve"}]}}}
    record = store.create_task(store.TaskRecord(id="changed", agent="swe", prompt="finish it",
                               state="running", manifest=manifest))
    assert store.pause_task(record.id, "Budget reached", thread_id="thread-1", checkpoint=checkpoint)
    monkeypatch.setenv("DATABRICKS_HOST", "https://other.example.com")
    with pytest.raises(executor.TaskError, match="different workspace"):
        executor.resume_task(record.id)
    loaded = store.get_task(record.id)
    assert loaded.state == "paused_budget"
    assert loaded.checkpoint == checkpoint
