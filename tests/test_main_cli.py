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
    assert main_mod.classify_args(["--help"])[0] == "delegate"


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

    monkeypatch.setattr(dcode, "launch", fake_dcode_launch)
    main_mod._launch_interactive(profile="s2", passthrough=["-r"])
    assert calls["profile"] == "s2"
    assert calls["passthrough"] == ["-r"]
    assert calls["default_endpoint"]  # non-empty endpoint string
    assert "databricks-claude-sonnet-4-5" in calls["endpoints"]


def test_doctor_reports_checks(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda profile=None: False)
    monkeypatch.setattr(auth, "current_username", lambda profile=None: None)
    # Keep the doctor check hermetic: never touch the real ~/.deepagents config.
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *a, **k: Path("/tmp/manta-doctor"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Preflight" in result.stdout
    assert "deepagents-code" in result.stdout
