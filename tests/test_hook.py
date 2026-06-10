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
