from __future__ import annotations

import json
from types import SimpleNamespace as Obj

import pytest

from manta_code.config import MantaConfig
from manta_code.providers.targets import (
    POLICY_ENV,
    CapabilityEvidence,
    TargetResolutionError,
    export_target_policy,
    merge_request_tag_headers,
    parse_target,
    resolve_target_policy,
)


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch, tmp_path):
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path))


@pytest.mark.parametrize("value", ["system.ai.hyphen-underscore_1", "custom_schema.team.model", "model-services/system.ai.exact-name", "databricks:system.ai.exact-name"])
def test_exact_service_identity(value):
    target = parse_target(value)
    expected = value.removeprefix("databricks:").removeprefix("model-services/")
    assert target.name == expected
    assert target.resource_name == f"model-services/{expected}"
    assert target.public_ref == f"databricks:{expected}"


@pytest.mark.parametrize("value", ["", " ", " system.ai.x", "models/system.ai.x", "model-provider-services/system.ai.x", "a..x", "a.b.c/d", "a.b.c:1", "a.b." + "x"*256, "`a`.b.c"])
def test_invalid_target_namespace_or_identifier(value):
    with pytest.raises(TargetResolutionError):
        parse_target(value)


def test_explicit_ambiguous_endpoint_kind():
    target = parse_target("legacy.dotted.endpoint", kind="endpoint")
    assert target.kind == "endpoint"
    assert target.resource_name is None
    policy = resolve_target_policy(target.name, explicit={"target_kind": "endpoint"})
    assert policy.surface == "legacy"


def test_policy_precedence_same_constructor_and_registry(monkeypatch):
    from manta_code.databricks_chat import MantaChatDatabricks
    from manta_code.providers import resolve_model_ref
    cfg = MantaConfig.model_validate({"databricks": {
        "params": {"temperature": .4},
        "targets": {"system.ai.pinned": {"api": "responses", "max_output_tokens": 123, "temperature": .2, "profile": {"max_input_tokens": 12000, "tool_calling": True}}},
        "request_tags": {"application": "manta"},
    }})
    monkeypatch.setenv(POLICY_ENV, export_target_policy(cfg, {"temperature": .9, "timeout": 12}))
    primary = MantaChatDatabricks(model="system.ai.pinned")
    pin = resolve_model_ref("databricks:system.ai.pinned")
    assert pin.target_policy == primary.target_policy
    assert pin.use_responses_api is True
    assert pin.temperature == .2
    assert pin.max_tokens == 123
    assert pin.request_timeout == 12
    assert pin.profile == {"max_input_tokens": 12000, "tool_calling": True}
    assert MantaChatDatabricks(model="system.ai.pinned", temperature=.1).temperature == .1


def test_policy_export_excludes_credentials_and_client_objects():
    cfg = MantaConfig.model_validate({"databricks": {"params": {"api_key": "secret", "model_kwargs": {"token": "secret", "max_tokens": 5}}}})
    data = export_target_policy(cfg, {"default_headers": {"Authorization": "secret"}, "timeout": 3, "client_secret": "secret"})
    assert "secret" not in data
    assert json.loads(data)["provider_params"] == {"timeout": 3}
    assert json.loads(data)["settings"]["params"]["model_kwargs"] == {"max_tokens": 5}


@pytest.mark.parametrize(("apis", "surface", "api"), [
    (("mlflow/v1/responses",), "unified", "responses"),
    (("mlflow/v1/chat/completions", "mlflow/v1/responses"), "unified", "responses"),
    (("mlflow/v1/chat/completions",), "unified", "chat"),
    (("openai/v1/responses",), "openai", "responses"),
    (("openai/v1/chat/completions",), "openai", "chat"),
])
def test_auto_protocol_from_actual_metadata(apis, surface, api):
    evidence = CapabilityEvidence(supported_api_types=apis)
    policy = resolve_target_policy("system.ai.custom", capability=evidence)
    assert (policy.surface, policy.api) == (surface, api)
    assert policy.capabilities.profile == {}  # APIs imply no context/tool limits


def test_explicit_api_unsupported_or_conflicting_options_fail():
    with pytest.raises(TargetResolutionError, match="does not advertise"):
        resolve_target_policy("system.ai.service", explicit={"api": "chat"}, capability=CapabilityEvidence(supported_api_types=("mlflow/v1/responses",)))
    with pytest.raises(TargetResolutionError, match="Contradictory"):
        resolve_target_policy("system.ai.service", explicit={"api": "responses", "use_responses_api": False})
    with pytest.raises(TargetResolutionError, match="requires a Unity"):
        resolve_target_policy("system.ai.service", explicit={"use_ai_gateway": False})
    with pytest.raises(TargetResolutionError, match="model-service FQN"):
        resolve_target_policy("legacy-service", explicit={"use_ai_gateway": True})


def test_tags_validate_and_merge_explicit_header_precedence():
    merged = merge_request_tag_headers({"databricks-ai-gateway-request-tags": '{"a":"user","b":"user"}', "x-custom": "keep"}, {"a": "default", "c": "default"})
    assert json.loads(merged["Databricks-Ai-Gateway-Request-Tags"]) == {"a": "user", "b": "user", "c": "default"}
    assert merged["x-custom"] == "keep"
    for invalid in ('{"a":2}', '[]', 'not json'):
        with pytest.raises(TargetResolutionError):
            merge_request_tag_headers({"Databricks-Ai-Gateway-Request-Tags": invalid}, {})


def test_clone_retains_clients_parameters_and_exact_identity(monkeypatch):
    from manta_code.databricks_chat import MantaChatDatabricks
    workspace = Obj(config=Obj(host="https://fixture.invalid"))
    model = MantaChatDatabricks(model="system.ai.exact", workspace_client=workspace, max_tokens=123, temperature=.2, request_tags={"app": "manta"}, databricks_profile="team")
    copy = model.clone_with_policy(api="responses", surface="unified")
    assert copy.workspace_client is workspace
    assert copy.max_tokens == 123
    assert copy.temperature == .2
    assert copy.target_policy.profile == "team"
    assert copy.transport_api == "responses"
    assert copy.model_name == model.model_name


def test_upstream_first_classpath_import_and_equal_explicit_override(tmp_path, monkeypatch):
    """Exercise actual upstream frames, before this module's class is loaded."""
    import subprocess
    import sys
    cfg = MantaConfig.model_validate({"databricks": {"targets": {"system.ai.pinned": {"api": "responses", "temperature": .2, "profile": {"max_input_tokens": 12000}}}}})
    monkeypatch.setenv(POLICY_ENV, export_target_policy(cfg, {"temperature": .9, "use_responses_api": False}))
    script = '''
import sys
from deepagents_code import config as upstream
from deepagents_code import model_config as mc
assert 'manta_code.databricks_chat' not in sys.modules
config = mc.ModelConfig(providers={'databricks': {'class_path': 'manta_code.databricks_chat:MantaChatDatabricks', 'models': ['system.ai.pinned'], 'params': {'temperature': .9, 'use_responses_api': False}}})
mc.ModelConfig.load = classmethod(lambda cls: config)
mc.has_provider_credentials = lambda *a, **k: True
mc.resolve_provider_credential = lambda *a, **k: None
mc.warn_on_split_credential_source = lambda *a, **k: None
mc.apply_stored_credentials = lambda *a, **k: None
first = upstream.create_model('databricks:system.ai.pinned')
assert first.model.temperature == .2, first.model.temperature
assert first.model.use_responses_api is True
assert first.context_limit == 12000, first.context_limit
second = upstream.create_model('databricks:system.ai.pinned', extra_kwargs={'temperature': .9, 'use_responses_api': False})
assert second.model.temperature == .9, second.model.temperature
assert second.model.use_responses_api is False
'''
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unverified_service_prefers_responses_after_qualification():
    service = resolve_target_policy("custom.team.unverified")
    endpoint = resolve_target_policy("legacy-endpoint")
    assert (service.surface, service.api) == ("unified", "responses")
    assert (endpoint.surface, endpoint.api) == ("legacy", "chat")
    assert service.capabilities.profile == {}


@pytest.mark.parametrize("service", ["system.ai.gemini-3-7-flash", "system.ai.gemini-4-argon"])
def test_exact_gemini_qualification_prefers_chat_and_disables_unsupported_stream_usage(service):
    from manta_code.databricks_chat import MantaChatDatabricks

    model = MantaChatDatabricks(model=service)
    assert model.transport_api == "chat"
    assert model.stream_usage is False
    assert "preferred_api" not in model.profile
    assert MantaChatDatabricks(model=service, api="responses").transport_api == "responses"
    with pytest.raises(TargetResolutionError, match="stream_options"):
        MantaChatDatabricks(model=service, stream_usage=True)
    with pytest.raises(TargetResolutionError, match="stream_options"):
        model._get_request_payload("test", stream=True, stream_options={"include_usage": True})
    assert MantaChatDatabricks(model=service, stream_usage=True, profile={"stream_usage": True}).stream_usage is True
    # Actual API metadata still constrains automatic protocol selection.
    only_responses = resolve_target_policy(service, capability=CapabilityEvidence(supported_api_types=("mlflow/v1/responses",)))
    assert only_responses.api == "responses"
    assert resolve_target_policy("custom.ai.gemini-4-argon").api == "responses"


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_per_call_output_cap_is_preserved_in_both_dialects(dialect):
    from manta_code.databricks_chat import MantaChatDatabricks
    model = MantaChatDatabricks(model="custom.team.service", api=dialect, max_tokens=128)
    payload = model._get_request_payload("test", max_output_tokens=67)
    assert payload["max_output_tokens" if dialect == "responses" else "max_tokens"] == 67
    with pytest.raises(TargetResolutionError, match="Transport options"):
        model._get_request_payload("test", api="responses")
    with pytest.raises(TargetResolutionError, match="Conflicting per-call"):
        model._get_request_payload("test", max_tokens=1, max_output_tokens=2)


def test_unsupported_responses_stop_is_actionable_and_temperature_not_silently_removed():
    from manta_code.databricks_chat import MantaChatDatabricks
    model = MantaChatDatabricks(model="gpt-5-custom-endpoint", api="responses", temperature=.3)
    assert model.temperature == .3
    assert model._get_request_payload("test")["temperature"] == .3
    with pytest.raises(TargetResolutionError, match="does not support stop"):
        model._get_request_payload("test", stop=["done"])


def test_exact_qualified_service_rejects_forced_tools_without_family_heuristics():
    from manta_code.databricks_chat import MantaChatDatabricks
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object", "properties": {}}}}]
    model = MantaChatDatabricks(model="system.ai.claude-sonnet-5-5")
    assert model.profile["tool_calling"] is True
    assert model.profile["tool_choice"] is False
    assert model.target_policy.capabilities.sources["tool_calling"].startswith("Manta labs")
    assert model.bind_tools(tools, tool_choice="auto")
    with pytest.raises(TargetResolutionError, match="does not support forced"):
        model.bind_tools(tools, tool_choice="echo")
    with pytest.raises(TargetResolutionError, match="does not support forced"):
        model._get_request_payload("test", tools=tools, tool_choice="required")
    override = MantaChatDatabricks(model="system.ai.claude-sonnet-5-5", profile={"tool_choice": True})
    assert override.bind_tools(tools, tool_choice="echo")
    # An arbitrary service's capabilities remain unknown; no name heuristic.
    assert MantaChatDatabricks(model="custom.team.claude-sonnet-5-5").bind_tools(tools, tool_choice="echo")


def test_unified_responses_replays_full_stateless_history_and_rejects_stateful_controls():
    from manta_code.databricks_chat import MantaChatDatabricks
    model = MantaChatDatabricks(model="custom.team.service")
    assert model.store is False
    assert model._get_request_payload("test")["store"] is False
    for options in ({"store": True}, {"use_previous_response_id": True}, {"previous_response_id": "resp_fixture"}, {"model_kwargs": {"previous_response_id": "resp_fixture"}}):
        with pytest.raises(TargetResolutionError, match="stateless"):
            MantaChatDatabricks(model="custom.team.service", **options)
    with pytest.raises(TargetResolutionError, match="stateless"):
        model._get_request_payload("test", previous_response_id="resp_fixture")
    with pytest.raises(TargetResolutionError, match="v0 is unsupported"):
        MantaChatDatabricks(model="custom.team.service", output_version="v0")
    # Legacy chat serialization has no Responses state and retains compatibility.
    assert MantaChatDatabricks(model="legacy-endpoint", output_version="v0")


def test_known_capability_options_fail_before_request_and_unknown_stays_permitted():
    from manta_code.databricks_chat import MantaChatDatabricks
    model = MantaChatDatabricks(model="custom.team.service", profile={"temperature": False, "reasoning_effort_levels": ["low", "high"], "max_output_tokens": 64})
    for options, message in [({"temperature": .2}, "temperature disabled"), ({"reasoning_effort": "medium"}, "reasoning effort values"), ({"max_tokens": 65}, "exceeds")]:
        with pytest.raises(TargetResolutionError, match=message):
            model._get_request_payload("test", **options)
    assert model._get_request_payload("test", reasoning_effort="low", max_tokens=64)["max_output_tokens"] == 64
    with pytest.raises(TargetResolutionError, match="unsupported by the responses"):
        model._get_request_payload("test", frequency_penalty=.3)
    unknown = MantaChatDatabricks(model="custom.team.unknown")
    assert unknown._get_request_payload("test", temperature=.2)["temperature"] == .2


@pytest.mark.parametrize('alias', ['model', 'model_name', 'endpoint'])
def test_per_call_target_alias_cannot_bypass_target_policy(alias):
    from manta_code.databricks_chat import MantaChatDatabricks
    model = MantaChatDatabricks(model='custom.team.selected')
    with pytest.raises(TargetResolutionError, match='model constructor'):
        model._get_request_payload('test', **{alias: 'custom.team.other'})


def test_exported_discovery_capability_isolated_from_explicit_other_profile(monkeypatch):
    from manta_code.providers.discovery import DiscoveryResult, TargetInfo
    inventory = DiscoveryResult((TargetInfo(parse_target('custom.team.service'), capabilities=CapabilityEvidence(supported_api_types=('mlflow/v1/chat/completions',))),), (), workspace='https://one.invalid', profile='one')
    monkeypatch.setenv(POLICY_ENV, export_target_policy(MantaConfig(), inventory=inventory))
    one = resolve_target_policy('custom.team.service', explicit={'databricks_profile': 'one'})
    two = resolve_target_policy('custom.team.service', explicit={'databricks_profile': 'two'})
    assert one.api == 'chat'
    assert two.api == 'responses'
    other_workspace = Obj(config=Obj(host='https://other.invalid'))
    assert resolve_target_policy('custom.team.service', explicit={'databricks_profile': 'one', 'workspace_client': other_workspace}).api == 'responses'
