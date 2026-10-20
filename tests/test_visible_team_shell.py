"""Frozen detached shell policy and actual restricted-command execution."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys

import pytest
from pydantic import ValidationError

from manta_code.agents.registry import AgentDef
from manta_code.agents.workspace import AgentWorkspace
from manta_code.tasks import executor, runner, store
from manta_code.tasks.context import TaskContextError, execution_context, validate_execution_manifest


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path / "deepagents"))
    config = tmp_path / "databricks.ini"
    config.write_text("[DEFAULT]\nhost=https://workspace.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    for key in ("MANTA_TASK_ID", "MANTA_TASK_DB_PATH", "MANTA_TASK_DEPTH", "MANTA_TASK_CONFIG_ROOT",
                "MANTA_ORIGINAL_CWD", "MANTA_PROJECT_ROOT", "MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE",
                "DATABRICKS_HOST", "DEEPAGENTS_CODE_SERVER_CWD", "DEEPAGENTS_CODE_SERVER_PROJECT_ROOT"):
        monkeypatch.delenv(key, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(executor, "_spawn_task", lambda record, **_kwargs: record)
    monkeypatch.setattr("manta_code.tasks.scheduler.ensure_scheduler", lambda **_kwargs: None)
    return project


@pytest.mark.parametrize("entries", [[""], ["pytest,python"], ["pytest\npython"], ["all", "pytest"], ["ALL"],
                                      ["recommended"], ["Recommended", "pytest"]])
def test_shell_definition_rejects_ambiguous_or_unrepresentable_entries(entries):
    with pytest.raises(ValidationError):
        AgentDef(name="fixture", task_shell_allow_list=entries)


@pytest.mark.parametrize("entries,expected", [([], []), ([" pytest ", "pytest"], ["pytest"]), (["all"], ["all"])])
def test_definition_roundtrip_effective_view_and_frozen_manifest(entries, expected):
    workspace = AgentWorkspace()
    workspace.create(AgentDef(name="shell-fixture", task_shell_allow_list=entries))
    assert AgentWorkspace().resolve("shell-fixture").task_shell_allow_list == expected
    effective = workspace.effective("shell-fixture")
    assert effective["detached_shell"] == {"enabled": bool(expected), "allow_list": expected}
    task = executor.submit_task("shell-fixture", "Inspect shell policy")
    assert task.manifest["task_shell_allow_list"] == expected
    validate_execution_manifest(task.manifest)
    workspace.update("shell-fixture", {"task_shell_allow_list": ["python"]})
    with pytest.raises(TaskContextError, match="agent/model/policy changed"):
        validate_execution_manifest(task.manifest)
    assert store.get_task(task.id).manifest["task_shell_allow_list"] == expected


@pytest.mark.parametrize("entries,expected", [([], None), (["pytest", "python"], "pytest,python"), (["all"], "all")])
def test_actual_runner_forwards_only_the_frozen_shell_policy(monkeypatch, entries, expected):
    workspace = AgentWorkspace()
    workspace.create(AgentDef(name="shell-fixture", task_shell_allow_list=entries))
    task = executor.submit_task("shell-fixture", "Use reviewed shell policy")
    calls = []
    monkeypatch.setattr("manta_code.dcode.run_headless", lambda **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr("manta_code.auth.databricks_configured", lambda _profile: True)
    monkeypatch.setenv("DEEPAGENTS_CODE_SHELL_ALLOW_LIST", "all")
    assert runner.run_task(task.id) == 0
    assert calls[0]["shell_allow_list"] == expected
    assert store.get_task(task.id).state == "done"


def test_nested_runner_uses_reviewed_configuration_root(monkeypatch, environment, tmp_path):
    original = tmp_path / "reviewed-config"
    (original / ".manta").mkdir(parents=True)
    (original / ".manta" / "config.toml").write_text('[interactive]\ndefault_endpoint="reviewed-target"\n')
    (environment / ".manta").mkdir()
    (environment / ".manta" / "config.toml").write_text('[interactive]\ndefault_endpoint="snapshot-target"\n')
    monkeypatch.setenv("MANTA_TASK_CONFIG_ROOT", str(original))
    workspace = AgentWorkspace()
    workspace.create(AgentDef(name="shell-fixture"))
    parent = executor.submit_task("shell-fixture", "Parent task")
    with execution_context(parent.manifest):
        child = executor.submit_task("shell-fixture", "Nested task", parent_task_id=parent.id)
    assert child.manifest["source_project_root"] != str(original)
    assert child.manifest["config_root"] == str(original)
    calls = []
    monkeypatch.setattr("manta_code.dcode.run_headless", lambda **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr("manta_code.auth.databricks_configured", lambda _profile: True)
    assert runner.run_task(child.id) == 0
    assert calls[0]["default_endpoint"] == "reviewed-target"
    (original / ".manta" / "config.toml").write_text('[interactive]\ndefault_endpoint="drifted-target"\n')
    with pytest.raises(TaskContextError, match="runtime configuration changed"):
        validate_execution_manifest(child.manifest)


@pytest.mark.parametrize("entries", [[], ["pytest"], ["all"]])
def test_fresh_headless_process_ignores_inherited_shell_expansion(entries, tmp_path):
    """Use the real resolver in a separate process with an unrestricted config."""
    store.create_task(store.TaskRecord(id="shell-process", agent="fixture", prompt="Fixture", state="running",
                                      manifest={"task_shell_allow_list": entries}))
    deepagents = tmp_path / "deepagents"
    deepagents.mkdir(exist_ok=True)
    (deepagents / "config.toml").write_text('[shell]\nallow_list=["all"]\n')
    env = {**os.environ, "MANTA_TASK_ID": "shell-process", "DEEPAGENTS_CODE_SHELL_ALLOW_LIST": "all"}
    script = """
import json
from deepagents_code.client import non_interactive as client
from deepagents_code.config import SHELL_ALLOW_ALL
from manta_code.budget_runtime import install_budget_runtime
before = client._resolve_shell_allow_list()
assert isinstance(before, type(SHELL_ALLOW_ALL))
assert install_budget_runtime()
after = client._resolve_shell_allow_list()
print(json.dumps({'entries': after, 'unrestricted': isinstance(after, type(SHELL_ALLOW_ALL))}))
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=True)
    actual = json.loads(result.stdout.strip().splitlines()[-1])
    assert actual["unrestricted"] is (entries == ["all"])
    assert actual["entries"] == (["__ALL__"] if entries == ["all"] else entries or None)


@pytest.mark.parametrize("allowed", [False, True])
def test_restricted_shell_executes_real_process_only_after_exact_human_approval(allowed, environment, monkeypatch):
    from deepagents_code.agent import ShellAllowListMiddleware
    from langchain.agents import create_agent
    from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from manta_code import budget_runtime
    from manta_code.tasks import approvals
    from manta_code.tasks.events import EventLogMiddleware

    class Scripted(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    task = store.create_task(store.TaskRecord(id="native-shell", agent="fixture", prompt="Fixture", state="running"))
    monkeypatch.setenv("MANTA_TASK_ID", task.id)
    effect = environment / "executed.txt"
    command = shlex.join([sys.executable, "-c", "__import__('pathlib').Path('executed.txt').write_text('once')"])

    @tool(response_format="content_and_artifact")
    def execute(command: str) -> tuple[str, dict]:
        """Run a harmless fixed fixture command in the disposable checkout."""
        completed = subprocess.run(shlex.split(command), cwd=environment, capture_output=True, text=True, check=False)
        return completed.stdout + completed.stderr, {"exit_code": completed.returncode}

    gate = HumanInTheLoopMiddleware({"execute": {"allowed_decisions": ["approve", "reject"]}})
    graph = create_agent(Scripted(responses=[AIMessage(content="", tool_calls=[
        {"name": "execute", "id": "actual-shell", "args": {"command": command}}]), AIMessage(content="Done")]),
        [execute], middleware=[EventLogMiddleware(agent="fixture"), ShellAllowListMiddleware(
            [sys.executable] if allowed else ["pytest"]), gate], checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "restricted-shell-thread"}}

    async def exercise():
        paused = await graph.ainvoke({"messages": [HumanMessage(content="Run the fixture")]}, config)
        assert paused["__interrupt__"] and not effect.exists()
        snapshot = await graph.aget_state(config)
        requests = budget_runtime.pending_requests(snapshot)
        checkpoint = approvals.checkpoint_requests(task.id, "restricted-shell-thread", requests)
        saved = store.get_task(task.id)
        assert saved.state == "waiting_approval" and saved.checkpoint == checkpoint
        decisions = approvals.decision_mapping(requests, "approve")
        result = await graph.ainvoke(Command(resume=decisions), config)
        assert not result.get("__interrupt__")
        assert effect.exists() is allowed
        if allowed:
            assert effect.read_text() == "once"
        else:
            assert any(getattr(message, "status", "") == "error" and "not in the allow-list" in str(message.content)
                       for message in result["messages"])

    asyncio.run(exercise())
