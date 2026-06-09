from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import manta_code.dcode as dcode
import manta_code.main as main_mod
from manta_code import auth
from manta_code.main import app

runner = CliRunner()


def test_no_subcommand_launches_interactive(monkeypatch):
    seen = {}

    def fake_launch(*, profile, passthrough):
        seen["profile"] = profile
        seen["passthrough"] = passthrough

    monkeypatch.setattr(main_mod, "_launch_interactive", fake_launch)
    result = runner.invoke(app, ["-p", "prod"])
    assert result.exit_code == 0
    assert seen["profile"] == "prod"
    assert seen["passthrough"] == []


def test_classify_args_launch_and_passthrough():
    # -r and --skill are deepagents-code flags Manta does not define; they must
    # be collected and forwarded verbatim, while -p is extracted.
    mode, profile, passthrough = main_mod.classify_args(["-p", "s1", "-r", "--skill", "demo"])
    assert mode == "launch"
    assert profile == "s1"
    assert passthrough == ["-r", "--skill", "demo"]


def test_classify_args_bare_launch():
    assert main_mod.classify_args([]) == ("launch", None, [])
    assert main_mod.classify_args(["--profile=prod"]) == ("launch", "prod", [])
    assert main_mod.classify_args(["-ps2"]) == ("launch", "s2", [])


def test_classify_args_delegates_subcommands_and_help():
    assert main_mod.classify_args(["doctor"])[0] == "delegate"
    assert main_mod.classify_args(["-p", "s1", "init"])[0] == "delegate"
    assert main_mod.classify_args(["agents"])[0] == "delegate"
    assert main_mod.classify_args(["--help"])[0] == "delegate"


def _patch_discover_to_tmp(monkeypatch, tmp_path):
    from manta_code import subagents

    marker = tmp_path / ".state" / "marker"
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)
    original = subagents.discover_subagents
    monkeypatch.setattr(
        subagents,
        "discover_subagents",
        lambda: original(base_dir=tmp_path, project_root=tmp_path / "none"),
    )


def test_agents_command_lists_subagents(monkeypatch, tmp_path):
    _patch_discover_to_tmp(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0
    assert "planning" in result.stdout
    assert "swe" in result.stdout
    assert "review" in result.stdout


def test_agents_command_shows_one_config(monkeypatch, tmp_path):
    _patch_discover_to_tmp(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "planning"])
    assert result.exit_code == 0
    assert "databricks-claude-opus-4-8" in result.stdout

    missing = runner.invoke(app, ["agents", "nope"])
    assert missing.exit_code == 1


def test_main_entry_launches_when_no_subcommand(monkeypatch):
    seen = {}

    def fake_launch(*, profile, passthrough):
        seen["profile"] = profile
        seen["passthrough"] = passthrough

    monkeypatch.setattr(main_mod, "_launch_interactive", fake_launch)
    monkeypatch.setattr("sys.argv", ["manta", "-p", "s1", "-r"])
    main_mod.main_entry()
    assert seen["profile"] == "s1"
    assert seen["passthrough"] == ["-r"]


def test_subcommand_does_not_launch_runtime(monkeypatch, tmp_path):
    called = {"launched": False}

    def fake_launch(*, profile, passthrough):
        called["launched"] = True

    monkeypatch.setattr(main_mod, "_launch_interactive", fake_launch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert called["launched"] is False
    assert "Initialized" in result.stdout


def test_launch_interactive_invokes_dcode(monkeypatch):
    calls = {}

    def fake_dcode_launch(**kwargs):
        calls.update(kwargs)
        return 0

    # deepagents-code is an optional extra; pretend it is installed so this test
    # runs in CI environments that only install the base/dev deps.
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(dcode, "launch", fake_dcode_launch)
    # Keep the test hermetic: don't hit the workspace for endpoint discovery.
    monkeypatch.setattr(auth, "list_serving_chat_endpoints", lambda profile=None: [])
    main_mod._launch_interactive(profile="s2", passthrough=["-r"])
    assert calls["profile"] == "s2"
    assert calls["passthrough"] == ["-r"]
    assert calls["default_endpoint"]  # non-empty endpoint string
    assert "databricks-gpt-oss-120b" in calls["endpoints"]


def test_resolve_endpoints_merges_discovered(monkeypatch):
    from manta_code.config import MantaConfig

    monkeypatch.setattr(
        auth,
        "list_serving_chat_endpoints",
        lambda profile=None: [
            "databricks-gpt-oss-120b",  # duplicate of the configured default
            "databricks-claude-sonnet-4-5",  # genuinely new, discovered endpoint
        ],
    )
    out = main_mod._resolve_endpoints(MantaConfig(), "s2")
    assert out[0] == "databricks-gpt-oss-120b"  # configured default stays first
    assert "databricks-claude-opus-4-8" in out  # configured extra kept
    assert "databricks-claude-sonnet-4-5" in out  # discovered endpoint added
    assert out.count("databricks-gpt-oss-120b") == 1  # deduped


def test_doctor_reports_checks(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda profile=None: False)
    monkeypatch.setattr(auth, "current_username", lambda profile=None: None)
    # Keep the doctor check hermetic: never touch the real ~/.deepagents config.
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *a, **k: Path("/tmp/manta-doctor"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Preflight" in result.stdout
    assert "deepagents-code" in result.stdout
