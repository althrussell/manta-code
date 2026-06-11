from __future__ import annotations

import pytest

from manta_code.agents import registry
from manta_code.agents.registry import AgentDef, FsRule


def test_name_validation():
    assert registry.is_valid_name("data-reviewer")
    assert registry.is_valid_name("swe")
    assert not registry.is_valid_name("Bad Name")
    assert not registry.is_valid_name("-leading")
    assert not registry.is_valid_name("trailing-")
    with pytest.raises(ValueError):
        AgentDef(name="Has Space")


def test_save_load_round_trip(tmp_path):
    defn = AgentDef(
        name="reviewer",
        description="Read-only reviewer",
        model="databricks:databricks-claude-sonnet-4-5",
        system_prompt="You review code.\n\nBe thorough.",
        read_only=True,
        tools_deny=["execute"],
        approval=["write_file"],
        budget_max_tokens=50_000,
        budget_max_usd=1.5,
        databricks_tools=["uc_catalog", "sql"],
        filesystem=[FsRule(operations=["read"], paths=["/tmp/**"], mode="allow")],
    )
    registry.save_agent(defn, root=tmp_path)

    # Prompt lives in AGENTS.md, structured data in agent.toml.
    assert registry.agent_md_path("reviewer", root=tmp_path).read_text().startswith(
        "You review code."
    )
    toml_text = registry.agent_toml_path("reviewer", root=tmp_path).read_text()
    assert "system_prompt" not in toml_text

    loaded = registry.load_agent("reviewer", root=tmp_path)
    assert loaded.name == "reviewer"
    assert loaded.read_only is True
    assert loaded.model == "databricks:databricks-claude-sonnet-4-5"
    assert loaded.system_prompt.endswith("Be thorough.")
    assert loaded.tools_deny == ["execute"]
    assert loaded.approval == ["write_file"]
    assert loaded.budget_max_usd == 1.5
    assert loaded.databricks_tools == ["uc_catalog", "sql"]
    assert loaded.filesystem[0].paths == ["/tmp/**"]


def test_relative_fs_path_normalized_to_absolute_glob():
    rule = FsRule(operations=["read"], paths=["src/**", "/abs/**"])
    assert rule.paths == ["/**/src/**", "/abs/**"]


def test_list_and_delete(tmp_path):
    registry.save_agent(AgentDef(name="alpha"), root=tmp_path)
    registry.save_agent(AgentDef(name="beta"), root=tmp_path)
    names = [d.name for d in registry.list_agents(root=tmp_path)]
    assert names == ["alpha", "beta"]

    assert registry.delete_agent("alpha", root=tmp_path) is True
    assert registry.delete_agent("alpha", root=tmp_path) is False
    assert [d.name for d in registry.list_agents(root=tmp_path)] == ["beta"]


def test_list_skips_malformed(tmp_path):
    registry.save_agent(AgentDef(name="good"), root=tmp_path)
    bad_dir = registry.agents_root(tmp_path) / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "agent.toml").write_text("this is not = valid = toml ==", encoding="utf-8")
    names = [d.name for d in registry.list_agents(root=tmp_path)]
    assert names == ["good"]


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        registry.load_agent("nope", root=tmp_path)


def test_effective_namespace_defaults_to_name():
    assert AgentDef(name="x").effective_namespace() == "x"
    assert AgentDef(name="x", memory_namespace="shared").effective_namespace() == "shared"
