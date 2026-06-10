from __future__ import annotations

import pytest

from manta_code import providers


# --- ModelRef parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "provider", "model"),
    [
        ("databricks:databricks-gpt-5-5", "databricks", "databricks-gpt-5-5"),
        ("anthropic:claude-opus-4-8", "anthropic", "claude-opus-4-8"),
        # Only the first colon splits — gateway routes may contain colons.
        ("gateway:routes:chat/default", "gateway", "routes:chat/default"),
    ],
)
def test_parse_model_ref(spec, provider, model):
    ref = providers.parse_model_ref(spec)
    assert ref is not None
    assert ref.provider == provider
    assert ref.model == model
    assert str(ref) == f"{provider}:{model}"


@pytest.mark.parametrize("spec", [None, "", "bare-endpoint-name", ":", "x:", ":y", 42])
def test_parse_model_ref_rejects_invalid(spec):
    assert providers.parse_model_ref(spec) is None


# --- resolver registry ------------------------------------------------------------


def test_databricks_is_registered_by_default():
    assert providers.DATABRICKS_PROVIDER in providers.registered_providers()
    assert providers.resolver_for(providers.DATABRICKS_PROVIDER) is not None


def test_register_and_resolve_custom_provider():
    providers.register_resolver("test-gw", lambda model: f"resolved::{model}")
    try:
        assert "test-gw" in providers.registered_providers()
        assert providers.resolve_model_ref("test-gw:chat/default") == (
            "resolved::chat/default"
        )
    finally:
        providers._RESOLVERS.pop("test-gw", None)


def test_resolve_model_ref_none_for_unparseable():
    assert providers.resolve_model_ref("no-provider-prefix") is None


def test_resolve_model_ref_guards_resolver_exceptions():
    def _boom(model):
        raise RuntimeError("factory exploded")

    providers.register_resolver("test-broken", _boom)
    try:
        assert providers.resolve_model_ref("test-broken:x") is None
    finally:
        providers._RESOLVERS.pop("test-broken", None)


def test_resolve_model_ref_falls_back_to_langchain(monkeypatch):
    # Unregistered provider + fallback enabled -> langchain's init_chat_model
    # gets the full spec; here stubbed so the test stays offline.
    monkeypatch.setattr(
        providers, "_langchain_fallback", lambda spec: f"langchain::{spec}"
    )
    assert (
        providers.resolve_model_ref("anthropic:claude-opus-4-8")
        == "langchain::anthropic:claude-opus-4-8"
    )
    # fallback=False keeps resolution strictly registry-only.
    assert providers.resolve_model_ref("anthropic:claude-opus-4-8", fallback=False) is None


def test_databricks_factory_builds_manta_chat():
    pytest.importorskip("databricks_langchain")
    from manta_code.databricks_chat import MantaChatDatabricks

    model = providers.resolve_model_ref("databricks:databricks-gpt-5-5")
    assert isinstance(model, MantaChatDatabricks)
    assert model.model == "databricks-gpt-5-5"


def test_databricks_factory_returns_none_on_construction_failure(monkeypatch):
    pytest.importorskip("databricks_langchain")
    import manta_code.databricks_chat as dc

    class _Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("no workspace")

    monkeypatch.setattr(dc, "MantaChatDatabricks", _Exploding)
    assert providers._databricks_factory("databricks-gpt-5-5") is None


# --- integration with the subagent resolver shim ---------------------------------


def test_subagent_resolver_uses_registry_for_registered_providers():
    pytest.importorskip("deepagents")
    import manta_code.databricks_chat as dc

    dc._install_subagent_databricks_resolver()
    from deepagents import _models

    providers.register_resolver("test-gw", lambda model: f"gw::{model}")
    try:
        assert _models.resolve_model("test-gw:chat/default") == "gw::chat/default"
    finally:
        providers._RESOLVERS.pop("test-gw", None)


# --- gateway surface (Phase D) -----------------------------------------------------


class _Obj:
    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


def _fake_detail(name, *, usage=True, rate_limits=False, external=None, foundation=True):
    gw = _Obj(
        usage_tracking_config=_Obj(enabled=True) if usage else None,
        rate_limits=["limit"] if rate_limits else [],
        guardrails=None,
        fallback_config=None,
        inference_table_config=None,
    )
    entity = _Obj(
        foundation_model=object() if foundation else None,
        external_model=_Obj(provider=external) if external else None,
    )
    return _Obj(
        name=name, ai_gateway=gw, config=_Obj(served_entities=[entity])
    )


def test_gateway_inspect_foundation_endpoint():
    from manta_code.providers.gateway import _inspect_endpoint

    info = _inspect_endpoint(_fake_detail("databricks-gpt-5-5", rate_limits=True))
    assert info.gateway_governed
    assert info.features == ("usage-tracking", "rate-limits")
    assert info.external_provider is None
    assert info.source == "databricks foundation model"


def test_gateway_inspect_external_endpoint():
    from manta_code.providers.gateway import _inspect_endpoint

    info = _inspect_endpoint(
        _fake_detail("my-anthropic", external="anthropic", foundation=False)
    )
    assert info.external_provider == "anthropic"
    assert "external, via gateway" in info.source


def test_gateway_surface_providers_and_governance():
    from manta_code.providers.gateway import GatewaySurface, _inspect_endpoint

    surface = GatewaySurface(
        endpoints=[
            _inspect_endpoint(_fake_detail("databricks-gpt-5-5")),
            _inspect_endpoint(_fake_detail("ext-oai", external="openai", foundation=False)),
            _inspect_endpoint(_fake_detail("bare", usage=False)),
        ]
    )
    assert surface.providers == ["databricks", "openai"]
    assert len(surface.governed) == 2
    assert [e.name for e in surface.external] == ["ext-oai"]


def test_gateway_discovery_empty_on_auth_failure(monkeypatch):
    from manta_code.providers import gateway as G
    from manta_code import auth

    def _boom(profile=None):
        raise RuntimeError("no sdk")

    monkeypatch.setattr(auth, "resolve_workspace_client", _boom)
    assert G.discover_gateway_surface().endpoints == []
