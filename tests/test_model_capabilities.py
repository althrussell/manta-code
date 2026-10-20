from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from manta_code.config import MantaConfig
from manta_code.model_capabilities import (
    capability_summary,
    format_capability_card,
    install_model_capability_bridge,
)
from manta_code.providers.targets import (
    POLICY_ENV,
    capability_evidence_from_dict,
    export_target_policy,
    resolve_target_policy,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _runtime(monkeypatch, names):
    mc = pytest.importorskip("deepagents_code.model_config")
    config = mc.ModelConfig(providers={"databricks": {
        "class_path": "manta_code.databricks_chat:MantaChatDatabricks", "models": names,
    }})
    monkeypatch.setattr(mc.ModelConfig, "load", classmethod(lambda cls: config))
    mc.clear_caches()
    assert install_model_capability_bridge()
    return mc


def test_actual_runtime_efforts_and_limits_share_adapter_evidence(monkeypatch):
    from manta_code.databricks_chat import MantaChatDatabricks
    from deepagents_code import reasoning_effort

    name = "system.ai.gpt-6-1-sol"
    mc = _runtime(monkeypatch, [name])
    entry = mc.get_model_profiles()[f"databricks:{name}"]
    model = MantaChatDatabricks(model=name)
    assert entry["profile"] == model.profile
    assert entry["profile"]["max_input_tokens"] == 1_050_000
    assert entry["profile"]["max_output_tokens"] == 128_000
    assert reasoning_effort.available_efforts_for_model(f"databricks:{name}", None) == (
        "low", "medium", "high", "xhigh", "max",
    )
    assert reasoning_effort.default_effort_for_model(f"databricks:{name}") == "medium"
    assert "context_window" not in model.profile  # Transport metadata is not a LangChain field.


def test_profile_overrides_refresh_without_a_second_cache(monkeypatch):
    from manta_code.databricks_chat import MantaChatDatabricks
    from deepagents_code import reasoning_effort

    name = "custom.team.service"
    mc = _runtime(monkeypatch, [name])
    assert mc.get_model_profiles()[f"databricks:{name}"]["profile"] == {}
    for limit, levels in ((12000, ["low", "high"]), (24000, ["medium"])):
        config = MantaConfig.model_validate({"databricks": {"targets": {name: {"profile": {
            "max_input_tokens": limit, "reasoning_output": True, "reasoning_effort_levels": levels,
        }}}}})
        monkeypatch.setenv(POLICY_ENV, export_target_policy(config))
        profile = mc.get_model_profiles()[f"databricks:{name}"]["profile"]
        assert profile == MantaChatDatabricks(model=name).profile
        assert profile["max_input_tokens"] == limit
        assert reasoning_effort.supported_efforts_for_model(f"databricks:{name}") == tuple(levels)
        assert mc.get_model_profiles(cli_override={"max_input_tokens": 9000})[f"databricks:{name}"]["profile"]["max_input_tokens"] == 9000


def test_nested_effort_change_keeps_siblings_and_removes_conflicting_value(monkeypatch):
    from deepagents_code import reasoning_effort as effort

    _runtime(monkeypatch, ["system.ai.gpt-6-1-sol"])
    spec = "databricks:system.ai.gpt-6-1-sol"
    existing = {"reasoning": {"effort": "high", "summary": "auto"}, "max_tokens": 128}
    assert effort.current_effort_from_model_params(spec, existing) == "high"
    assert effort.has_explicit_effort_model_params(spec, existing)
    updated = effort.with_effort_model_params(spec, existing, "low")
    assert updated == {"reasoning": {"summary": "auto"}, "max_tokens": 128, "reasoning_effort": "low"}
    assert existing["reasoning"]["effort"] == "high"


def test_responses_effort_alias_merges_summary_and_per_call_precedence():
    from manta_code.databricks_chat import MantaChatDatabricks

    model = MantaChatDatabricks(model="system.ai.gpt-6-1-sol",
        reasoning={"summary": "auto", "effort": "high"})
    payload = model._get_request_payload("hello", reasoning_effort="low")
    assert payload["reasoning"] == {"summary": "auto", "effort": "low"}
    assert "reasoning_effort" not in payload
    summary = MantaChatDatabricks(model="system.ai.gpt-6-1-sol",
        reasoning={"summary": "auto"}, reasoning_effort="max")
    assert summary._get_request_payload("hello")["reasoning"] == {"summary": "auto", "effort": "max"}


def test_unknown_exact_target_and_protocol_qualified_facts_remain_honest():
    unknown = capability_summary("custom.team.gpt-6-1-sol")
    assert unknown["values"] == {}
    assert unknown["inference_access"] == "unverified"
    assert "Context: unknown" in format_capability_card("custom.team.gpt-6-1-sol")
    sonnet = resolve_target_policy("system.ai.claude-sonnet-5-5")
    assert sonnet.capabilities.values["forced_tool_choice"] is False
    chat = resolve_target_policy("system.ai.claude-sonnet-5-5", explicit={"api": "chat"})
    assert "forced_tool_choice" not in chat.capabilities.values
    restored = capability_evidence_from_dict(json.loads(json.dumps(asdict(sonnet.capabilities))))
    assert restored == sonnet.capabilities
    assert capability_summary("system.ai.claude-opus-5-5")["qualification_context"]["profile"] == "labs"
    assert "Live evidence (labs)" in format_capability_card("system.ai.claude-opus-5-5")


def test_remote_unconfigured_known_service_has_efforts_but_gemini_failed_route_does_not(monkeypatch):
    from deepagents_code import reasoning_effort
    from manta_code.databricks_chat import MantaChatDatabricks
    from manta_code.providers.targets import TargetResolutionError

    _runtime(monkeypatch, [])
    assert reasoning_effort.supported_efforts_for_model("databricks:system.ai.claude-opus-5-5") == (
        "low", "medium", "high", "xhigh", "max",
    )
    assert reasoning_effort.supported_efforts_for_model("databricks:system.ai.gemini-4-argon") == ()
    with pytest.raises(TargetResolutionError, match="does not support configurable reasoning effort"):
        MantaChatDatabricks(model="system.ai.gemini-4-argon", reasoning_effort="high")._get_request_payload("hello")
    responses = resolve_target_policy("system.ai.gemini-4-argon", explicit={"api": "responses"})
    assert responses.capabilities.profile["reasoning_effort_levels"] == ["low", "medium", "high"]
    # The control override must be explicit, rather than an error-driven retry.
    overridden = MantaChatDatabricks(model="system.ai.gemini-4-argon", reasoning_effort="high",
        profile={"reasoning_effort_levels": ["low", "medium", "high"]})
    assert overridden._get_request_payload("hello")["reasoning_effort"] == "high"


def test_clone_re_resolves_protocol_facts_without_promoting_them_to_override():
    from manta_code.databricks_chat import MantaChatDatabricks

    model = MantaChatDatabricks(model="system.ai.claude-sonnet-5-5")
    chat = model.clone_with_policy(api="chat", surface="unified")
    assert "forced_tool_choice" not in chat.target_policy.capabilities.values
    assert "tool_choice" not in chat.profile
    explicit = MantaChatDatabricks(model="system.ai.claude-sonnet-5-5", profile={"tool_choice": True})
    assert explicit.clone_with_policy(api="chat", surface="unified").profile["tool_choice"] is True


def test_context_preflight_includes_tools_instructions_and_reserved_output():
    from manta_code.providers.context import context_preflight

    profile = {"context_window": 1000, "max_output_tokens": 100}
    base = context_preflight({"input": "hello"}, profile)
    tools = context_preflight({"input": "hello", "instructions": "system " * 100,
        "tools": [{"type": "function", "name": "echo", "description": "detail " * 100}]}, profile)
    assert tools.input_tokens_estimate > base.input_tokens_estimate + 100
    assert tools.reserved_output_tokens == 100
    assert tools.total_tokens_estimate == tools.input_tokens_estimate + 100
    assert tools.estimate_complete


def test_context_guard_uses_effective_cli_override_and_output_reserve():
    from manta_code.databricks_chat import MantaChatDatabricks
    from manta_code.providers.targets import TargetResolutionError

    model = MantaChatDatabricks(model="system.ai.gpt-6-1-sol", max_tokens=80)
    model.profile = {**model.profile, "max_input_tokens": 100}
    with pytest.raises(TargetResolutionError, match="reserved output 80"):
        model._get_request_payload("repeated word " * 30)
    assert model.last_context_preflight["context_window"] == 100
    assert model.last_context_preflight["reserved_output_tokens"] == 80
    model._get_request_payload("hello", max_output_tokens=10)
    unknown_output = MantaChatDatabricks(model="custom.team.service", profile={"max_input_tokens": 100})
    with pytest.raises(TargetResolutionError, match="reserved output unknown"):
        unknown_output._get_request_payload("repeated word " * 100)


def test_media_and_opaque_state_are_unmeasured_without_exposing_payload():
    from manta_code.providers.context import context_preflight

    payload = {"input": [{"type": "input_image", "image_url": "data:image/png;base64," + "x" * 100000},
                         {"type": "reasoning", "encrypted_content": "private-state"}]}
    budget = context_preflight(payload, {})
    assert not budget.estimate_complete
    assert budget.context_window is None
    assert budget.total_tokens_estimate is None
    assert budget.input_tokens_estimate < 100
    assert set(budget.unmeasured) == {"input_image", "opaque continuation state"}
    assert "private-state" not in str(budget.as_dict())


def test_argon_unary_and_streaming_output_caps_differ():
    from manta_code.databricks_chat import MantaChatDatabricks
    from manta_code.providers.targets import TargetResolutionError

    model = MantaChatDatabricks(model="system.ai.gemini-4-argon")
    with pytest.raises(TargetResolutionError, match="limit 32768"):
        model._get_request_payload("hello", max_tokens=32769)
    assert model._get_request_payload("hello", stream=True, max_tokens=65536)["max_tokens"] == 65536


def test_real_effort_selector_selects_documented_gateway_option(monkeypatch):
    from deepagents_code import reasoning_effort as effort
    from deepagents_code.tui.widgets.effort_selector import EffortSelectorScreen
    from textual.app import App

    spec = "databricks:system.ai.gpt-6-1-sol"
    _runtime(monkeypatch, [spec.removeprefix("databricks:")])
    selected = []

    async def render():
        app = App()
        async with app.run_test(size=(100, 35)) as pilot:
            screen = EffortSelectorScreen(model_spec=spec,
                efforts=effort.available_efforts_for_model(spec, None),
                default_effort=effort.default_effort_for_model(spec))
            await app.push_screen(screen, callback=selected.append)
            await pilot.pause()
            await pilot.press("end", "enter")
            await pilot.pause()

    asyncio.run(render())
    assert selected == ["max"]
