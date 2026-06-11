from __future__ import annotations

from pathlib import Path

from manta_code import auth


def test_resolve_profile_priority(monkeypatch):
    monkeypatch.delenv("MANTA_PROFILE", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    assert auth.resolve_profile() is None
    assert auth.resolve_profile("explicit") == "explicit"

    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "envprof")
    assert auth.resolve_profile() == "envprof"
    # MANTA_PROFILE wins over DATABRICKS_CONFIG_PROFILE.
    monkeypatch.setenv("MANTA_PROFILE", "manta")
    assert auth.resolve_profile() == "manta"
    # Explicit flag still beats env.
    assert auth.resolve_profile("flag") == "flag"


def test_list_profiles_parses_cfg(tmp_path: Path, monkeypatch):
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text(
        "[DEFAULT]\nhost = https://default.example.com\ntoken = x\n\n"
        "[prod]\nhost = https://prod.example.com\ntoken = y\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    profiles = {p.name: p.host for p in auth.list_profiles()}
    assert profiles["DEFAULT"] == "https://default.example.com"
    assert profiles["prod"] == "https://prod.example.com"


def test_list_profiles_skips_reserved_sections(tmp_path: Path, monkeypatch):
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text(
        "[DEFAULT]\nhost = https://default.example.com\ntoken = x\n\n"
        "[__settings__]\nfoo = bar\n\n"
        "[prod]\nhost = https://prod.example.com\ntoken = y\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    names = {p.name for p in auth.list_profiles()}
    assert names == {"DEFAULT", "prod"}
    assert "__settings__" not in names


def test_list_profiles_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "nope.cfg"))
    assert auth.list_profiles() == []


class _FakeEndpoint:
    def __init__(self, name, task):
        self.name = name
        self.task = task


def test_list_serving_chat_endpoints_filters_to_chat(monkeypatch):
    class _FakeServing:
        def list(self):
            return [
                _FakeEndpoint("databricks-claude-sonnet-4-5", "llm/v1/chat"),
                _FakeEndpoint("databricks-gte-large-en", "llm/v1/embeddings"),
                _FakeEndpoint("databricks-meta-llama-3-3-70b-instruct", "llm/v1/chat"),
                _FakeEndpoint("custom-pyfunc", None),
                _FakeEndpoint("", "llm/v1/chat"),
            ]

    class _FakeClient:
        serving_endpoints = _FakeServing()

    monkeypatch.setattr(auth, "resolve_workspace_client", lambda profile=None: _FakeClient())
    names = auth.list_serving_chat_endpoints()
    assert names == [
        "databricks-claude-sonnet-4-5",
        "databricks-meta-llama-3-3-70b-instruct",
    ]


def test_list_serving_chat_endpoints_empty_on_error(monkeypatch):
    def _boom(profile=None):
        raise RuntimeError("no auth")

    monkeypatch.setattr(auth, "resolve_workspace_client", _boom)
    assert auth.list_serving_chat_endpoints() == []


def test_is_authenticated_false_on_error(monkeypatch):
    def boom(_profile=None):
        raise auth.AuthError("no sdk")

    monkeypatch.setattr(auth, "resolve_workspace_client", boom)
    assert auth.is_authenticated() is False
    assert auth.current_username() is None


def test_ensure_auth_returns_profile_when_authenticated(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda profile=None: True)
    assert auth.ensure_auth("prod") == "prod"


def test_ensure_auth_non_interactive_returns_none(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda profile=None: False)
    assert auth.ensure_auth(None, interactive=False) is None


def test_databricks_configured_false_when_nothing_present(tmp_path: Path, monkeypatch):
    for var in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE", "MANTA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "nope.cfg"))
    assert auth.databricks_configured() is False


def test_databricks_configured_via_explicit_profile(tmp_path: Path, monkeypatch):
    for var in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE", "MANTA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "nope.cfg"))
    assert auth.databricks_configured("prod") is True


def test_databricks_configured_via_env(tmp_path: Path, monkeypatch):
    for var in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE", "MANTA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "nope.cfg"))
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.example.com")
    assert auth.databricks_configured() is True


def test_databricks_configured_via_config_file(tmp_path: Path, monkeypatch):
    for var in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE", "MANTA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text("[prod]\nhost = https://prod.example.com\n", encoding="utf-8")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    assert auth.databricks_configured() is True
