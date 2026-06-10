from __future__ import annotations

import sys
import tomllib

import pytest

from manta_code import _boot, dcode


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


# --- build_launch_env ----------------------------------------------------------


def test_build_launch_env_sets_profile(monkeypatch):
    for var in ("MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    env = dcode.build_launch_env("s1", base_env={})
    assert env["DATABRICKS_CONFIG_PROFILE"] == "s1"
    assert env[dcode.SPLASH_SUBHEADER_ENV] == dcode.SPLASH_SUBHEADER


def test_build_launch_env_no_profile_no_var(monkeypatch):
    for var in ("MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    env = dcode.build_launch_env(None, base_env={})
    assert "DATABRICKS_CONFIG_PROFILE" not in env


def test_build_launch_env_does_not_clobber_existing_splash(monkeypatch):
    env = dcode.build_launch_env("s1", base_env={dcode.SPLASH_SUBHEADER_ENV: "custom"})
    assert env[dcode.SPLASH_SUBHEADER_ENV] == "custom"


# --- build_dcode_argv ----------------------------------------------------------


def test_build_argv_injects_default_model():
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


def test_build_run_argv_defaults():
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
    assert argv[argv.index("--json-output") + 1] == "stream-json"
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
    dcode.ensure_dcode_config(["ep-b", "ep-c"], config_path=cfg)  # idempotent union
    data = tomllib.loads(cfg.read_text())
    provider = data["models"]["providers"]["databricks"]
    assert provider["class_path"] == dcode.DATABRICKS_CLASS_PATH
    assert provider["models"] == ["ep-a", "ep-b", "ep-c"]


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


def test_databricks_only_models_filters_to_databricks(monkeypatch):
    model_config = pytest.importorskip("deepagents_code.model_config")

    class _FakeConfig:
        providers = {
            "databricks": {"models": ["databricks-claude-sonnet", "databricks-gpt"]},
            "anthropic": {"models": ["claude-opus"]},
        }

    monkeypatch.setattr(
        model_config.ModelConfig, "load", classmethod(lambda cls: _FakeConfig())
    )
    assert _boot._databricks_only_models() == {
        "databricks": ["databricks-claude-sonnet", "databricks-gpt"]
    }


def test_databricks_only_models_empty_when_provider_absent(monkeypatch):
    model_config = pytest.importorskip("deepagents_code.model_config")

    class _FakeConfig:
        providers = {"anthropic": {"models": ["claude-opus"]}}

    monkeypatch.setattr(
        model_config.ModelConfig, "load", classmethod(lambda cls: _FakeConfig())
    )
    assert _boot._databricks_only_models() == {}


def test_restrict_models_to_databricks_patches_discovery():
    model_config = pytest.importorskip("deepagents_code.model_config")
    original = model_config.get_available_models
    try:
        assert _boot.restrict_models_to_databricks() is True
        assert model_config.get_available_models is _boot._databricks_only_models
    finally:
        model_config.get_available_models = original


def test_rebrand_auth_screen_replaces_screen_internals():
    auth = pytest.importorskip("deepagents_code.widgets.auth")
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


def test_rebrand_model_selector_footer_neutralizes_databricks(monkeypatch):
    model_selector = pytest.importorskip("deepagents_code.widgets.model_selector")
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
        assert "Databricks AI Gateway endpoint" in fake._static.content.plain
    finally:
        screen._update_footer = original


def test_allow_blocking_server_appends_flag(monkeypatch):
    server = pytest.importorskip("deepagents_code.server")
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

    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    app = _FakeApp()

    async def run():
        _boot.switch_databricks_workspace(app, "acme-prod")
        # Let the scheduled restart task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())

    import os

    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "acme-prod"
    assert app.restarted is True
    assert any("Switching to workspace 'acme-prod'" in msg for _, msg in app.notifications)


def test_switch_databricks_workspace_without_restart_hook(monkeypatch):
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
