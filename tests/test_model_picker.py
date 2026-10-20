"""Regress the real catalog-backed Textual picker shipped by Deep Agents."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from manta_code import _boot, dcode


@pytest.fixture
def gateway_picker(monkeypatch, tmp_path):
    selector = pytest.importorskip("deepagents_code.tui.widgets.model_selector")
    model_config = pytest.importorskip("deepagents_code.model_config")
    model_catalog = pytest.importorskip("deepagents_code.model_catalog")
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setattr(dcode, "DEEPAGENTS_CONFIG_PATH", tmp_path / "deepagents.toml")
    # Preserve descriptors, including the upstream staticmethod, around the
    # installer so these tests exercise the actual selector without global
    # monkeypatches leaking into other runtime tests.
    for name in (
        "__init__", "compose", "_catalog_model_data", "_apply_subset",
        "_info_line_content", "_help_text", "_update_display", "action_select",
        "_update_footer",
    ):
        monkeypatch.setattr(selector.ModelSelectorScreen, name,
                            inspect.getattr_static(selector.ModelSelectorScreen, name))
    monkeypatch.setattr(selector, "_RECOMMENDED_MODELS", selector._RECOMMENDED_MODELS)
    monkeypatch.setattr(model_config, "get_available_models", model_config.get_available_models)
    monkeypatch.setattr(model_catalog, "get_available_models", model_catalog.get_available_models)
    monkeypatch.setattr(_boot, "_original_get_available_models", _boot._original_get_available_models)
    assert _boot.extend_recommended_models()
    assert _boot.prefer_databricks_models()
    assert _boot.rebrand_model_selector_footer()
    return selector


def _catalog(*, models=None, allowed_models=None, current_spec=None):
    from deepagents_code.model_catalog import CatalogProfile, CatalogProvider, ModelCatalog
    from deepagents_code.model_config import ProviderAuthState

    models = models or [
        "openai:gpt-6-astra", "anthropic:claude-opus-5",
        "databricks:system.ai.old-workspace-only",
    ]
    return ModelCatalog(
        models=models,
        profiles={
            spec: CatalogProfile(profile={"name": "Collision"}, overridden_keys=[])
            for spec in models
        },
        providers={
            "openai": CatalogProvider(state=ProviderAuthState.MISSING, env_var="OPENAI_API_KEY"),
            "anthropic": CatalogProvider(state=ProviderAuthState.MISSING, env_var="ANTHROPIC_API_KEY"),
            "databricks": CatalogProvider(state=ProviderAuthState.UNKNOWN),
        },
        allowed_models=allowed_models,
        managed_policy=allowed_models is not None,
        current_spec=current_spec,
    )


def test_catalog_uses_active_inventory_and_preserves_policy(monkeypatch):
    pytest.importorskip("deepagents_code.model_catalog")
    names = ("system.ai.shared", "team_a.models.shared", "team_b.models.shared", "legacy-chat")
    monkeypatch.setattr(_boot, "_gateway_picker_inventory", lambda *_: SimpleNamespace(names=names))
    catalog = _catalog(
        allowed_models=["databricks:team_a.models.shared", "openai:gpt-6-astra"],
        current_spec="openai:gpt-6-astra",
    )
    actual = _boot._gateway_model_catalog(catalog)
    assert actual.models == ["databricks:team_a.models.shared"]
    assert set(actual.providers) == {"databricks"}
    assert actual.providers["databricks"].env_var is None
    assert actual.providers["databricks"].install_extra is None
    assert actual.current_spec is None
    assert actual.profiles == {}
    assert actual.allowed_models == catalog.allowed_models
    assert actual.managed_policy
    # The upstream result is shared presentation data, so never mutate it.
    assert "openai:gpt-6-astra" in catalog.models
    assert "openai" in catalog.providers


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_discovery_failure_never_reuses_another_workspace_catalog(monkeypatch, failure):
    pytest.importorskip("deepagents_code.model_catalog")

    def inventory(*_):
        if failure == "exception":
            raise RuntimeError("discovery unavailable")
        return None

    monkeypatch.setattr(_boot, "_gateway_picker_inventory", inventory)
    monkeypatch.setattr(_boot, "_gateway_picker_pins", lambda *_: ("team.models.explicit-pin",))
    actual = _boot._gateway_model_catalog(_catalog())
    assert actual.models == ["databricks:team.models.explicit-pin"]
    assert "databricks:system.ai.old-workspace-only" not in actual.models


def test_inventory_uses_selected_profile_and_only_intentional_pins(monkeypatch, tmp_path):
    from manta_code.agents import defaults, registry
    from manta_code import config

    monkeypatch.setenv("MANTA_PROFILE", "workspace-b")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "workspace-a")
    path = tmp_path / "deepagents.toml"
    path.write_text('''
[models.providers.databricks]
models = ["system.ai.workspace-a-only", "team.models.user-pin"]
[manta.databricks_inventory]
version = 1
models = ["system.ai.workspace-a-only"]
''')
    monkeypatch.setattr(dcode, "DEEPAGENTS_CONFIG_PATH", path)
    monkeypatch.setattr(config, "interactive_endpoints", lambda *_: ["system.ai.configured"])
    monkeypatch.setattr(defaults, "merged_agents", lambda *_: [SimpleNamespace(model="databricks:team.models.agent-pin")])
    monkeypatch.setattr(registry, "list_agents", lambda: [])
    calls = []

    def discover(profile, args):
        calls.append((profile, args))
        return SimpleNamespace(names=("system.ai.workspace-b-only",))

    monkeypatch.setattr(dcode, "_target_inventory", discover)
    actual = _boot._gateway_picker_inventory("databricks:team.models.current")
    profile, args = calls[0]
    assert profile == "workspace-b"
    assert args == [
        "-M", "databricks:team.models.current",
        "-M", "databricks:system.ai.configured",
        "-M", "databricks:team.models.agent-pin",
        "-M", "databricks:team.models.user-pin",
    ]
    assert actual.names == ("system.ai.workspace-b-only",)


@pytest.mark.parametrize("server_backed", [False, True])
def test_real_selector_lists_full_gateway_inventory_and_selects_exact_fqn(
    gateway_picker, monkeypatch, server_backed,
):
    from deepagents_code import model_catalog, model_config
    from deepagents_code.model_config import ModelConfig, ProviderAuthState, ProviderAuthStatus
    from textual.app import App
    from textual.widgets import Input, Static

    selector = gateway_picker
    names = ("system.ai.shared", "team_a.models.shared", "team_b.models.shared", "legacy-chat")
    monkeypatch.setattr(_boot, "_gateway_picker_inventory", lambda *_: SimpleNamespace(names=names))
    raw_catalog = _catalog(current_spec="openai:gpt-6-astra")
    requests = []

    async def remote_catalog(recommendations, current_spec):
        requests.append((recommendations, current_spec))
        return raw_catalog

    if not server_backed:
        # Exercise the upstream local _load_model_data -> load_model_catalog
        # path, including its eager addition of known direct-provider metadata.
        monkeypatch.setattr(model_catalog, "get_available_models", lambda: {
            "databricks": ["system.ai.old-workspace-only"], "openai": ["gpt-6-astra"],
        })
        monkeypatch.setattr(model_catalog, "get_model_profiles", lambda **_: {})
        monkeypatch.setattr(model_config.ModelConfig, "load", classmethod(lambda cls: ModelConfig()))
        monkeypatch.setattr(model_catalog, "get_provider_auth_status", lambda provider: ProviderAuthStatus(
            state=ProviderAuthState.MANAGED if provider == "databricks" else ProviderAuthState.MISSING,
            provider=provider,
        ))

    selected = []

    class Harness(App):
        pass

    async def render():
        app = Harness()
        async with app.run_test(size=(110, 42)) as pilot:
            screen = selector.ModelSelectorScreen(
                current_model="gpt-6-astra", current_provider="openai",
                default_scope=None, include_recent_models=False,
                catalog_loader=remote_catalog if server_backed else None,
                result_callback=selected.append,
            )
            await app.push_screen(screen)
            await pilot.pause()
            assert screen._loaded
            expected = {f"databricks:{name}" for name in names}
            assert set(dict(screen._filtered_models)) == expected
            assert {option.provider for option in screen._option_widgets} == {"databricks"}
            assert set(screen._catalog.providers) == {"databricks"}
            assert screen._show_specs
            assert "All Databricks AI Gateway models" in str(screen.query_one("#model-selector-info", Static).content)
            assert "gateway models" in screen.query_one("#model-filter", Input).placeholder
            assert "Ctrl+R" not in screen._help_text()
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert set(dict(screen._filtered_models)) == expected
            screen.query_one("#model-filter", Input).value = "team_b.models.shared"
            await pilot.pause()
            assert screen._filtered_models[0] == ("databricks:team_b.models.shared", "databricks")
            await pilot.press("enter")
            await pilot.pause()
            assert selected == [("databricks:team_b.models.shared", "databricks")]
            # A gateway selection authenticates through the workspace, and
            # never pushes the upstream API-key or provider-install modal.
            assert len(app.screen_stack) == 1

    asyncio.run(render())
    if server_backed:
        assert requests
        assert all(spec.startswith("databricks:") for spec in requests[0][0])


def test_typed_direct_provider_cannot_open_an_api_key_prompt(gateway_picker, monkeypatch):
    from textual.app import App
    from textual.widgets import Input

    monkeypatch.setattr(_boot, "_gateway_picker_inventory", lambda *_: SimpleNamespace(names=("system.ai.gateway-only",)))
    selected = []

    async def load_catalog(*_):
        return _catalog()

    async def render():
        app = App()
        async with app.run_test(size=(110, 42)) as pilot:
            screen = gateway_picker.ModelSelectorScreen(
                default_scope=None, include_recent_models=False,
                catalog_loader=load_catalog, result_callback=selected.append,
            )
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#model-filter", Input).value = "openai:gpt-6-astra"
            await pilot.pause()
            assert not screen._filtered_models
            await pilot.press("enter")
            await pilot.pause()
            assert not selected
            assert app.screen is screen
            assert len(app.screen_stack) == 2
            assert "Direct API providers are outside" in str(screen._options_container.children[0].content)
            # An explicit service remains usable when metadata permission is
            # unavailable, without inferring a direct vendor from its name.
            screen.query_one("#model-filter", Input).value = "team.models.infer-only"
            await pilot.pause()
            assert not screen._filtered_models
            await pilot.press("enter")
            await pilot.pause()
            assert selected == [("databricks:team.models.infer-only", "databricks")]

    asyncio.run(render())


def test_gateway_picker_installation_is_idempotent(gateway_picker):
    first_init = gateway_picker.ModelSelectorScreen.__init__
    first_footer = gateway_picker.ModelSelectorScreen._update_footer
    assert _boot.prefer_databricks_models()
    assert _boot.rebrand_model_selector_footer()
    assert gateway_picker.ModelSelectorScreen.__init__ is first_init
    assert gateway_picker.ModelSelectorScreen._update_footer is first_footer


def test_catalog_conversion_removes_direct_and_stale_personal_rows(gateway_picker, monkeypatch):
    good = "databricks:team.models.current-workspace"
    direct = "openai:gpt-6-astra"
    stale = "databricks:system.ai.old-workspace-only"
    monkeypatch.setattr(_boot, "_gateway_picker_inventory", lambda *_: SimpleNamespace(names=(good.split(":", 1)[1],)))
    monkeypatch.setattr(gateway_picker, "load_recent_models", lambda: [direct, stale, good])
    data = gateway_picker.ModelSelectorScreen._catalog_model_data(
        _catalog(models=[direct, stale, good]), include_recent=True,
        default_scope=SimpleNamespace(load=lambda: direct),
    )
    assert data.all_models == [(good, "databricks")]
    assert data.recent_specs == [good]
    assert data.default_spec is None
    assert set(data.profiles) == {good}
    assert data.install_extras == {}
