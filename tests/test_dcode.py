from __future__ import annotations

import tomllib

import pytest

from manta_code import dcode


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
    assert argv == ["/usr/bin/python3", "-m", "deepagents_code", "-M", "databricks:ep-a"]


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
