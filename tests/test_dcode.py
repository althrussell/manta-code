from __future__ import annotations

import sys
import tomllib

import pytest

from manta_code import _boot, dcode
from manta_code.model_defaults import role_model


def _preserve_model_picker_hooks(monkeypatch):
    """Restore process-wide upstream integrations after each patching test."""
    import inspect

    from deepagents_code import model_catalog, model_config
    from deepagents_code.tui.widgets import auth, model_selector

    for module in (model_config, model_catalog, model_selector, auth):
        if hasattr(module, "get_available_models"):
            monkeypatch.setattr(module, "get_available_models", module.get_available_models)
    monkeypatch.setattr(
        _boot, "_original_get_available_models", _boot._original_get_available_models,
    )
    screen = model_selector.ModelSelectorScreen
    for name in (
        "__init__", "_catalog_model_data", "_apply_subset", "_info_line_content",
        "_help_text", "action_select", "compose", "_update_display",
    ):
        monkeypatch.setattr(screen, name, inspect.getattr_static(screen, name))


# --- merge_databricks_provider -------------------------------------------------


def test_merge_creates_provider_from_empty():
    merged = dcode.merge_databricks_provider({}, ["ep-a", "ep-b"])
    provider = merged["models"]["providers"]["databricks"]
    assert provider["class_path"] == dcode.DATABRICKS_CLASS_PATH
    assert provider["models"] == ["ep-a", "ep-b"]


def test_merge_preserves_unrelated_settings():
    existing = {"theme": "dark", "models": {"providers": {"openai": {"models": ["gpt"]}}}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    assert merged["theme"] == "dark"
    assert merged["models"]["providers"]["openai"] == {"models": ["gpt"]}
    assert merged["models"]["providers"]["databricks"]["models"] == ["ep-a"]


def test_merge_unions_and_dedupes_models():
    existing = {"models": {"providers": {"databricks": {"models": ["ep-a", "ep-x"]}}}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a", "ep-b"])
    # preserves existing first, appends new, dedupes
    assert merged["models"]["providers"]["databricks"]["models"] == ["ep-a", "ep-x", "ep-b"]


def test_merge_does_not_mutate_input():
    existing = {"models": {"providers": {}}}
    dcode.merge_databricks_provider(existing, ["ep-a"])
    assert existing == {"models": {"providers": {}}}


def test_refresh_inventory_preserves_user_models_and_default_across_workspaces():
    initial = {"models": {"default": "databricks:my-user-pin", "providers": {
        "databricks": {"models": ["my-user-pin"], "params": {"timeout": 17}},
        "openai": {"models": ["gpt"]},
    }}}
    first = dcode.merge_databricks_provider(initial, ["workspace-a-only"])
    first["models"]["providers"]["databricks"]["models"].append("hand-added")
    second = dcode.merge_databricks_provider(first, ["workspace-b-only"])
    provider = second["models"]["providers"]["databricks"]
    assert provider["models"] == ["my-user-pin", "hand-added", "workspace-b-only"]
    assert provider["params"] == {"timeout": 17}
    assert second["models"]["default"] == "databricks:my-user-pin"
    assert second["models"]["providers"]["openai"] == {"models": ["gpt"]}


def test_only_owned_defaults_migrate_and_user_edits_revoke_ownership():
    first = dcode.merge_databricks_provider({}, ["old"], default_endpoint="old")
    second = dcode.merge_databricks_provider(first, ["new"], default_endpoint="new")
    assert second["models"]["default"] == "databricks:new"
    second["models"]["default"] = "openai:user-choice"
    third = dcode.merge_databricks_provider(second, ["newer"], default_endpoint="newer")
    assert third["models"]["default"] == "openai:user-choice"
    unmarked = {"models": {"default": "databricks:old"}}
    assert dcode.merge_databricks_provider(unmarked, ["new"], default_endpoint="new")["models"]["default"] == "databricks:old"


def test_merge_applies_params():
    merged = dcode.merge_databricks_provider({}, ["ep-a"], params={"use_ai_gateway": True})
    assert merged["models"]["providers"]["databricks"]["params"] == {"use_ai_gateway": True}


def test_merge_rejects_non_table_models():
    with pytest.raises(dcode.LauncherError):
        dcode.merge_databricks_provider({"models": "oops"}, ["ep-a"])


def test_merge_sets_default_model_when_absent():
    merged = dcode.merge_databricks_provider({}, ["ep-a"], default_endpoint="ep-a")
    assert merged["models"]["default"] == "databricks:ep-a"


def test_merge_preserves_existing_user_default():
    existing = {"models": {"default": "openai:gpt"}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"], default_endpoint="ep-a")
    assert merged["models"]["default"] == "openai:gpt"


def test_merge_without_default_endpoint_leaves_default_unset():
    merged = dcode.merge_databricks_provider({}, ["ep-a"])
    assert "default" not in merged["models"]


def test_merge_suppresses_tavily_warning_by_default():
    merged = dcode.merge_databricks_provider({}, ["ep-a"])
    assert "tavily" in merged["warnings"]["suppress"]


def test_merge_preserves_existing_suppressed_warnings():
    existing = {"warnings": {"suppress": ["ripgrep"]}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    assert merged["warnings"]["suppress"] == ["ripgrep", "tavily"]


def test_merge_does_not_duplicate_suppressed_warning():
    existing = {"warnings": {"suppress": ["tavily"]}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    assert merged["warnings"]["suppress"] == ["tavily"]


# --- branding theme (Databricks red) ------------------------------------------


def test_merge_registers_manta_theme_and_sets_default():
    merged = dcode.merge_databricks_provider({}, ["ep-a"])
    assert merged["themes"][dcode.MANTA_THEME_KEY]["primary"] == dcode.DATABRICKS_RED
    assert merged["themes"][dcode.MANTA_THEME_KEY]["dark"] is True
    assert merged["ui"]["theme"] == dcode.MANTA_THEME_KEY


def test_merge_respects_existing_theme_preference():
    existing = {"ui": {"theme": "dracula"}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    # User's saved theme choice is never overridden...
    assert merged["ui"]["theme"] == "dracula"
    # ...but the manta theme is still registered so it can be selected.
    assert dcode.MANTA_THEME_KEY in merged["themes"]


def test_merge_preserves_other_user_themes():
    existing = {"themes": {"solarized": {"label": "Solarized", "primary": "#268BD2"}}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    assert merged["themes"]["solarized"] == {"label": "Solarized", "primary": "#268BD2"}
    assert merged["themes"][dcode.MANTA_THEME_KEY]["primary"] == dcode.DATABRICKS_RED


def test_merge_theme_is_idempotent():
    cfg = dcode.merge_databricks_provider({}, ["ep-a"])
    again = dcode.merge_databricks_provider(cfg, ["ep-a"])
    assert again["themes"][dcode.MANTA_THEME_KEY]["primary"] == dcode.DATABRICKS_RED
    assert again["ui"]["theme"] == dcode.MANTA_THEME_KEY


# --- build_launch_env ----------------------------------------------------------


def test_build_launch_env_sets_profile(monkeypatch):
    for var in ("MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    env = dcode.build_launch_env("s1", base_env={})
    assert env["DATABRICKS_CONFIG_PROFILE"] == "s1"
    assert env[dcode.SPLASH_SUBHEADER_ENV] == dcode.SPLASH_SUBHEADER


def test_explicit_launch_profile_overrides_inherited_manta_profile(monkeypatch):
    monkeypatch.setenv("MANTA_PROFILE", "old")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "older")
    env = dcode.build_launch_env("selected")
    assert env["MANTA_PROFILE"] == env["DATABRICKS_CONFIG_PROFILE"] == "selected"


def test_build_launch_env_no_profile_no_var(monkeypatch):
    for var in ("MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    env = dcode.build_launch_env(None, base_env={})
    assert "DATABRICKS_CONFIG_PROFILE" not in env


def test_build_launch_env_does_not_clobber_existing_splash(monkeypatch):
    env = dcode.build_launch_env("s1", base_env={dcode.SPLASH_SUBHEADER_ENV: "custom"})
    assert env[dcode.SPLASH_SUBHEADER_ENV] == "custom"


# --- build_dcode_argv ----------------------------------------------------------


def test_build_argv_injects_default_model(monkeypatch):
    # Hermetic: ignore any persisted default/recent agent on this machine.
    monkeypatch.setattr(dcode, "_effective_initial_agent", lambda extras: None)
    argv = dcode.build_dcode_argv("ep-a", [], python="/usr/bin/python3")
    assert argv == [
        "/usr/bin/python3",
        "-m",
        dcode.DCODE_BOOT_MODULE,
        "-M",
        "databricks:ep-a",
    ]


def test_build_argv_respects_user_model_flag():
    argv = dcode.build_dcode_argv("ep-a", ["-M", "openai:gpt"], python="py")
    assert "databricks:ep-a" not in argv
    assert argv[-2:] == ["-M", "openai:gpt"]


def test_build_argv_forwards_passthrough():
    argv = dcode.build_dcode_argv("ep-a", ["-r", "--skill", "x"], python="py")
    assert argv[-3:] == ["-r", "--skill", "x"]


def test_has_model_flag_variants():
    assert dcode._has_model_flag(["--model=openai:gpt"]) is True
    assert dcode._has_model_flag(["-Mfoo"]) is True
    assert dcode._has_model_flag(["-r"]) is False


# --- build_run_argv (headless) ------------------------------------------------


def test_build_run_argv_defaults(monkeypatch):
    monkeypatch.setattr(dcode, "_effective_initial_agent", lambda extras: None)
    argv = dcode.build_run_argv("ep-a", "fix the job", [], python="py")
    assert argv[:3] == ["py", "-m", dcode.DCODE_BOOT_MODULE]
    assert "-M" in argv and "databricks:ep-a" in argv
    assert argv.count("-n") == 1
    # message immediately follows -n
    assert argv[argv.index("-n") + 1] == "fix the job"
    assert "-q" in argv
    assert "--no-stream" in argv
    assert "--max-turns" in argv
    assert "--timeout" in argv
    assert str(dcode.DEFAULT_RUN_TIMEOUT) in argv


def test_build_run_argv_options_and_passthrough():
    argv = dcode.build_run_argv(
        "ep-a",
        "do it",
        ["--extra"],
        quiet=False,
        no_stream=False,
        timeout=None,
        max_turns=None,
        json_output="stream-json",
        shell_allow_list="ls,cat",
        python="py",
    )
    assert "-q" not in argv
    assert "--no-stream" not in argv
    assert "--timeout" not in argv
    assert "--max-turns" not in argv
    assert "--json-output" not in argv
    assert argv[argv.index("--shell-allow-list") + 1] == "ls,cat"
    assert argv[-1] == "--extra"


def test_build_run_argv_respects_user_model():
    argv = dcode.build_run_argv("ep-a", "t", ["-M", "openai:gpt"], python="py")
    assert "databricks:ep-a" not in argv


# --- ensure_dcode_config (round-trip) -----------------------------------------


def test_ensure_dcode_config_roundtrip_idempotent(tmp_path):
    pytest.importorskip("tomli_w")
    cfg = tmp_path / "config.toml"
    dcode.ensure_dcode_config(["ep-a", "ep-b"], config_path=cfg)
    dcode.ensure_dcode_config(["ep-b", "ep-c"], config_path=cfg)
    data = tomllib.loads(cfg.read_text())
    provider = data["models"]["providers"]["databricks"]
    assert provider["class_path"] == dcode.DATABRICKS_CLASS_PATH
    assert provider["models"] == ["ep-b", "ep-c"]
    dcode.ensure_dcode_config(["ep-b", "ep-c"], config_path=cfg)
    assert tomllib.loads(cfg.read_text()) == data


def test_ensure_dcode_config_writes_default_endpoint(tmp_path):
    pytest.importorskip("tomli_w")
    cfg = tmp_path / "config.toml"
    dcode.ensure_dcode_config(["ep-a"], config_path=cfg, default_endpoint="ep-a")
    data = tomllib.loads(cfg.read_text())
    assert data["models"]["default"] == "databricks:ep-a"


# --- mark_onboarding_complete -------------------------------------------------


def test_mark_onboarding_complete_writes_marker(tmp_path):
    marker = tmp_path / ".state" / "onboarding_complete"
    path = dcode.mark_onboarding_complete(marker_path=marker)
    assert path == marker
    assert marker.read_text() == "1\n"


def test_mark_onboarding_complete_is_idempotent(tmp_path):
    marker = tmp_path / ".state" / "onboarding_complete"
    dcode.mark_onboarding_complete(marker_path=marker)
    dcode.mark_onboarding_complete(marker_path=marker)
    assert marker.read_text() == "1\n"


# --- branding boot shim -------------------------------------------------------


def test_versioned_appends_matching_version_tag():
    out = _boot._versioned("ART", "9.9.9")
    assert out.startswith("ART")
    assert "v9.9.9" in out


def test_composed_banner_stacks_ray_over_wordmark():
    # Both splash variants carry the manta-ray mark above the wordmark.
    assert "▝▜██▀▀▀██▛▘" in _boot.MANTA_UNICODE_BANNER  # compact mark body
    assert _boot.MANTA_WORDMARK_UNICODE.splitlines()[-1] in _boot.MANTA_UNICODE_BANNER
    assert "<##=======##>" in _boot.MANTA_ASCII_BANNER  # ascii mark body
    assert "|_|  |_/_/" in _boot.MANTA_ASCII_BANNER  # ascii wordmark tail


def test_compose_banner_centers_ray_as_block():
    # One shared pad for the whole mark: a line's intentional leading spaces
    # (the tail row) survive, instead of each row being re-centered and the
    # shape skewing (the "renders a bit wonky" bug).
    ray = "wwww\n  tt"
    wordmark = "\n" + "#" * 10 + "\n"
    out = _boot._compose_banner(ray, wordmark)
    lines = out.splitlines()
    assert "   wwww" in lines  # (10-4)//2 = 3 spaces
    assert "     tt" in lines  # same 3-space pad + the authored 2 spaces
    assert "##########" in out


def test_compose_banner_preserves_mark_alignment():
    out = _boot._compose_banner(_boot.MANTA_RAY_UNICODE, _boot.MANTA_WORDMARK_UNICODE)
    rows = [ln for ln in out.splitlines() if "▌" in ln or "▜" in ln or "▝▄" in ln]
    pads = [len(ln) - len(ln.lstrip()) for ln in rows]
    # Body rows share the pad; the tail keeps its authored +2 indent.
    assert pads[0] == pads[1]
    assert pads[2] == pads[0] + 2


def test_apply_branding_overrides_upstream_banner():
    config = pytest.importorskip("deepagents_code.config")
    original_unicode = config._UNICODE_BANNER
    original_ascii = config._ASCII_BANNER
    try:
        assert _boot.apply_branding() is True
        assert config._UNICODE_BANNER != original_unicode
        assert config._ASCII_BANNER != original_ascii
        # get_banner reads the patched constants (version logic may append a
        # version/"(local)" tag, so just assert it renders a non-empty banner).
        assert config.get_banner().strip()
    finally:
        config._UNICODE_BANNER = original_unicode
        config._ASCII_BANNER = original_ascii


def test_apply_branding_degrades_on_live_widget_integration_error(monkeypatch):
    config = pytest.importorskip("deepagents_code.config")
    from manta_code import branding

    monkeypatch.setattr(config, "_UNICODE_BANNER", config._UNICODE_BANNER)
    monkeypatch.setattr(config, "_ASCII_BANNER", config._ASCII_BANNER)

    def incompatible_widget(**_kwargs):
        raise AttributeError("upstream widget contract changed")

    monkeypatch.setattr(branding, "apply_tui_branding", incompatible_widget)
    assert _boot.apply_branding() is False


def test_databricks_model_discovery_excludes_direct_api_providers(monkeypatch):
    # Workspace models use SDK authentication; installed upstream provider
    # packages must not add unrelated API-key choices to Manta's picker.
    monkeypatch.setattr(
        _boot,
        "_original_get_available_models",
        lambda: {
            "anthropic": ["claude-opus"],
            "databricks": ["databricks-claude-sonnet", "databricks-gpt"],
            "openai": ["gpt-5"],
        },
    )
    result = _boot._databricks_first_models()
    assert result == {
        "databricks": ["databricks-claude-sonnet", "databricks-gpt"],
    }


def test_databricks_model_discovery_does_not_substitute_direct_providers(monkeypatch):
    # Missing workspace discovery must not silently offer a direct-key model.
    monkeypatch.setattr(
        _boot,
        "_original_get_available_models",
        lambda: {"anthropic": ["claude-opus"]},
    )
    assert _boot._databricks_first_models() == {}


def test_databricks_first_models_empty_on_upstream_failure(monkeypatch):
    def _boom():
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(_boot, "_original_get_available_models", _boom)
    assert _boot._databricks_first_models() == {}


def test_prefer_databricks_models_patches_discovery(monkeypatch):
    model_config = pytest.importorskip("deepagents_code.model_config")
    _preserve_model_picker_hooks(monkeypatch)
    original = model_config.get_available_models
    saved_original = _boot._original_get_available_models
    try:
        assert _boot.prefer_databricks_models() is True
        assert model_config.get_available_models is _boot._databricks_first_models
        # The upstream implementation is captured so the wrapper can delegate.
        assert _boot._original_get_available_models is original
        # Idempotent: re-applying must not capture the wrapper as the original.
        assert _boot.prefer_databricks_models() is True
        assert _boot._original_get_available_models is original
    finally:
        model_config.get_available_models = original
        _boot._original_get_available_models = saved_original


def test_prefer_databricks_models_updates_loaded_catalog(monkeypatch):
    model_config = pytest.importorskip("deepagents_code.model_config")
    catalog = pytest.importorskip("deepagents_code.model_catalog")
    _preserve_model_picker_hooks(monkeypatch)
    def discover():
        return {"openai": ["gpt"], "databricks": ["team-chat"]}

    monkeypatch.setattr(model_config, "get_available_models", discover)
    monkeypatch.setattr(catalog, "get_available_models", discover)
    monkeypatch.setattr(_boot, "_original_get_available_models", None)
    assert _boot.prefer_databricks_models() is True
    assert catalog.get_available_models() == {"databricks": ["team-chat"]}


def test_rebrand_auth_screen_replaces_screen_internals():
    auth = pytest.importorskip("deepagents_code.tui.widgets.auth")
    screen = auth.AuthManagerScreen
    original_compose = screen.compose
    original_on_mount = screen.on_mount
    original_selected = screen.on_option_list_option_selected
    try:
        assert _boot.rebrand_auth_screen() is True
        # compose / on_mount / selection handler are swapped for the switcher.
        assert screen.compose is not original_compose
        assert screen.on_mount is not original_on_mount
        assert screen.on_option_list_option_selected is not original_selected
    finally:
        screen.compose = original_compose
        screen.on_mount = original_on_mount
        screen.on_option_list_option_selected = original_selected


def test_auth_provider_selection_preserves_upstream_handler():
    auth = pytest.importorskip("deepagents_code.tui.widgets.auth")
    from types import SimpleNamespace

    screen = auth.AuthManagerScreen
    names = (
        "compose", "on_mount", "on_option_list_option_selected",
        "action_cursor_down", "action_cursor_up",
    )
    original = {name: getattr(screen, name) for name in names}
    calls = []

    def upstream_selected(self, event):
        calls.append(event.option.id)

    try:
        screen.on_option_list_option_selected = upstream_selected
        assert _boot.rebrand_auth_screen() is True
        assert _boot.rebrand_auth_screen() is True
        event = SimpleNamespace(
            option=SimpleNamespace(id="openai_codex"),
            option_list=SimpleNamespace(id="auth-manager-options"),
        )
        screen.on_option_list_option_selected(object(), event)
        assert calls == ["openai_codex"]
    finally:
        for name, value in original.items():
            setattr(screen, name, value)


def test_rebrand_model_selector_footer_neutralizes_databricks(monkeypatch):
    model_selector = pytest.importorskip("deepagents_code.tui.widgets.model_selector")
    screen = model_selector.ModelSelectorScreen
    original = screen._update_footer
    try:
        assert _boot.rebrand_model_selector_footer() is True
        assert screen._update_footer is not original

        class _FakeStatic:
            def __init__(self):
                self.content = None

            def update(self, content):
                self.content = content

        class _FakeSelector:
            def __init__(self):
                self._filtered_models = [("databricks:foo", "databricks")]
                self._selected_index = 0
                self._profiles = {}
                self._static = _FakeStatic()

            def query_one(self, _selector, *_args):
                return self._static

        fake = _FakeSelector()
        screen._update_footer(fake)
        assert "Context: unknown" in fake._static.content.plain
        assert "Workspace authentication" in fake._static.content.plain
    finally:
        screen._update_footer = original


def test_allow_blocking_server_appends_flag(monkeypatch):
    server = pytest.importorskip("deepagents_code.client.launch.server")
    original = server._build_server_cmd
    try:
        monkeypatch.setattr(
            server,
            "_build_server_cmd",
            lambda *_a, **_k: [sys.executable, "-m", "langgraph_cli", "dev"],
            raising=True,
        )
        assert _boot.allow_blocking_server() is True
        cmd = server._build_server_cmd(object(), host="127.0.0.1", port=1234)
        assert "--allow-blocking" in cmd
        # Idempotent: re-applying the patch must not duplicate the flag.
        assert cmd.count("--allow-blocking") == 1
    finally:
        server._build_server_cmd = original


class _FakeApp:
    def __init__(self):
        self.notifications = []
        self.restarted = False

    def notify(self, message, *, severity="information", markup=True):
        self.notifications.append((severity, message))

    async def _restart_server_manual(self):
        self.restarted = True


def test_switch_databricks_workspace_sets_env_and_restarts(monkeypatch):
    import asyncio

    monkeypatch.setenv("MANTA_PROFILE", "old")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    refreshed = []
    monkeypatch.setattr(_boot, "_refresh_workspace_inventory", lambda *args: refreshed.append(args))
    app = _FakeApp()
    app._effective_model_spec = lambda: "databricks:custom.team.current"

    async def run():
        _boot.switch_databricks_workspace(app, "acme-prod")
        # Let the scheduled restart task run.
        for _ in range(20):
            if app.restarted:
                break
            await asyncio.sleep(0.005)

    asyncio.run(run())

    import os

    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "acme-prod"
    assert os.environ["MANTA_PROFILE"] == "acme-prod"
    assert app.restarted is True
    assert refreshed == [("acme-prod", "databricks:custom.team.current")]
    assert any("Switching to workspace 'acme-prod'" in msg for _, msg in app.notifications)


def test_switch_databricks_workspace_without_restart_hook(monkeypatch):
    monkeypatch.setenv("MANTA_PROFILE", "old")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    class _NoRestartApp:
        def __init__(self):
            self.notifications = []

        def notify(self, message, *, severity="information", markup=True):
            self.notifications.append((severity, message))

    app = _NoRestartApp()
    _boot.switch_databricks_workspace(app, "acme-prod")

    import os

    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "acme-prod"
    assert any(sev == "warning" for sev, _ in app.notifications)


def test_boot_main_refuses_missing_required_control_plane(monkeypatch, capsys):
    # ADR 0010: falling back to vanilla is fine; falling back *silently* is not.
    pytest.importorskip("deepagents_code")
    import deepagents_code.main as upstream_main

    monkeypatch.setattr(_boot, "apply_branding", lambda: True)
    monkeypatch.setattr(_boot, "rebrand_model_selector_footer", lambda: True)
    monkeypatch.setattr(_boot, "extend_recommended_models", lambda: True)
    monkeypatch.setattr(_boot, "prefer_databricks_models", lambda: True)
    monkeypatch.setattr(_boot, "rebrand_auth_screen", lambda: True)
    monkeypatch.setattr(_boot, "align_agent_switch_model", lambda: True)
    monkeypatch.setattr(_boot, "add_agent_mentions_to_autocomplete", lambda: True)
    monkeypatch.setattr(_boot, "allow_blocking_server", lambda: True)
    monkeypatch.setattr(_boot, "install_manta_build_hook", lambda: False)
    monkeypatch.setattr(upstream_main, "cli_main", lambda: None)

    with pytest.raises(RuntimeError, match="Required Manta integration unavailable: agent control plane"):
        _boot.main()


def test_boot_main_silent_when_everything_applies(monkeypatch, capsys):
    pytest.importorskip("deepagents_code")
    import deepagents_code.main as upstream_main

    for fn in (
        "apply_branding",
        "rebrand_model_selector_footer",
        "extend_recommended_models",
        "prefer_databricks_models",
        "rebrand_auth_screen",
        "align_agent_switch_model",
        "add_agent_mentions_to_autocomplete",
        "allow_blocking_server",
        "install_manta_build_hook",
    ):
        monkeypatch.setattr(_boot, fn, lambda: True)
    monkeypatch.setattr(upstream_main, "cli_main", lambda: None)

    _boot.main()
    assert "Manta degraded" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("failed_hook", "warning"),
    [
        ("apply_branding", "Manta welcome banner"),
        ("rebrand_model_selector_footer", "Databricks model details"),
        ("extend_recommended_models", "Databricks model recommendations"),
        ("prefer_databricks_models", "Databricks workspace model picker"),
    ],
)
def test_boot_main_announces_ui_integration_failure(monkeypatch, capsys, failed_hook, warning):
    pytest.importorskip("deepagents_code")
    import deepagents_code.main as upstream_main

    for hook in (
        "apply_branding",
        "rebrand_model_selector_footer",
        "extend_recommended_models",
        "prefer_databricks_models",
        "rebrand_auth_screen",
        "align_agent_switch_model",
        "add_agent_mentions_to_autocomplete",
        "add_session_cost_command",
        "allow_blocking_server",
        "install_manta_build_hook",
    ):
        monkeypatch.setattr(_boot, hook, lambda: True)
    monkeypatch.setattr(_boot, failed_hook, lambda: False)
    monkeypatch.setattr(upstream_main, "cli_main", lambda: None)

    _boot.main()
    assert warning in capsys.readouterr().err


def test_rebranded_auth_screen_renders_both_sections(tmp_path, monkeypatch):
    # Pilot-render the recomposed /auth screen: the Databricks workspace picker
    # leads and upstream's provider API-key list survives below it (ADR 0010).
    pytest.importorskip("deepagents_code")
    textual = pytest.importorskip("textual")  # noqa: F841
    import asyncio

    from textual.app import App
    from textual.widgets import OptionList

    cfg = tmp_path / ".databrickscfg"
    cfg.write_text(
        "[prod]\nhost = https://prod.example.com\ntoken = x\n", encoding="utf-8"
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    assert _boot.rebrand_auth_screen() is True
    from deepagents_code.tui.widgets.auth import AuthManagerScreen

    class _Harness(App):
        pass

    async def _render() -> tuple[int, list[str]]:
        app = _Harness()
        async with app.run_test(size=(100, 40)) as pilot:
            await app.push_screen(AuthManagerScreen())
            await pilot.pause()
            screen = app.screen
            workspaces = screen.query_one("#manta-workspace-options", OptionList)
            prov = screen.query_one("#auth-manager-options", OptionList)
            ids = [
                prov.get_option_at_index(i).id for i in range(prov.option_count)
            ]
            return workspaces.option_count, ids

    workspace_count, provider_ids = asyncio.run(_render())
    assert workspace_count == 1  # the prod profile from the fake config file
    # Upstream's installed providers are still manageable on the same screen.
    assert "anthropic" in provider_ids


# --- agent-addressed launches use the agent's pin (VISION pillar 2) -------------


def test_build_argv_agent_launch_uses_agent_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_dcode_argv("ep-default", ["-a", "chief"], python="py")
    # chief pins databricks-gpt-5-4-mini: the session model matches the agent
    # that actually runs, so the TUI footer tells the truth.
    assert argv[argv.index("-M") + 1] == role_model("chief")
    assert "databricks:ep-default" not in argv


def test_build_argv_agent_launch_falls_back_for_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_dcode_argv("ep-default", ["-a", "ghost"], python="py")
    assert argv[argv.index("-M") + 1] == "databricks:ep-default"


def test_build_argv_user_model_flag_beats_agent_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_dcode_argv("ep-default", ["-a", "chief", "-M", "openai:gpt"], python="py")
    assert "databricks:databricks-gpt-5-4" not in argv
    assert argv.count("-M") == 1


def test_build_run_argv_agent_launch_uses_agent_pin(tmp_path, monkeypatch):
    # Background tasks (the runner passes -a <agent>) launch on the agent's
    # pinned model too — not the cheap session default.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_run_argv("ep-default", "do it", ["-a", "planning"], python="py")
    assert argv[argv.index("-M") + 1] == role_model("planning")


def test_addressed_agent_parsing():
    assert dcode._addressed_agent(["-a", "chief"]) == "chief"
    assert dcode._addressed_agent(["--agent", "swe"]) == "swe"
    assert dcode._addressed_agent(["--agent=review"]) == "review"
    assert dcode._addressed_agent(["-r"]) is None


def test_align_agent_switch_model_applies_pin(tmp_path, monkeypatch):
    # Selecting a Manta agent in /agents must also switch the session model to
    # that agent's pin (thread-preserving, non-persisted) so the footer and
    # `manta agents` agree.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    try:
        assert _boot.align_agent_switch_model() is True
        # Idempotent: re-applying doesn't double-wrap.
        wrapped = cls._restart_server_for_agent_swap
        assert _boot.align_agent_switch_model() is True
        assert cls._restart_server_for_agent_swap is wrapped

        calls = {}

        class _FakeApp:
            _assistant_id = "chief"

            async def _switch_model(self, spec, **kwargs):
                calls["spec"] = spec
                calls["kwargs"] = kwargs

        async def fake_swap(self, agent_name, **kwargs):
            calls["swapped"] = agent_name
            return True

        # Re-wrap a fresh stub so the wrapper calls our fake original swap.
        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True
        asyncio.run(cls._restart_server_for_agent_swap(_FakeApp(), "chief"))
        assert calls["swapped"] == "chief"
        assert calls["spec"] == role_model("chief")
        assert calls["kwargs"] == {"persist": False, "announce_unchanged": False}
    finally:
        cls._restart_server_for_agent_swap = original


def test_align_agent_switch_model_skips_non_manta_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    try:
        calls = {}

        async def fake_swap(self, agent_name, **kwargs):
            calls["swapped"] = agent_name
            return True

        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True

        class _FakeApp:
            _assistant_id = "agent"

            async def _switch_model(self, spec, **kwargs):
                calls["spec"] = spec

        # Unpinned target: falls back to the configured cheap default so a
        # previous specialist's premium pin never ratchets into the base agent.
        from manta_code import auth as manta_auth

        monkeypatch.setattr(manta_auth, "databricks_configured", lambda profile=None: True)
        asyncio.run(cls._restart_server_for_agent_swap(_FakeApp(), "agent"))
        assert calls["swapped"] == "agent"
        assert calls["spec"] == role_model("orchestrator")
    finally:
        cls._restart_server_for_agent_swap = original


def test_bare_launch_uses_recent_agent_pin(tmp_path, monkeypatch):
    # Upstream reopens sessions on [agents].recent; the session model must
    # follow that agent's pin, not the cheap default.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    mc = pytest.importorskip("deepagents_code.model_config")
    monkeypatch.setattr(mc, "load_default_agent", lambda *a, **k: None)
    monkeypatch.setattr(mc, "load_recent_agent", lambda *a, **k: "planning")
    argv = dcode.build_dcode_argv("ep-default", [], python="py")
    assert argv[argv.index("-M") + 1] == role_model("planning")


def test_bare_launch_default_agent_beats_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    mc = pytest.importorskip("deepagents_code.model_config")
    monkeypatch.setattr(mc, "load_default_agent", lambda *a, **k: "chief")
    monkeypatch.setattr(mc, "load_recent_agent", lambda *a, **k: "planning")
    argv = dcode.build_dcode_argv("ep-default", [], python="py")
    assert argv[argv.index("-M") + 1] == role_model("chief")


def test_bare_launch_base_agent_keeps_cheap_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    mc = pytest.importorskip("deepagents_code.model_config")
    monkeypatch.setattr(mc, "load_default_agent", lambda *a, **k: None)
    monkeypatch.setattr(mc, "load_recent_agent", lambda *a, **k: None)
    argv = dcode.build_dcode_argv("ep-default", [], python="py")
    assert argv[argv.index("-M") + 1] == "databricks:ep-default"


def test_a_flag_beats_persisted_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    mc = pytest.importorskip("deepagents_code.model_config")
    monkeypatch.setattr(mc, "load_default_agent", lambda *a, **k: "planning")
    argv = dcode.build_dcode_argv("ep-default", ["-a", "chief"], python="py")
    assert argv[argv.index("-M") + 1] == role_model("chief")


class _FakeCompletionView:
    def __init__(self):
        self.rendered = None

    def render_completion_suggestions(self, suggestions, selected):
        self.rendered = list(suggestions)

    def clear_completion_suggestions(self):
        self.rendered = None

    def replace_completion_range(self, start, end, replacement):
        pass


def _fresh_controller(tmp_path):
    from deepagents_code.tui.widgets.autocomplete import FuzzyFileController

    controller = FuzzyFileController(_FakeCompletionView(), cwd=tmp_path)
    controller._file_cache = ["src/main.py", "chart.py"]
    return controller


def test_agent_mentions_complete_at_message_start(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    pytest.importorskip("deepagents_code.tui.widgets.autocomplete")
    assert _boot.add_agent_mentions_to_autocomplete() is True
    assert _boot.add_agent_mentions_to_autocomplete() is True  # idempotent

    controller = _fresh_controller(tmp_path)
    controller.on_text_changed("@ch", 3)
    labels = [label for label, _hint in controller._suggestions]
    hints = dict(controller._suggestions)
    assert labels[0] == "@chief"
    assert hints["@chief"] == "agent"
    # File matches still follow the agent suggestions.
    assert any(label.startswith("@chart") for label in labels)


def test_agent_mentions_not_offered_mid_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    pytest.importorskip("deepagents_code.tui.widgets.autocomplete")
    assert _boot.add_agent_mentions_to_autocomplete() is True

    controller = _fresh_controller(tmp_path)
    text = "look at @ch"
    controller.on_text_changed(text, len(text))
    labels = [label for label, _hint in controller._suggestions]
    assert "@chief" not in labels  # mid-message @ stays a file mention


@pytest.mark.parametrize("resume_args", [
    ["-r", "thread-1"], ["-rthread-1"], ["--resume=thread-1"],
])
def test_resume_launch_gets_no_model_injection(tmp_path, monkeypatch, resume_args):
    # A resumed thread adopts its own model; injecting -M would override it
    # (review finding).
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_dcode_argv("ep-default", resume_args, python="py")
    assert "-M" not in argv


def test_databricks_pin_skipped_when_unconfigured(tmp_path, monkeypatch):
    # Off-Databricks (default_endpoint=None): a databricks: pin would force an
    # unreachable provider — skip injection entirely (review finding).
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    argv = dcode.build_dcode_argv(None, ["-a", "chief"], python="py")
    assert "-M" not in argv


def test_auth_screen_tab_actions_switch_focus_target():
    auth_widgets = pytest.importorskip("deepagents_code.tui.widgets.auth")
    screen = auth_widgets.AuthManagerScreen
    original = (
        screen.compose,
        screen.on_mount,
        screen.on_option_list_option_selected,
        screen.__dict__.get("action_cursor_down"),
        screen.__dict__.get("action_cursor_up"),
    )
    try:
        assert _boot.rebrand_auth_screen() is True
        # Tab actions are overridden: with two lists, Tab moves focus between
        # sections instead of silently moving the provider highlight while
        # Enter acts on the workspace list (review finding).
        assert screen.action_cursor_down is not None
        assert screen.action_cursor_down.__name__ == "_action_cursor_down"
        assert screen.action_cursor_up.__name__ == "_action_cursor_up"
    finally:
        screen.compose, screen.on_mount, screen.on_option_list_option_selected = original[:3]
        if original[3] is not None:
            screen.action_cursor_down = original[3]
        if original[4] is not None:
            screen.action_cursor_up = original[4]


def test_agent_swap_resumes_previous_thread(tmp_path, monkeypatch):
    # Switching agents must continue the conversation (auto-resume the
    # previous thread) instead of stranding it on a fresh thread; the user
    # can /clear to start over.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    from deepagents_code.tui.widgets.message_store import MessageType
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    try:
        calls = {}

        async def fake_swap(self, agent_name, **kwargs):
            calls["swapped"] = agent_name
            calls["resumed"] = kwargs["resume_thread_id"]
            # Upstream resumes within the restart, before returning success.
            self._lc_thread_id = kwargs["resume_thread_id"] or "new-thread"
            self._message_store = _FakeMessageStore([])
            return True

        class _FakeMessage:
            def __init__(self, type_):
                self.type = type_

        class _FakeMessageStore:
            def __init__(self, messages):
                self._messages = messages

            def get_all_messages(self):
                return self._messages

        class _FakeApp:
            _assistant_id = "chief"
            _lc_thread_id = "prev-thread"
            _message_store = _FakeMessageStore(
                [_FakeMessage(MessageType.USER), _FakeMessage(MessageType.ASSISTANT)]
            )

            async def _resume_thread(self, thread_id):
                calls["resumed"] = thread_id

            async def _switch_model(self, spec, **kwargs):
                calls["spec"] = spec

            def notify(self, *a, **k):
                calls["notified"] = True

        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True
        asyncio.run(cls._restart_server_for_agent_swap(_FakeApp(), "chief"))
        assert calls["resumed"] == "prev-thread"
        assert calls["notified"] is True
        assert calls["spec"] == role_model("chief")  # pin still applies
    finally:
        cls._restart_server_for_agent_swap = original


def test_agent_swap_skips_resume_for_empty_thread(tmp_path, monkeypatch):
    # A USER-only thread has no checkpoint row: resuming it would fail, so
    # the swap keeps upstream's fresh-thread behavior (mirrors the resume-hint
    # gating).
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    from deepagents_code.tui.widgets.message_store import MessageType
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    try:
        calls = {}

        async def fake_swap(self, agent_name, **kwargs):
            calls["swapped"] = agent_name
            calls["resume_thread_id"] = kwargs["resume_thread_id"]
            return True

        class _FakeMessage:
            type = MessageType.USER

        class _FakeApp:
            _assistant_id = "chief"
            _lc_thread_id = "prev-thread"

            class _message_store:  # noqa: N801 - stand-in attr
                @staticmethod
                def get_all_messages():
                    return [_FakeMessage()]

            async def _resume_thread(self, thread_id):
                calls["resumed"] = thread_id

            async def _switch_model(self, spec, **kwargs):
                calls["spec"] = spec

        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True
        asyncio.run(cls._restart_server_for_agent_swap(_FakeApp(), "chief"))
        assert "resumed" not in calls
        assert calls["resume_thread_id"] is None
        assert calls["spec"]  # pin alignment still applies
    finally:
        cls._restart_server_for_agent_swap = original


def test_agent_swap_resume_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_SWAP_RESUME", "0")
    app_mod = pytest.importorskip("deepagents_code.app")
    from deepagents_code.tui.widgets.message_store import MessageType
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    try:
        calls = {}

        async def fake_swap(self, agent_name, **kwargs):
            calls["swapped"] = agent_name
            calls["resume_thread_id"] = kwargs["resume_thread_id"]
            return True

        class _FakeMessage:
            type = MessageType.ASSISTANT

        class _FakeApp:
            _assistant_id = "chief"
            _lc_thread_id = "prev-thread"

            class _message_store:  # noqa: N801 - stand-in attr
                @staticmethod
                def get_all_messages():
                    return [_FakeMessage()]

            async def _resume_thread(self, thread_id):
                calls["resumed"] = thread_id

            async def _switch_model(self, spec, **kwargs):
                calls["spec"] = spec

        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True
        asyncio.run(cls._restart_server_for_agent_swap(_FakeApp(), "chief"))
        assert "resumed" not in calls
        assert calls["resume_thread_id"] is None
    finally:
        cls._restart_server_for_agent_swap = original


@pytest.mark.parametrize("success", [True, False])
def test_agent_swap_preserves_explicit_transition_arguments(tmp_path, monkeypatch, success):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_restart_server_for_agent_swap"]
    calls = {}
    payload = object()

    async def fake_swap(self, agent_name, **kwargs):
        calls["transition"] = kwargs
        return success

    class FakeApp:
        _assistant_id = "chief"

        async def _switch_model(self, spec, **kwargs):
            calls["model"] = spec

    try:
        cls._restart_server_for_agent_swap = fake_swap
        assert _boot.align_agent_switch_model() is True
        result = asyncio.run(cls._restart_server_for_agent_swap(
            FakeApp(), "chief", resume_thread_id="requested-thread",
            preloaded_payload=payload, persist_default_agent=False,
        ))
        assert result is success
        assert calls["transition"] == {
            "resume_thread_id": "requested-thread",
            "preloaded_payload": payload,
            "persist_default_agent": False,
        }
        assert "model" not in calls
    finally:
        cls._restart_server_for_agent_swap = original


def test_merge_sets_chief_as_default_agent_when_absent():
    # Fresh installs open as the chief of staff (VISION pillar 5's front door).
    merged = dcode.merge_databricks_provider({}, ["ep-a"])
    assert merged["agents"]["default"] == "chief"


def test_merge_never_overrides_user_default_agent():
    existing = {"agents": {"default": "swe", "recent": "planning"}}
    merged = dcode.merge_databricks_provider(existing, ["ep-a"])
    assert merged["agents"]["default"] == "swe"
    assert merged["agents"]["recent"] == "planning"  # untouched


def test_extend_recommended_models_adds_manta_lineup(tmp_path, monkeypatch):
    # Recommended mode may rank workspace models, but must never reintroduce
    # upstream API-key providers after gateway catalog scoping.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    ms = pytest.importorskip("deepagents_code.tui.widgets.model_selector")
    original = ms._RECOMMENDED_MODELS
    try:
        assert _boot.extend_recommended_models() is True
        extended = ms._RECOMMENDED_MODELS
        assert all(spec.startswith("databricks:") for spec in extended)
        assert role_model("orchestrator") in extended  # default
        assert role_model("planning") in extended  # planning pin
        assert role_model("chief") in extended  # chief pin
        # Idempotent re-application doesn't balloon the set.
        size = len(extended)
        assert _boot.extend_recommended_models() is True
        assert len(ms._RECOMMENDED_MODELS) == size
    finally:
        ms._RECOMMENDED_MODELS = original


def test_model_picker_renders_and_filters_offline_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    ms = pytest.importorskip("deepagents_code.tui.widgets.model_selector")
    _preserve_model_picker_hooks(monkeypatch)
    import asyncio

    from deepagents_code.model_catalog import CatalogProvider, ModelCatalog
    from deepagents_code.model_config import ProviderAuthState
    from types import SimpleNamespace
    from textual.app import App
    from textual.widgets import Static

    default = role_model("orchestrator")
    custom = "databricks:team-chat"
    catalog = ModelCatalog(
        models=[default, "anthropic:claude-opus-4-8", custom],
        profiles={},
        providers={
            provider: CatalogProvider(state=ProviderAuthState.MANAGED)
            for provider in ("databricks", "anthropic")
        },
    )

    async def load_catalog(*_args):
        return catalog

    class Harness(App):
        pass

    original_recommended = ms._RECOMMENDED_MODELS
    original_footer = ms.ModelSelectorScreen._update_footer
    try:
        assert _boot.extend_recommended_models() is True
        assert _boot.rebrand_model_selector_footer() is True
        monkeypatch.setattr(
            _boot,
            "_gateway_picker_inventory",
            lambda current_spec=None: SimpleNamespace(names=(default.split(":", 1)[1], "team-chat")),
        )
        assert _boot.prefer_databricks_models() is True

        async def render():
            app = Harness()
            async with app.run_test(size=(100, 40)) as pilot:
                screen = ms.ModelSelectorScreen(
                    current_model="databricks-gpt-oss-120b",
                    current_provider="databricks",
                    default_scope=None,
                    include_recent_models=False,
                    check_provider_requirements=False,
                    catalog_loader=load_catalog,
                )
                await app.push_screen(screen)
                await pilot.pause()
                assert screen._loaded
                assert default in dict(screen._filtered_models)
                assert custom in dict(screen._filtered_models)
                assert {provider for _, provider in screen._filtered_models} == {"databricks"}
                footer = screen.query_one("#model-detail-footer", Static)
                assert "Context:" in str(footer.content)
                assert "Workspace authentication" in str(footer.content)
                await pilot.press("ctrl+r")
                await pilot.pause()
                assert custom in dict(screen._filtered_models)
                assert {provider for _, provider in screen._filtered_models} == {"databricks"}
                await pilot.press("t", "e", "a", "m")
                await pilot.pause()
                # The upstream selector uses fuzzy matching; a service FQN
                # may also match characters spread across the search term.
                assert screen._filtered_models[0] == (custom, "databricks")
                assert ("anthropic:claude-opus-4-8", "anthropic") not in screen._filtered_models

        asyncio.run(render())
    finally:
        ms._RECOMMENDED_MODELS = original_recommended
        ms.ModelSelectorScreen._update_footer = original_footer


def test_session_cost_report_contents(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    from manta_code.agents import usage as U

    for agent, cost in (("orchestrator", 0.01), ("swe", 0.20)):
        U.record_usage(
            U.UsageRecord(
                agent=agent, model="databricks-gpt-5-4", input_tokens=1000,
                output_tokens=100, cost_usd=cost, thread_id="thr-1",
            )
        )
    report = _boot._session_cost_report("thr-1")
    assert "Session spend: $0.2100" in report
    assert "swe" in report and "orchestrator" in report
    assert "Recent calls" in report
    assert "Today" in report
    assert "manta receipts" in report


def test_session_cost_report_empty_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    report = _boot._session_cost_report(None)
    assert "No spend recorded" in report


def test_cost_command_intercepted(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    app_mod = pytest.importorskip("deepagents_code.app")
    import asyncio

    cls = app_mod.DeepAgentsApp
    original = cls.__dict__["_handle_command"]
    try:
        calls = {"mounted": [], "passed": []}

        async def fake_handle(self, command):
            calls["passed"].append(command)

        cls._handle_command = fake_handle
        assert _boot.add_session_cost_command() is True
        assert _boot.add_session_cost_command() is True  # idempotent

        class _FakeApp:
            _lc_thread_id = "thr-x"

            async def _mount_message(self, widget):
                calls["mounted"].append(widget)

        asyncio.run(cls._handle_command(_FakeApp(), "/cost"))
        assert len(calls["mounted"]) == 2  # echo + report
        assert calls["passed"] == []  # intercepted, not forwarded
        asyncio.run(cls._handle_command(_FakeApp(), "/help"))
        assert calls["passed"] == ["/help"]  # everything else passes through
    finally:
        cls._handle_command = original


def test_cost_command_registered_for_autocomplete():
    cr = pytest.importorskip("deepagents_code.command_registry")
    assert _boot.add_session_cost_command() is True
    assert any(e.name == "/cost" for e in cr.get_slash_commands())
    assert "/cost" in cr.SIDE_EFFECT_FREE  # answers even while the agent is busy
    assert "/cost" not in cr.QUEUE_BOUND
    definitions = [e for e in cr.COMMANDS if e.name == "/cost"]
    assert len(definitions) == 1
    assert definitions[0].aliases == ("/spend",)
