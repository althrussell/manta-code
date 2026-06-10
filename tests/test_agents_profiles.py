from __future__ import annotations

from manta_code.agents.profiles import (
    MANTA_PROFILE_SENTINEL,
    clean_legacy_subagents,
    render_profile_md,
    sync_agent_profiles,
)
from manta_code.agents.registry import AgentDef


def _agent(name: str, prompt: str = "do the thing") -> AgentDef:
    return AgentDef(name=name, description=f"{name} agent", system_prompt=prompt)


def test_render_profile_md_carries_sentinel_and_prompt():
    md = render_profile_md(_agent("planning", "Plan carefully."))
    assert md.startswith(MANTA_PROFILE_SENTINEL)
    assert "Plan carefully." in md
    assert md.endswith("\n")


def test_sync_writes_profiles_and_skips_base_agent(tmp_path):
    state = tmp_path / "state.json"
    agents = [_agent("planning"), _agent("swe"), _agent("agent")]
    result = sync_agent_profiles(agents, deepagents_dir=tmp_path, state_path=state)

    assert set(result.written) == {"planning", "swe"}
    # The base 'agent' profile is never managed.
    assert "agent" not in result.written
    assert not (tmp_path / "agent").exists()
    for name in ("planning", "swe"):
        md = tmp_path / name / "AGENTS.md"
        assert md.is_file()
        assert MANTA_PROFILE_SENTINEL in md.read_text(encoding="utf-8")


def test_sync_is_idempotent(tmp_path):
    state = tmp_path / "state.json"
    agents = [_agent("planning")]
    sync_agent_profiles(agents, deepagents_dir=tmp_path, state_path=state)
    second = sync_agent_profiles(agents, deepagents_dir=tmp_path, state_path=state)
    # Unchanged content -> nothing rewritten on the second pass.
    assert second.written == []


def test_sync_regenerates_on_prompt_change(tmp_path):
    state = tmp_path / "state.json"
    sync_agent_profiles([_agent("planning", "v1")], deepagents_dir=tmp_path, state_path=state)
    result = sync_agent_profiles(
        [_agent("planning", "v2")], deepagents_dir=tmp_path, state_path=state
    )
    assert "planning" in result.written
    assert "v2" in (tmp_path / "planning" / "AGENTS.md").read_text(encoding="utf-8")


def test_sync_skips_non_manta_profile(tmp_path):
    state = tmp_path / "state.json"
    # A user-authored profile of the same name (no sentinel) must be preserved.
    user_dir = tmp_path / "planning"
    user_dir.mkdir()
    (user_dir / "AGENTS.md").write_text("hand written, do not touch", encoding="utf-8")

    result = sync_agent_profiles([_agent("planning")], deepagents_dir=tmp_path, state_path=state)
    assert "planning" in result.skipped
    assert "planning" not in result.written
    assert (user_dir / "AGENTS.md").read_text(encoding="utf-8") == "hand written, do not touch"


def test_sync_prunes_deleted_agents(tmp_path):
    state = tmp_path / "state.json"
    sync_agent_profiles(
        [_agent("planning"), _agent("scratch")], deepagents_dir=tmp_path, state_path=state
    )
    assert (tmp_path / "scratch").exists()

    # 'scratch' removed from the registry -> its managed profile is pruned.
    result = sync_agent_profiles([_agent("planning")], deepagents_dir=tmp_path, state_path=state)
    assert "scratch" in result.pruned
    assert not (tmp_path / "scratch").exists()
    assert (tmp_path / "planning").exists()


def test_prune_leaves_user_profile_of_deleted_name(tmp_path):
    state = tmp_path / "state.json"
    sync_agent_profiles([_agent("scratch")], deepagents_dir=tmp_path, state_path=state)
    # User replaces the managed file with their own (sentinel removed).
    (tmp_path / "scratch" / "AGENTS.md").write_text("mine now", encoding="utf-8")

    result = sync_agent_profiles([], deepagents_dir=tmp_path, state_path=state)
    assert "scratch" not in result.pruned
    assert (tmp_path / "scratch" / "AGENTS.md").read_text(encoding="utf-8") == "mine now"


def test_clean_legacy_subagents_removes_unmodified(tmp_path):
    marker = tmp_path / ".state" / "cleaned"
    legacy = tmp_path / "agent" / "agents" / "planning"
    legacy.mkdir(parents=True)
    (legacy / "AGENTS.md").write_text(
        "---\nname: planning\n---\nYou are Manta's planning specialist ...",
        encoding="utf-8",
    )
    removed = clean_legacy_subagents(deepagents_dir=tmp_path, marker_path=marker)
    assert removed == ["planning"]
    assert not legacy.exists()
    assert marker.exists()
    # Second run is a no-op (marker gates it).
    assert clean_legacy_subagents(deepagents_dir=tmp_path, marker_path=marker) == []


def test_clean_legacy_subagents_leaves_user_edited(tmp_path):
    marker = tmp_path / ".state" / "cleaned"
    legacy = tmp_path / "agent" / "agents" / "swe"
    legacy.mkdir(parents=True)
    (legacy / "AGENTS.md").write_text("my own swe agent", encoding="utf-8")
    removed = clean_legacy_subagents(deepagents_dir=tmp_path, marker_path=marker)
    assert removed == []
    assert legacy.exists()
