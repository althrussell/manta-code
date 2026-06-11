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


def test_agents_command_lists_builtins(monkeypatch, tmp_path):
    # Hermetic registry: point MANTA_HOME at a temp dir so no real user agents
    # leak into the listing.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))

    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0
    # Built-in agents always appear, even with an empty registry.
    assert "planning" in result.stdout
    assert "swe" in result.stdout
    assert "review" in result.stdout
    assert "built-in" in result.stdout


def test_agents_show_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))

    result = runner.invoke(app, ["agents", "show", "planning"])
    assert result.exit_code == 0
    assert "databricks-claude-opus-4-8" in result.stdout
    assert "read-only" in result.stdout

    missing = runner.invoke(app, ["agents", "show", "nope"])
    assert missing.exit_code == 1


def test_agents_create_and_delete_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))

    created = runner.invoke(
        app, ["agents", "create", "data-reviewer", "--describe", "review SQL queries"]
    )
    assert created.exit_code == 0
    assert "Created agent" in created.stdout

    shown = runner.invoke(app, ["agents", "show", "data-reviewer"])
    assert shown.exit_code == 0
    # The drafter infers read-only from "review" and db tools from "SQL".
    assert "read-only" in shown.stdout

    deleted = runner.invoke(app, ["agents", "delete", "data-reviewer", "--yes"])
    assert deleted.exit_code == 0
    assert runner.invoke(app, ["agents", "show", "data-reviewer"]).exit_code == 1


def test_agents_memory_add_show_clear(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("langgraph.store.sqlite")
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))

    added = runner.invoke(
        app, ["agents", "memory", "review", "--add", "prefers small PRs; ping me@x.com"]
    )
    assert added.exit_code == 0

    shown = runner.invoke(app, ["agents", "memory", "review"])
    assert shown.exit_code == 0
    assert "prefers small PRs" in shown.stdout
    assert "me@x.com" not in shown.stdout  # redacted on write

    cleared = runner.invoke(app, ["agents", "memory", "review", "--clear"])
    assert cleared.exit_code == 0
    assert "Cleared 1" in cleared.stdout


def test_cost_empty_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    result = runner.invoke(app, ["cost"])
    assert result.exit_code == 0
    assert "No usage recorded" in result.stdout


def test_cost_and_budget_render(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    from manta_code.agents import usage as U

    db = U.usage_db_path()
    U.record_usage(
        U.UsageRecord(agent="swe", model="databricks-gpt-oss-120b", input_tokens=1000,
                      output_tokens=200, cost_usd=0.02, scaffold_tokens=300, net_new_tokens=700),
        path=db,
    )
    U.record_usage(
        U.UsageRecord(agent="planning", model="databricks-claude-opus-4-8", input_tokens=2000,
                      output_tokens=500, cache_read=800, cost_usd=0.50,
                      scaffold_tokens=500, net_new_tokens=1500),
        path=db,
    )

    cost = runner.invoke(app, ["cost", "--breakdown"])
    assert cost.exit_code == 0
    assert "planning" in cost.stdout
    assert "swe" in cost.stdout
    assert "Total" in cost.stdout
    assert "scaffolding" in cost.stdout

    by_model = runner.invoke(app, ["cost", "--by", "model"])
    assert by_model.exit_code == 0
    assert "databricks-claude-opus-4-8" in by_model.stdout

    bad = runner.invoke(app, ["cost", "--by", "nonsense"])
    assert bad.exit_code == 1

    bud = runner.invoke(app, ["budget", "--days", "7"])
    assert bud.exit_code == 0
    assert "Total" in bud.stdout


def test_run_forwards_to_headless(monkeypatch):
    import pytest

    pytest.importorskip("deepagents_code")
    import manta_code.dcode as dcode

    captured = {}

    def fake_run_headless(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(dcode, "run_headless", fake_run_headless)

    result = runner.invoke(
        app, ["run", "fix the job", "--timeout", "30", "--max-turns", "5", "--", "-r"]
    )
    assert result.exit_code == 0
    assert captured["message"] == "fix the job"
    assert captured["timeout"] == 30
    assert captured["max_turns"] == 5
    assert captured["no_stream"] is True
    assert "-r" in captured["passthrough"]


def test_run_propagates_exit_code(monkeypatch):
    import pytest

    pytest.importorskip("deepagents_code")
    import manta_code.dcode as dcode

    monkeypatch.setattr(dcode, "run_headless", lambda **k: 124)
    result = runner.invoke(app, ["run", "task"])
    assert result.exit_code == 124


def test_watch_renders_then_stops(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))

    # Stop the loop on the first sleep so the test doesn't hang.
    def boom(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", boom)
    result = runner.invoke(app, ["watch", "--interval", "0.2"])
    assert result.exit_code == 0
    assert "live spend" in result.stdout.lower()


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


def test_launch_interactive_databricks_optional(monkeypatch):
    # Off Databricks (ADR 0010): launch proceeds with no Databricks default
    # model and no endpoint registration, instead of failing.
    calls = {}

    def fake_dcode_launch(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(dcode, "launch", fake_dcode_launch)
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: False)
    main_mod._launch_interactive(profile=None, passthrough=[])
    assert calls["default_endpoint"] is None
    assert calls["endpoints"] == []


def test_doctor_reports_databricks_optional(monkeypatch):
    monkeypatch.setattr(auth, "databricks_configured", lambda profile=None: False)
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *a, **k: Path("/tmp/manta-doctor"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "not configured (optional)" in result.stdout
