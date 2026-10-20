"""Unity user surfaces retain metadata uncertainty and qualify tool replay."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as Obj

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from typer.testing import CliRunner

from manta_code import auth, diagnostics
from manta_code.main import app
from manta_code.providers import discovery, gateway
from manta_code.providers.discovery import DiscoveryOutcome, DiscoveryResult, TargetInfo, TargetLookup
from manta_code.providers.targets import CapabilityEvidence, parse_target

runner = CliRunner()


@pytest.fixture(autouse=True)
def local_manta_home(tmp_path, monkeypatch):
    from manta_code.agents.usage import clear_pricing_cache

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    clear_pricing_cache()
    yield
    clear_pricing_cache()


def service(name="system.ai.test", *, apis=("mlflow/v1/responses",), config=None):
    return Obj(name="model-services/" + name, supported_api_types=apis, config=config)


def test_gateway_service_posture_visible_without_inventing_provider():
    config = Obj(
        rate_limits=[object()], inference_table=object(),
        routing=Obj(destinations=[Obj(name="primary", traffic_percentage=100)],
                    fallback=Obj(destinations=[Obj(name="backup")])),
    )
    info = gateway._inspect_service(service(config=config))
    assert info.name == "system.ai.test"
    assert info.kind == "service"
    assert info.features == ("rate-limits", "inference-tables", "fallbacks")
    assert info.destinations == ("primary (100%)",)
    assert info.fallback_destinations == ("backup",)
    assert info.external_provider is None


def test_gateway_absent_configuration_is_unknown():
    info = gateway._inspect_service(service())
    assert info.governance == "unknown (no visible policy)"
    assert not info.gateway_governed


def test_gateway_explicit_service_get_is_full_and_single_request(monkeypatch):
    calls = []

    def get_model_service(*, name):
        calls.append(name)
        return service("custom.schema.outside")

    client = Obj(ai_gateway=Obj(get_model_service=get_model_service))
    monkeypatch.setattr(auth, "resolve_workspace_client", lambda profile=None: client)
    result = gateway.discover_gateway_surface(names=["custom.schema.outside"])
    assert calls == ["model-services/custom.schema.outside"]
    assert result.endpoints[0].supported_api_types == ("mlflow/v1/responses",)


def test_gateway_denied_metadata_retains_target(monkeypatch):
    class Denied(Exception):
        status_code = 403

    def denied(**kwargs):
        raise Denied("authorization=must-not-leak")

    monkeypatch.setattr(auth, "resolve_workspace_client", lambda profile=None: Obj(
        ai_gateway=Obj(get_model_service=denied),
    ))
    result = gateway.discover_gateway_surface(names=["system.ai.private"])
    assert result.endpoints[0].name == "system.ai.private"
    assert result.endpoints[0].metadata_status == "denied"
    assert "must-not-leak" not in repr(result)
    assert "unknown" in result.endpoints[0].governance


def test_gateway_auth_setup_is_bounded(monkeypatch):
    import threading
    import time

    from manta_code import config

    release, finished = threading.Event(), threading.Event()

    def slow(profile=None):
        try:
            release.wait(1)
            return Obj()
        finally:
            finished.set()

    settings = config.MantaConfig.model_validate({"databricks": {"discovery_timeout_seconds": .02}})
    monkeypatch.setattr(config, "load_config", lambda: settings)
    monkeypatch.setattr(auth, "resolve_workspace_client", slow)
    started = time.monotonic()
    try:
        result = gateway.discover_gateway_surface(names=["system.ai.private"])
        assert time.monotonic() - started < .2
        assert result.outcomes[0].source == "authentication"
        assert result.outcomes[0].status == "unavailable"
    finally:
        release.set()
        assert finished.wait(1)


def test_gateway_cli_target_outside_inventory(monkeypatch):
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    seen = {}

    def inspect(profile, **kwargs):
        seen.update(profile=profile, **kwargs)
        return gateway.GatewaySurface([gateway._inspect_service(service("custom.schema.outside"))])

    monkeypatch.setattr(gateway, "discover_gateway_surface", inspect)
    result = runner.invoke(app, ["-p", "labs", "gateway", "--target", "custom.schema.outside"])
    assert result.exit_code == 0, result.stdout
    assert seen["profile"] == "labs"
    assert seen["names"] == ["custom.schema.outside"]
    assert "Unity Catalog" in result.stdout
    assert "unknown" in result.stdout


@pytest.mark.parametrize("status,exists,expected", [
    ("denied", None, 0), ("unsupported", None, 0), ("unavailable", None, 0),
    ("absent", False, 1), ("success", True, 0),
])
def test_pin_verify_distinguishes_absence_and_uncertain_metadata(tmp_path, monkeypatch, status, exists, expected):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    seen = []

    def lookup(name, **kwargs):
        seen.append(name)
        return TargetLookup(TargetInfo(parse_target(name)) if exists else None,
                            DiscoveryOutcome("unity", status), exists)

    monkeypatch.setattr(discovery, "lookup_databricks_target", lookup)
    result = runner.invoke(app, ["agents", "set-model", "review", "custom.schema.outside"])
    assert result.exit_code == expected, result.stdout
    assert seen == ["custom.schema.outside"]
    if exists is None:
        assert "unverified" in result.stdout
    if exists is False:
        assert "not found" in result.stdout


def test_pin_verify_respects_explicit_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    seen = []

    def lookup(name, **kwargs):
        seen.append(kwargs["profile"])
        return TargetLookup(None, DiscoveryOutcome("unity", "denied"), None)

    monkeypatch.setattr(discovery, "lookup_databricks_target", lookup)
    result = runner.invoke(app, ["-p", "labs", "agents", "set-model", "review", "custom.schema.outside"])
    assert result.exit_code == 0, result.stdout
    assert seen == ["labs"]


def test_credentials_do_not_require_current_user(monkeypatch):
    def forbidden():
        pytest.fail("identity API was called")

    monkeypatch.setattr(auth, "resolve_workspace_client", lambda profile=None: Obj(
        config=Obj(authenticate=lambda: {"Authorization": "Bearer SECRET"}),
        current_user=Obj(me=forbidden),
    ))
    result = diagnostics.credential_context("labs")
    assert "SDK credentials ready" in result
    assert "SECRET" not in result
    assert "unverified" in result


@pytest.mark.parametrize("slow_auth", [False, True])
def test_credential_setup_deadline_includes_sdk_auth(monkeypatch, slow_auth):
    import threading
    import time

    release, finished = threading.Event(), threading.Event()

    def wait():
        try:
            release.wait(1)
            return {"Authorization": "Bearer SECRET"}
        finally:
            finished.set()

    def client(profile=None):
        if slow_auth:
            wait()
        return Obj(config=Obj(authenticate=(lambda: {"Authorization": "Bearer SECRET"}) if slow_auth else wait))

    monkeypatch.setattr(auth, "resolve_workspace_client", client)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            diagnostics.credential_context("labs", timeout_seconds=.02)
        assert time.monotonic() - started < .2
    finally:
        release.set()
        assert finished.wait(1)


def test_upstream_legacy_default_migration_is_read_only(tmp_path):
    path = tmp_path / "config.toml"
    text = '[models]\ndefault = "databricks:old-endpoint"\n'
    path.write_text(text)
    model, detail = diagnostics.upstream_default_diagnostic(path)
    assert model == "databricks:old-endpoint"
    assert "legacy" in detail
    assert path.read_text() == text


def test_probe_uses_metadata_protocol_and_tool_roundtrip(monkeypatch):
    from manta_code import databricks_chat

    calls = []
    constructed = []

    class Model:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def bind_tools(self, tools, **kwargs):
            calls.append(("bind", kwargs))
            return self

        def invoke(self, messages):
            calls.append(("invoke", messages))
            if len(messages) == 1:
                nonce = messages[0].content.split("with value ", 1)[1].split(".", 1)[0]
                return AIMessage(content=[{"type": "reasoning", "id": "opaque", "encrypted_content": "STATE"}],
                                 tool_calls=[{"name": "manta_probe_echo", "args": {"value": nonce}, "id": "call1", "type": "tool_call"}])
            assert messages[1].content[0]["encrypted_content"] == "STATE"
            assert isinstance(messages[2], ToolMessage)
            return AIMessage(content=messages[2].content)

    capability = CapabilityEvidence(supported_api_types=("mlflow/v1/responses",))
    monkeypatch.setattr(discovery, "lookup_databricks_target", lambda *a, **k: TargetLookup(
        TargetInfo(parse_target("system.ai.test"), capabilities=capability), DiscoveryOutcome("unity", "success"), True,
    ))
    monkeypatch.setattr(databricks_chat, "MantaChatDatabricks", Model)
    result = diagnostics.probe_databricks_target("system.ai.test", profile="labs")
    assert result.ok, result
    assert result.calls == 2
    assert result.protocol == "responses"
    assert constructed[0]["use_responses_api"] is True
    assert constructed[0]["databricks_profile"] == "labs"
    assert constructed[0]["max_retries"] == 0
    assert len([c for c in calls if c[0] == "invoke"]) == 2


def test_probe_failure_does_not_guess_protocol_or_retry(monkeypatch):
    from manta_code import databricks_chat

    invocations = []

    class Model:
        def __init__(self, **kwargs):
            pass

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            invocations.append(messages)
            raise RuntimeError("only supports Responses API; authorization=SECRET")

    monkeypatch.setattr(discovery, "lookup_databricks_target", lambda *a, **k: TargetLookup(
        None, DiscoveryOutcome("legacy", "denied"), None,
    ))
    monkeypatch.setattr(databricks_chat, "MantaChatDatabricks", Model)
    result = diagnostics.probe_databricks_target("legacy-chat")
    assert not result.ok and result.protocol == "chat"
    assert result.calls == len(invocations) == 1
    assert result.detail == "RuntimeError"


def test_doctor_no_probe_never_calls_model_or_identity(monkeypatch, tmp_path):
    import manta_code.dcode as dcode
    from manta_code.config import MantaConfig
    from manta_code import main as main_module

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path))
    monkeypatch.setattr(main_module, "load_config", lambda: MantaConfig())
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *a, **k: Path("/tmp/unit-config"))
    monkeypatch.setattr(dcode, "mark_onboarding_complete", lambda: None)
    monkeypatch.setattr(diagnostics, "credential_context", lambda profile=None: "SDK ready; inference unverified")
    monkeypatch.setattr(diagnostics, "probe_databricks_target", lambda *a, **k: pytest.fail("inference probe called"))
    monkeypatch.setattr(discovery, "discover_databricks_targets", lambda *a, **k: DiscoveryResult((), (
        DiscoveryOutcome("unity", "denied", "system.ai"), DiscoveryOutcome("legacy", "unsupported"),
    )))
    monkeypatch.setattr(discovery, "lookup_databricks_target", lambda *a, **k: TargetLookup(
        None, DiscoveryOutcome("unity", "denied"), None,
    ))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "inconclusive" in result.stdout
    assert "unverified" in result.stdout
    assert "not found" not in result.stdout


def test_cost_and_receipts_show_partial_known_subtotals(tmp_path, monkeypatch):
    from manta_code.agents import usage

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    usage.record_usage(usage.UsageRecord(
        agent="swe", model="legacy-priced", input_tokens=100, output_tokens=20,
        cost_usd=0.25,
    ))
    usage.record_usage(usage.UsageRecord(
        agent="swe", model="custom.schema.unknown", input_tokens=300, output_tokens=40,
        cost_usd=None,
    ))
    result = runner.invoke(app, ["cost"])
    assert result.exit_code == 0, result.stdout
    assert "$0.2500 known subtotal" in result.stdout
    assert "1/2 calls priced" in result.stdout
    assert "Unknown charges are excluded" in result.stdout
    receipts = runner.invoke(app, ["receipts"])
    assert receipts.exit_code == 0, receipts.stdout
    assert "unavailable (pricing/usage incomplete)" in receipts.stdout


def test_launch_exports_only_nonsecret_inventory_capabilities(monkeypatch):
    import json

    from manta_code import dcode

    info = TargetInfo(
        parse_target("custom.schema.service"),
        capabilities=CapabilityEvidence(
            values={"max_input_tokens": 12345, "tool_calling": True},
            sources={"max_input_tokens": "explicit override"},
            supported_api_types=("mlflow/v1/responses",),
        ),
    )
    inventory = DiscoveryResult((info,), (), workspace="https://workspace.invalid", profile="labs")
    env = dcode.build_launch_env("labs", base_env={}, inventory=inventory,
                                 provider_params={"token": "SECRET", "temperature": 0.2})
    payload = json.loads(env["MANTA_DATABRICKS_POLICY"])
    assert payload["capabilities"]["service:custom.schema.service"]["values"]["max_input_tokens"] == 12345
    assert payload["capabilities"]["service:custom.schema.service"]["supported_api_types"] == ["mlflow/v1/responses"]
    assert payload["provider_params"] == {"temperature": 0.2}
    assert "SECRET" not in env["MANTA_DATABRICKS_POLICY"]
    assert "workspace.invalid" not in env["MANTA_DATABRICKS_POLICY"]


def test_direct_launch_inventory_includes_exact_explicit_model(monkeypatch):
    from manta_code import dcode

    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    seen = {}

    def discover(profile=None, **kwargs):
        seen.update(profile=profile, **kwargs)
        return DiscoveryResult((), ())

    monkeypatch.setattr(discovery, "discover_databricks_targets", discover)
    dcode._target_inventory("labs", ["-M", "databricks:custom.schema.outside"])
    assert seen["profile"] == "labs"
    assert "custom.schema.outside" in seen["configured"]


def test_workspace_refresh_includes_current_model_and_private_agent_pins(monkeypatch):
    import json

    from manta_code import _boot, dcode
    from manta_code.agents import defaults, registry
    from manta_code.agents.registry import AgentDef

    pin = "custom.team.private-agent"
    current = "custom.team.current-model"
    seen = {}

    monkeypatch.delenv("MANTA_DATABRICKS_POLICY", raising=False)
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: True)
    monkeypatch.setattr(registry, "list_agents", lambda: [])
    monkeypatch.setattr(defaults, "merged_agents", lambda agents: [
        AgentDef(name="private", model="databricks:" + pin),
    ])

    def discover(profile=None, **kwargs):
        seen.update(profile=profile, **kwargs)
        return DiscoveryResult((TargetInfo(
            parse_target(pin), capabilities=CapabilityEvidence(
                supported_api_types=("mlflow/v1/chat/completions",),
            ),
        ),), ())

    monkeypatch.setattr(discovery, "discover_databricks_targets", discover)
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(dcode, "databricks_provider_params", lambda: {})
    _boot._refresh_workspace_inventory("new-workspace", "databricks:" + current)
    assert seen["profile"] == "new-workspace"
    assert seen["refresh"] is True
    assert {pin, current} <= set(seen["configured"])
    import os

    payload = json.loads(os.environ["MANTA_DATABRICKS_POLICY"])
    assert payload["capabilities"]["service:" + pin]["supported_api_types"] == [
        "mlflow/v1/chat/completions",
    ]
