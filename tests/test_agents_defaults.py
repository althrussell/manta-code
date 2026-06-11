from __future__ import annotations

from manta_code.agents import defaults
from manta_code.agents.registry import AgentDef


def test_defaults_are_enforced():
    by_name = {a.name: a for a in defaults.DEFAULT_AGENTS}
    assert by_name["planning"].read_only is True
    assert by_name["review"].read_only is True
    assert by_name["swe"].read_only is False
    # SWE gates its mutations behind approval.
    assert "execute" in by_name["swe"].approval


def test_all_defaults_pin_databricks_models():
    for agent in defaults.DEFAULT_AGENTS:
        assert agent.model and agent.model.startswith("databricks:")


def test_merged_agents_user_overrides_default():
    custom_review = AgentDef(name="review", read_only=False, description="relaxed")
    merged = defaults.merged_agents([custom_review, AgentDef(name="extra")])
    by_name = {a.name: a for a in merged}
    # User's review replaces the built-in; planning/swe defaults remain; extra added.
    assert by_name["review"].read_only is False
    assert "planning" in by_name and "swe" in by_name
    assert "extra" in by_name
    # Exactly one entry per name (no duplicate review).
    assert len(merged) == len({a.name for a in merged})


def test_defaults_compile():
    import pytest

    pytest.importorskip("deepagents")
    from manta_code.agents.factory import compile_subagent

    for agent in defaults.DEFAULT_AGENTS:
        sub = compile_subagent(agent)
        assert sub["name"] == agent.name
        assert sub["system_prompt"]


def test_chief_is_read_only_with_task_tools():
    from manta_code.agents.defaults import CHIEF

    # The chief coordinates; it must not write code, and its power comes from
    # the background-task tools (ADR 0010 Phase B).
    assert CHIEF.read_only is True
    assert "tasks" in CHIEF.manta_tools
    assert CHIEF.model  # pinned, like the other built-ins
