"""CLI and SDK share real definitions, isolated checkouts and task records."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manta_code import sdk
from manta_code.agents.registry import AgentDef, definition_revision
from manta_code.agents.workspace import AgentWorkspace
from manta_code.main import app
from manta_code.tasks import context, executor, store

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.py").write_text("answer = 1\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    for variable in ("MANTA_ORIGINAL_CWD", "DEEPAGENTS_CODE_SERVER_CWD", "MANTA_TASK_ID", "MANTA_TASK_DEPTH"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(context, "_workspace", lambda profile, config_file: (profile, "https://fixture.databricks.com"))

    def spawn(record, **kwargs):
        store.update_task(record.id, state="running", expect_state="starting")
        return store.get_task(record.id)

    monkeypatch.setattr(executor, "_spawn_task", spawn)
    return root


def test_agent_create_edit_clone_disable_have_identical_cli_sdk_manifests(project):
    created = runner.invoke(app, ["agents", "create", "python-review", "--description", "Review Python",
                                  "--model", "databricks:system.ai.gpt-6-1-sol", "--effort", "low",
                                  "--max-output-tokens", "256", "--context", "fork", "--read-only"])
    assert created.exit_code == 0, created.output
    definition = sdk.agent("python-review")
    assert definition.context_mode == "fork"
    assert definition.reasoning_effort == "low"
    assert definition.max_output_tokens == 256
    shown = runner.invoke(app, ["agents", "show", "python-review", "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout) == sdk.agent_details("python-review")
    settings = project / "settings.json"
    settings.write_text(json.dumps({"description": "A stricter reviewer", "tools_ask": ["custom_read"]}))
    edited = runner.invoke(app, ["agents", "edit", "python-review", "--file", str(settings)])
    assert edited.exit_code == 0, edited.output
    assert sdk.agent("python-review").tools_ask == ["custom_read"]
    clone = runner.invoke(app, ["agents", "clone", "python-review", "python-review-copy"])
    assert clone.exit_code == 0
    assert sdk.agent("python-review-copy").description == "A stricter reviewer"
    assert runner.invoke(app, ["agents", "disable", "python-review"]).exit_code == 0
    assert "python-review" not in {value.name for value in sdk.agents()}
    assert not sdk.agent("python-review", include_disabled=True).enabled
    assert runner.invoke(app, ["agents", "enable", "python-review"]).exit_code == 0
    assert sdk.validate_agent("python-review")["ok"]
    assert runner.invoke(app, ["agents", "validate", "python-review"]).exit_code == 0


def test_sdk_definition_edit_roundtrip_and_stale_cli_edit_preserves_learnings(project, monkeypatch):
    definition = sdk.create_agent(AgentDef(name="helper", system_prompt="Original role"))
    from manta_code.agents.registry import agent_md_path
    learned = agent_md_path("helper")
    learned.write_text("Learned: use project conventions.\n")
    sdk.clone_agent("helper", "other")
    updated = sdk.edit_agent("helper", {"system_prompt": "Updated role"}, expected_revision=definition_revision(definition))
    assert updated.system_prompt == "Updated role"
    assert learned.read_text() == "Learned: use project conventions.\n"
    invalid = project / "invalid.json"
    invalid.write_text('{"unknown_control": true}')
    result = runner.invoke(app, ["agents", "edit", "helper", "--file", str(invalid)])
    assert result.exit_code == 1
    assert sdk.agent("helper").system_prompt == "Updated role"
    sdk.enable_agent("helper", False)
    assert AgentWorkspace().resolve("helper", include_disabled=True).enabled is False


def test_real_directory_task_cli_review_sdk_integrate_preserves_unrelated_work(project):
    submitted = runner.invoke(app, ["task", "submit", "swe", "Improve code", "--isolation", "directory", "--concurrency", "2"])
    assert submitted.exit_code == 0, submitted.output
    task = sdk.tasks()[0]
    work = Path(task.manifest["execution_root"])
    assert work != project and work.is_dir()
    (work / "code.py").write_text("answer = 42\n")
    (work / "new.txt").write_text("artifact\n")
    (project / "unrelated.txt").write_text("local work\n")
    store.record_test_result(task.id, "python -m pytest", 0, "1 passed")
    store.record_artifact(task.id, str(work / "new.txt"), "fixture artifact")
    store.update_task(task.id, state="done", result="Updated the answer", expect_state="running")
    review = runner.invoke(app, ["task", "review", task.id, "--json"])
    assert review.exit_code == 0, review.output
    data = json.loads(review.stdout)
    assert set(data["changed_files"]) == {"code.py", "new.txt"}
    assert data["tests"][0]["exit_code"] == 0
    assert data["artifacts"][0]["label"] == "fixture artifact"
    assert data["spend"]["cost_complete"] is False
    with pytest.raises(executor.TaskError, match="changed"):
        sdk.TaskHandle(task.id).integrate(expected_patch_sha256="stale-review")
    rejected = runner.invoke(app, ["task", "integrate", task.id, "--expected-patch", "stale-review"])
    assert rejected.exit_code == 1
    assert sdk.TaskHandle(task.id).integrate(expected_patch_sha256=data["patch_sha256"]).integration_state == "integrated"
    assert (project / "code.py").read_text() == "answer = 42\n"
    assert (project / "unrelated.txt").read_text() == "local work\n"


def test_sdk_submit_fork_dependencies_and_cli_discard_use_same_store(project):
    first = sdk.submit("review", "Read the fixture")
    second = sdk.submit("swe", "Implement the finding", dependencies=[first.id], parent_task_id=first.id,
                        context_mode="fork", conversation_context="The user asked for a focused change.",
                        max_concurrency=2, isolation="directory")
    record = second.status()
    assert record.state == "waiting_dependencies"
    assert record.dependencies == [first.id]
    assert record.parent_task_id == first.id
    assert "Visible conversation context" in record.prompt
    assert record.manifest["context_mode"] == "fork"
    state = runner.invoke(app, ["task", "status", second.id, "--json"])
    assert json.loads(state.stdout)["dependencies"] == [first.id]
    store.update_task(first.id, state="done", expect_state="running")
    started = runner.invoke(app, ["task", "schedule", "--concurrency", "2"])
    assert started.exit_code == 0
    assert second.status().state == "running"
    store.update_task(second.id, state="failed", expect_state="running")
    retried = runner.invoke(app, ["task", "retry", second.id])
    assert retried.exit_code == 0, retried.output
    assert second.status().attempt == 2
    store.update_task(second.id, state="done", expect_state="running")
    work = Path(second.status().manifest["execution_root"])
    (work / "new.txt").write_text("unintegrated work")
    discarded = runner.invoke(app, ["task", "discard", second.id])
    assert discarded.exit_code == 0, discarded.output
    assert not work.exists()
    assert not (project / "new.txt").exists()
    assert second.status().integration_state == "discarded"
    assert "new.txt" in second.review()["changed_files"]


def test_cli_and_sdk_respond_reject_stale_pending_identity(project, monkeypatch):
    record = store.create_task(store.TaskRecord(
        id="input-task", agent="review", prompt="Need a decision", state="waiting_input", thread_id="fixture-thread",
        checkpoint={"pending_id": "current", "interrupts": {"interrupt": {
            "action_requests": [{"name": "manta_request_input", "args": {"question": "Which file?"}}]}}}))
    with pytest.raises(executor.TaskError, match="changed"):
        sdk.TaskHandle(record.id).respond(pending_id="old", decision="answer", value="code.py")
    rejected = runner.invoke(app, ["task", "respond", record.id, "--pending-id", "old", "--decision", "answer", "--value", "code.py"])
    assert rejected.exit_code == 1
    assert "changed" in rejected.stdout
    captured = {}

    def resume(task_id, **kwargs):
        captured.update(kwargs)
        return store.get_task(task_id)

    monkeypatch.setattr(executor, "resume_task", resume)
    answered = runner.invoke(app, ["task", "respond", record.id, "--pending-id", "current", "--decision", "answer", "--value", "code.py"])
    assert answered.exit_code == 0
    assert captured["resume_value"]["interrupt"]["answer"] == "code.py"


def test_cli_submission_rejects_disabled_agent_and_missing_fork_snapshot(project):
    sdk.enable_agent("review", False)
    assert runner.invoke(app, ["task", "submit", "review", "Read fixture"]).exit_code == 1
    sdk.enable_agent("review", True)
    failure = runner.invoke(app, ["task", "submit", "review", "Read fixture", "--context", "fork"])
    assert failure.exit_code == 1
    assert "explicit visible conversation snapshot" in failure.stdout
