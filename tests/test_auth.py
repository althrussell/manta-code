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


def test_list_profiles_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "nope.cfg"))
    assert auth.list_profiles() == []


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
