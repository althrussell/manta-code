from __future__ import annotations

import concurrent.futures
import tomllib

import pytest

from manta_code.agents import registry
from manta_code.agents.defaults import merged_agents
from manta_code.agents.profiles import sync_agent_profiles
from manta_code.agents.registry import AgentDef, definition_revision
from manta_code.agents.workspace import AgentWorkspace


def test_builtin_disable_is_durable_and_does_not_mutate_default(tmp_path):
    workspace = AgentWorkspace(root=tmp_path)
    workspace.set_enabled("review", False)
    assert "review" not in {item.name for item in AgentWorkspace(root=tmp_path).list()}
    assert not AgentWorkspace(root=tmp_path).resolve("review", include_disabled=True).enabled
    assert "review" not in {item.name for item in merged_agents(registry.list_agents(root=tmp_path))}
    with pytest.raises(ValueError, match="disabled"):
        workspace.resolve("review")
    workspace.set_enabled("review", True)
    assert workspace.resolve("review").enabled
    assert AgentWorkspace(root=tmp_path / "other").resolve("review").enabled


def test_registry_edits_preserve_legacy_and_new_learnings(tmp_path):
    directory = registry.agent_dir("custom", root=tmp_path)
    directory.mkdir(parents=True)
    (directory / "agent.toml").write_text('name="custom"\ndescription="legacy"\n')
    learned = "Original legacy role\n\nLearned: run uv tests.\n"
    (directory / "AGENTS.md").write_text(learned)
    workspace = AgentWorkspace(root=tmp_path)
    old = workspace.resolve("custom")
    saved = workspace.update("custom", {"system_prompt": "A revised role", "description": "updated"},
                             expected_revision=definition_revision(old))
    assert saved.system_prompt == "A revised role"
    assert (directory / "AGENTS.md").read_text() == learned
    (directory / "AGENTS.md").write_text(learned + "Learned: use ruff.\n")
    workspace.update("custom", {"description": "again"})
    assert (directory / "AGENTS.md").read_text() == learned + "Learned: use ruff.\n"


def test_role_and_definition_commit_is_atomic_on_failure(tmp_path, monkeypatch):
    original = AgentDef(name="atomic", model="databricks:system.ai.glm-5-3", system_prompt="old role")
    registry.save_agent(original, root=tmp_path)
    write = registry._atomic_write

    def fail_toml(path, content):
        if path.name == "agent.toml":
            raise OSError("simulated commit failure")
        return write(path, content)

    monkeypatch.setattr(registry, "_atomic_write", fail_toml)
    with pytest.raises(OSError, match="commit failure"):
        registry.save_agent(original.model_copy(update={"model": "databricks:system.ai.kimi-k3", "system_prompt": "new role"}), root=tmp_path)
    loaded = registry.load_agent("atomic", root=tmp_path)
    assert loaded.system_prompt == "old role"
    assert loaded.model == "databricks:system.ai.glm-5-3"


def test_concurrent_create_is_not_last_writer_wins(tmp_path):
    def create(index):
        try:
            return AgentWorkspace(root=tmp_path).create(AgentDef(name="race", description=str(index))).description
        except FileExistsError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(create, [1, 2]))
    assert len([value for value in values if value is not None]) == 1
    assert AgentWorkspace(root=tmp_path).resolve("race").description in {"1", "2"}


def test_clone_is_independent_and_optimistic_edit_rejects_stale(tmp_path):
    workspace = AgentWorkspace(root=tmp_path)
    workspace.create(AgentDef(name="first", memory_namespace="shared", system_prompt="Review carefully"))
    first = workspace.resolve("first")
    cloned = workspace.clone("first", "second")
    assert cloned.effective_namespace() == "second"
    assert cloned.system_prompt == first.system_prompt
    revision = definition_revision(first)
    workspace.update("first", {"description": "changed"}, expected_revision=revision)
    with pytest.raises(ValueError, match="changed"):
        workspace.update("first", {"description": "lost update"}, expected_revision=revision)
    assert workspace.resolve("second").description == ""
    with pytest.raises(FileExistsError):
        workspace.clone("first", "review")


def test_effective_manifest_distinguishes_inherited_launch_state(tmp_path):
    workspace = AgentWorkspace(root=tmp_path)
    workspace.create(AgentDef(name="inspect", read_only=True, memory=False, context_mode="fork",
                             reasoning_effort="low", max_output_tokens=128, tools_deny=["custom"],
                             budget_max_tokens=1000))
    manifest = workspace.effective("inspect", inherited_model="databricks:system.ai.gpt-6-1-sol",
                                   inherited_tools=["custom", "manta_remember", "sql_query", "read_file"])
    assert manifest["model_source"] == "inherited"
    assert manifest["tools"] == ["custom", "read_file"]
    assert manifest["policy"]["read_only"] is True
    assert "custom" in manifest["policy"]["deny"]
    assert manifest["memory"]["enabled"] is False
    assert manifest["detached_context_mode"] == "isolated"
    assert workspace.effective("inspect")["tools"] is None
    assert workspace.test("inspect")["context_mode"] == "fork"


@pytest.mark.parametrize("changes", [
    {"context_mode": "fork", "skills": ["/skills/"]},
    {"max_output_tokens": 0}, {"budget_max_tokens": -1}, {"budget_max_usd": float("nan")},
    {"databricks_tools": ["nonexistent"]}, {"manta_tools": ["nonexistent"]},
    {"model": "databricks:system.ai.gpt-6-1-sol", "reasoning_effort": "ultra"},
    {"model": "databricks:system.ai.gpt-6-1-sol", "max_output_tokens": 128001},
    {"model": "bare-model"}, {"unused_field": True},
])
def test_invalid_controls_cannot_be_saved(tmp_path, changes):
    workspace = AgentWorkspace(root=tmp_path)
    workspace.create(AgentDef(name="safe"))
    with pytest.raises(ValueError):
        workspace.update("safe", changes)
    assert workspace.resolve("safe") == AgentDef(name="safe")


def test_disable_and_reenable_restore_profile_learning(tmp_path):
    workspace = AgentWorkspace(root=tmp_path / "registry")
    base = tmp_path / "profiles"
    state = base / ".state" / "profiles.json"
    sync_agent_profiles(workspace.list(), deepagents_dir=base, state_path=state)
    path = base / "review" / "AGENTS.md"
    path.write_text(path.read_text() + "Learned: use project tests.\n")
    workspace.set_enabled("review", False)
    sync_agent_profiles(workspace.list(), deepagents_dir=base, state_path=state)
    assert not path.exists()
    workspace.set_enabled("review", True)
    sync_agent_profiles(workspace.list(), deepagents_dir=base, state_path=state)
    assert "Learned: use project tests." in path.read_text()


def test_profile_restore_does_not_confuse_agent_name_prefixes(tmp_path):
    base = tmp_path / "profiles"
    state = base / ".state" / "profiles.json"
    definitions = [AgentDef(name="review"), AgentDef(name="review-copy")]
    sync_agent_profiles(definitions, deepagents_dir=base, state_path=state)
    original = base / "review" / "AGENTS.md"
    copy = base / "review-copy" / "AGENTS.md"
    original.write_text(original.read_text() + "Original agent learning\n")
    copy.write_text(copy.read_text() + "Cloned agent learning\n")
    sync_agent_profiles([], deepagents_dir=base, state_path=state)
    sync_agent_profiles([definitions[0]], deepagents_dir=base, state_path=state)
    assert "Original agent learning" in original.read_text()
    assert "Cloned agent learning" not in original.read_text()


def test_registry_role_pointer_is_bounded_to_immutable_role_dir(tmp_path):
    registry.save_agent(AgentDef(name="safe"), root=tmp_path)
    path = registry.agent_toml_path("safe", root=tmp_path)
    data = tomllib.loads(path.read_text())
    assert data["role_file"].startswith(".roles/")
    path.write_text('name="safe"\nrole_file="../../secret"\n')
    with pytest.raises(ValueError, match="invalid role_file"):
        registry.load_agent("safe", root=tmp_path)


@pytest.mark.parametrize("mode,expects_parent_history", [("isolated", False), ("fork", True)])
def test_real_delegation_context_matches_definition(mode, expects_parent_history):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from manta_code.agents.factory import compile_subagent

    class RecordingModel(FakeMessagesListChatModel):
        seen: list = []

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            self.seen.append(messages)
            return super()._generate(messages, **kwargs)

    child = RecordingModel(responses=[AIMessage(content="CHILD_DONE")])
    parent = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": "specialist", "description": "Return CHILD_DONE"}, "id": "fixture-task", "type": "tool_call"}]),
        AIMessage(content="PARENT_DONE"),
    ])
    spec = compile_subagent(AgentDef(name="specialist", description="Test context", context_mode=mode, memory=False))
    spec["model"] = child
    graph = create_deep_agent(parent, subagents=[spec])
    graph.invoke({"messages": [HumanMessage(content="PARENT_HISTORY_MARKER; delegate this task.")]})
    assert child.seen
    visible = "\n".join(str(message.content) for message in child.seen[0])
    assert ("PARENT_HISTORY_MARKER" in visible) is expects_parent_history
    assert "Return CHILD_DONE" in visible


def test_managed_ask_preapproval_does_not_waive_explicit_approval(tmp_path, monkeypatch):
    from manta_code.agents.factory import _runtime_interrupt_on, native_interrupt_on, select_agent_tools
    from manta_code.tasks import store
    from langchain_core.tools import tool

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_TASK_ID", "managed")
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    store.create_task(store.TaskRecord(id="managed", agent="review", prompt="test", state="running"))
    gates = _runtime_interrupt_on(AgentDef(name="guarded", tools_ask=["ask_only", "both"], approval=["explicit", "both"]))
    assert all(gate["when"](None) for gate in gates.values())
    monkeypatch.setenv("MANTA_ALLOW_ASKS", "1")
    assert not gates["ask_only"]["when"](None)
    assert gates["explicit"]["when"](None)
    assert gates["both"]["when"](None)

    @tool
    def run_job() -> str:
        """Harmless job fixture."""
        return "fixture"

    @tool
    def manta_request_input(question: str) -> str:
        """Harmless input fixture."""
        return question

    assert native_interrupt_on([run_job])["run_job"]["when"](None)
    assert "manta_request_input" in {item.name for item in select_agent_tools(AgentDef(name="reader", memory=False), [manta_request_input])}
