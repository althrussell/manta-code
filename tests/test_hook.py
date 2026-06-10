from __future__ import annotations

import pytest

from manta_code import hook

pytest.importorskip("deepagents")


def test_enrich_injects_builtin_subagents(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    names = {s["name"] for s in kwargs["subagents"]}
    # Built-ins are always injected even with an empty registry.
    assert {"planning", "swe", "review"} <= names


def test_enrich_user_agent_overrides_markdown_subagent(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    from manta_code.agents.registry import AgentDef, save_agent

    save_agent(AgentDef(name="planning", description="my planner"), root=tmp_path)

    # Simulate an upstream markdown subagent named "planning" already present.
    kwargs = {"subagents": [{"name": "planning", "description": "old", "system_prompt": "x"}]}
    hook.enrich_kwargs(kwargs)
    planners = [s for s in kwargs["subagents"] if s["name"] == "planning"]
    # Exactly one "planning" remains — Manta's, not the markdown one.
    assert len(planners) == 1
    assert planners[0]["description"] == "my planner"


def test_enrich_preserves_unrelated_subagents(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    kwargs = {"subagents": [{"name": "general-purpose", "description": "d", "system_prompt": "p"}]}
    hook.enrich_kwargs(kwargs)
    names = {s["name"] for s in kwargs["subagents"]}
    assert "general-purpose" in names  # not a Manta agent -> kept
    assert "planning" in names  # Manta built-in -> added


def test_active_agent_name_reads_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "review")
    assert hook.active_agent_name() == "review"
    # The base profile is not a Manta agent.
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "agent")
    assert hook.active_agent_name() is None
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    assert hook.active_agent_name() is None


def test_enrich_enforces_active_profile_top_level(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    # Select the read-only built-in 'review' as the top-level profile.
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "review")
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    mw_names = {type(m).__name__ for m in (kwargs.get("middleware") or [])}
    # review is read_only -> its tool policy is enforced on the primary loop.
    assert "ToolPolicyMiddleware" in mw_names


def test_enrich_pins_model_for_active_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "planning")
    # Force the resolver so the test doesn't depend on the Databricks chat import.
    from manta_code.middleware import routing as R

    monkeypatch.setattr(R, "databricks_model_resolver", lambda: (lambda name: f"m::{name}"))
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    mw_names = {type(m).__name__ for m in (kwargs.get("middleware") or [])}
    # planning pins databricks-claude-opus-4-8 -> the primary loop is pinned.
    assert "ModelPinMiddleware" in mw_names


def test_enrich_base_agent_no_top_level_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    mw_names = {type(m).__name__ for m in (kwargs.get("middleware") or [])}
    # No active Manta profile -> no top-level tool-policy enforcement.
    assert "ToolPolicyMiddleware" not in mw_names


def test_delegation_policy_appended_for_base_orchestrator(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    kwargs: dict = {"system_prompt": "You are a coding agent."}
    hook.enrich_kwargs(kwargs)
    assert "Delegating to specialist agents (Manta)" in kwargs["system_prompt"]
    # Base prompt is preserved (append, not replace).
    assert kwargs["system_prompt"].startswith("You are a coding agent.")


def test_delegation_policy_not_appended_when_profile_active(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "swe")
    kwargs: dict = {"system_prompt": "You are a coding agent."}
    hook.enrich_kwargs(kwargs)
    # A specific profile is primary -> no circular "delegate to swe" nudge.
    assert "Delegating to specialist agents (Manta)" not in kwargs["system_prompt"]


def test_delegation_policy_skipped_without_system_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    # No system_prompt (None sentinel) -> never fabricate one; leave it for
    # create_deep_agent to compute its rich default.
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    assert "system_prompt" not in kwargs or kwargs["system_prompt"] is None


def test_delegation_policy_not_duplicated(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    kwargs: dict = {"system_prompt": "base"}
    hook.enrich_kwargs(kwargs)
    hook.enrich_kwargs(kwargs)  # idempotent: applying twice adds it once
    assert kwargs["system_prompt"].count("Delegating to specialist agents (Manta)") == 1


def test_install_is_idempotent_and_wraps(monkeypatch):
    import deepagents_code.agent as dc_agent

    # Reset module + upstream state so the test is self-contained.
    original = dc_agent.create_deep_agent
    monkeypatch.setattr(hook, "_installed", False)
    try:
        assert hook.install_build_hook() is True
        assert getattr(dc_agent.create_deep_agent, "__manta_hook__", False) is True
        wrapped_once = dc_agent.create_deep_agent
        # Second call is a no-op (already installed).
        assert hook.install_build_hook() is True
        assert dc_agent.create_deep_agent is wrapped_once
    finally:
        dc_agent.create_deep_agent = original


def test_wrapped_falls_back_when_enrichment_raises(monkeypatch):
    import deepagents_code.agent as dc_agent

    calls = {"n": 0}

    def fake_original(*args, **kwargs):
        calls["n"] += 1
        return "AGENT"

    original = dc_agent.create_deep_agent
    monkeypatch.setattr(dc_agent, "create_deep_agent", fake_original)
    monkeypatch.setattr(hook, "_installed", False)

    def boom(_kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(hook, "enrich_kwargs", boom)
    try:
        assert hook.install_build_hook() is True
        # Even though enrichment raises, the original still runs and returns.
        result = dc_agent.create_deep_agent(model="m")
        assert result == "AGENT"
        assert calls["n"] == 1
    finally:
        dc_agent.create_deep_agent = original


def test_databricks_tools_skipped_when_not_configured(monkeypatch):
    # Databricks is detect-and-enable (ADR 0010): without a workspace, the
    # UC/SQL/jobs tools are not injected at all.
    from manta_code import auth

    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: False)
    assert hook.build_databricks_tools() == []


def test_databricks_tools_built_when_configured(monkeypatch):
    from manta_code import auth

    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    sentinel = [object()]
    import manta_code.databricks_tools as dbt

    monkeypatch.setattr(dbt, "build_default_databricks_tools", lambda: sentinel)
    assert hook.build_databricks_tools() == sentinel


def test_orchestrator_middleware_includes_agent_addressing(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    mw_names = {type(m).__name__ for m in hook.build_orchestrator_middleware()}
    assert "AgentAddressMiddleware" in mw_names
    # And the status-feed event log rides along.
    assert "EventLogMiddleware" in mw_names


def test_addressing_active_for_manta_profile_too(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "review")
    mw_names = {type(m).__name__ for m in hook.build_orchestrator_middleware()}
    assert "AgentAddressMiddleware" in mw_names


def test_enrich_injects_task_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    names = {getattr(t, "name", "") for t in (kwargs.get("tools") or [])}
    assert "manta_task_submit" in names
    assert "manta_task_status" in names
