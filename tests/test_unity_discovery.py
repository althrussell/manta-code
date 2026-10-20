from __future__ import annotations

import time
import threading
from types import SimpleNamespace as Obj

import pytest

from manta_code.providers import discovery as d
from manta_code.providers.targets import POLICY_ENV, DatabricksTarget, TargetKind, export_target_policy
from manta_code.config import MantaConfig


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path))
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    d.clear_discovery_cache()


def service(name="system.ai.chat", apis=("mlflow/v1/responses",)):
    return Obj(name=f"model-services/{name}", supported_api_types=list(apis))


def client(services=(), endpoints=(), host="https://one.invalid", token="one", get=None):
    return Obj(
        config=Obj(host=host, token=token),
        ai_gateway=Obj(list_model_services=lambda **kw: iter(services), get_model_service=get or (lambda name: service(name.removeprefix("model-services/")))),
        serving_endpoints=Obj(list=lambda: iter(endpoints), get=lambda name: Obj(name=name, task="llm/v1/chat")),
    )


class Denied(Exception):
    error_code = "PERMISSION_DENIED"


class Missing(Exception):
    error_code = "RESOURCE_DOES_NOT_EXIST"


def test_sources_independent_and_lazy_failure_partial():
    c = client(endpoints=[Obj(name="legacy", task="llm/v1/chat")])
    def lazy(**kw):
        yield service()
        raise Denied("sensitive token should not enter inventory")
    c.ai_gateway.list_model_services = lazy
    result = d.discover_databricks_targets(client=c)
    assert set(result.names) == {"legacy", "system.ai.chat"}
    assert [(o.source, o.status) for o in result.outcomes] == [("legacy", "success"), ("unity", "partial")]
    assert "sensitive" not in str(result)


def test_sdk_follows_empty_intermediate_pages_and_exact_parent():
    from databricks.sdk.service.catalog import AiGatewayAPI
    pages = [
        {"model_services": [], "next_page_token": "second"},
        {"model_services": [{"name": "model-services/system.ai.chat", "supported_api_types": ["mlflow/v1/responses"]}], "next_page_token": "third"},
        {"model_services": []},
    ]
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, dict(kwargs["query"])))
        return pages.pop(0)
    c = client()
    c.ai_gateway = AiGatewayAPI(Obj(do=request, _cfg=Obj(workspace_id=None)))
    result = d.discover_databricks_targets(client=c)
    assert result.names == ("system.ai.chat",)
    assert [call[2].get("page_token") for call in calls] == [None, "second", "third"]
    assert all(call[2]["parent"] == "schemas/system.ai" for call in calls)


def test_schema_partial_denied_and_embedding_only_filtered():
    c = client()
    def listings(**kwargs):
        if kwargs["parent"] == "schemas/private.models":
            raise Denied()
        return iter([service("system.ai.embedding", ("mlflow/v1/embeddings",)), service()])
    c.ai_gateway.list_model_services = listings
    result = d.discover_databricks_targets(client=c, schemas=["system.ai", "private.models"])
    assert result.names == ("system.ai.chat",)
    assert result.find("system.ai.embedding").coding_capable is False
    assert result.outcomes[-1].status == "denied"
    assert result.find("system.ai.chat").capabilities.profile == {}


def test_direct_configured_pin_outside_schema_and_metadata_denial():
    gets = []
    def direct(name):
        gets.append(name)
        if name.endswith("denied"):
            raise Denied()
        return service(name.removeprefix("model-services/"))
    result = d.discover_databricks_targets(client=client(get=direct), configured=["custom.team.pin", "custom.team.denied"])
    assert set(gets) == {"model-services/custom.team.pin", "model-services/custom.team.denied"}
    assert result.find("custom.team.pin").metadata_status == "verified"
    assert result.find("custom.team.denied").metadata_status == "unverified"
    assert "custom.team.denied" in result.names


@pytest.mark.parametrize(("error", "status", "exists"), [(Denied(), "denied", None), (Missing(), "absent", False), (AttributeError(), "unsupported", None), (TimeoutError(), "unavailable", None)])
def test_lookup_does_not_confuse_denial_with_absence(error, status, exists):
    def get(name):
        raise error
    result = d.lookup_databricks_target("system.ai.pin", client=client(get=get))
    assert result.exists is exists
    assert result.outcome.status == status
    assert result.target is None


def test_lookup_preserves_detail_and_exact_resource():
    detail = service("custom.team.pin")
    result = d.lookup_databricks_target("model-services/custom.team.pin", client=client(get=lambda name: detail))
    assert result.exists is True
    assert result.detail is detail
    assert result.target.name == "custom.team.pin"


@pytest.mark.parametrize("slow_auth", [False, True])
def test_direct_lookup_deadline_bounds_auth_and_full_metadata(monkeypatch, slow_auth):
    from manta_code import auth

    release = threading.Event()
    finished = threading.Event()

    def wait():
        try:
            release.wait(1)
            return client() if slow_auth else service()
        finally:
            finished.set()

    if slow_auth:
        monkeypatch.setattr(auth, "resolve_workspace_client", lambda profile=None: wait())
    selected_client = None if slow_auth else client(get=lambda name: wait())
    started = time.monotonic()
    try:
        result = d.lookup_databricks_target("system.ai.pin", client=selected_client, timeout=.02)
        assert time.monotonic() - started < .2
        assert result.target is None and result.exists is None
        assert result.outcome.status == "unavailable"
        assert "deadline" in result.outcome.message
    finally:
        release.set()
        assert finished.wait(1)


def test_cache_workspace_principal_profile_schema_and_refresh_isolation():
    calls = []
    c = client([service()])
    def listing(**kwargs):
        calls.append(kwargs["parent"])
        return iter([service()])
    c.ai_gateway.list_model_services = listing
    first = d.discover_databricks_targets(client=c, profile="one")
    assert first.cached is False
    assert d.discover_databricks_targets(client=c, profile="one").cached is True
    d.discover_databricks_targets(client=c, profile="one", refresh=True)
    c.config.host = "https://two.invalid"
    d.discover_databricks_targets(client=c, profile="one")
    c.config.token = "different-principal"
    d.discover_databricks_targets(client=c, profile="one")
    d.discover_databricks_targets(client=c, profile="two")
    d.discover_databricks_targets(client=c, profile="two", schemas=["custom.team"])
    assert len(calls) == 6
    assert len(d._CACHE) == 5  # refresh replaces the same scoped entry
    assert "different-principal" not in str(list(d._CACHE))


def test_discovery_deadline_retains_partial_and_configured_names():
    c = client()
    def slow(**kw):
        yield service()
        time.sleep(.15)
        yield service("system.ai.later")
    c.ai_gateway.list_model_services = slow
    started = time.monotonic()
    result = d.discover_databricks_targets(client=c, timeout=.02, configured=["custom.team.configured"])
    assert time.monotonic()-started < .1
    assert set(result.names) == {"system.ai.chat", "custom.team.configured"}
    assert any(o.status == "partial" for o in result.outcomes)
    # The immutable snapshot cannot grow when an abandoned page finishes.
    time.sleep(.16)
    assert "system.ai.later" not in result.names


def test_explicit_dotted_endpoint_kind_uses_legacy_direct_get(monkeypatch):
    cfg = MantaConfig.model_validate({"databricks": {"targets": {"custom.team.legacy": {"kind": "endpoint"}}}})
    monkeypatch.setenv(POLICY_ENV, export_target_policy(cfg))
    c = client([service("custom.team.legacy")])
    result = d.discover_databricks_targets(client=c, configured=["custom.team.legacy"])
    assert result.find("custom.team.legacy").target.kind == "endpoint"
    assert len(result.targets) == 2
    lookup = d.lookup_databricks_target("custom.team.legacy", client=c)
    assert lookup.target.target.kind == "endpoint"
    assert lookup.detail.task == "llm/v1/chat"
    assert result.find(DatabricksTarget("custom.team.legacy", TargetKind.SERVICE)).target.kind == "service"


def test_auth_client_construction_is_inside_launch_deadline(monkeypatch):
    from manta_code import auth
    def slow(profile=None):
        time.sleep(.15)
        return client()
    monkeypatch.setattr(auth, "resolve_workspace_client", slow)
    started = time.monotonic()
    result = d.discover_databricks_targets(timeout=.02, configured=["system.ai.configured"])
    assert time.monotonic()-started < .1
    assert result.names == ("system.ai.configured",)
    assert result.outcomes[0].status == "unavailable"


def test_cache_expires_and_is_bounded(monkeypatch):
    cfg = MantaConfig.model_validate({"databricks": {"discovery_cache_seconds": .01}})
    from manta_code import config
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    c = client([service()])
    assert not d.discover_databricks_targets(client=c).cached
    assert d.discover_databricks_targets(client=c).cached
    time.sleep(.02)
    assert not d.discover_databricks_targets(client=c).cached
    monkeypatch.setattr(d, "_CACHE_MAX", 2)
    d.discover_databricks_targets(client=c, profile="two")
    d.discover_databricks_targets(client=c, profile="three")
    assert len(d._CACHE) == 2
