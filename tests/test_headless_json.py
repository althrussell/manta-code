"""Manta owns stable machine output across upstream CLI upgrades."""
from __future__ import annotations

import json
import os
import sys

import pytest
from typer.testing import CliRunner

from manta_code import dcode
from manta_code.main import app


def test_json_text_captures_both_channels_and_preserves_failure(capsys):
    code = dcode._run_json_output([
        sys.executable, "-c",
        "import sys; print('answer 🌊'); print('diagnostic', file=sys.stderr); sys.exit(124)",
    ], dict(os.environ), "text")
    captured = capsys.readouterr()
    assert code == 124
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1, "type": "result", "success": False, "exit_code": 124,
        "stdout": "answer 🌊\n", "stderr": "diagnostic\n",
    }


def test_stream_json_drains_large_stderr_and_incremental_utf8(capsys):
    script = (
        "import os,sys; "
        "os.write(2,b'e'*262144); "
        "[os.write(1,bytes([b])) for b in '🌊'.encode()]; "
        "sys.exit(3)"
    )
    assert dcode._run_json_output([sys.executable, "-c", script], dict(os.environ), "stream-json") == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert all(event["schema_version"] == 1 for event in events)
    assert "".join(event["text"] for event in events if event["type"] == "stdout") == "🌊"
    assert "".join(event["text"] for event in events if event["type"] == "stderr") == "e" * 262144
    assert events[-1] == {
        "schema_version": 1, "type": "result", "success": False, "exit_code": 3,
    }


def test_stream_json_emits_output_before_process_completion(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "stream-received"
    original = dcode._emit_json_event

    def emit(event):
        original(event)
        if event["type"] == "stdout":
            marker.touch()

    monkeypatch.setattr(dcode, "_emit_json_event", emit)
    script = """
import pathlib, sys, time
marker = pathlib.Path(sys.argv[1])
print('ready', flush=True)
deadline = time.monotonic() + 3
while not marker.exists() and time.monotonic() < deadline:
    time.sleep(.01)
sys.exit(0 if marker.exists() else 7)
"""
    code = dcode._run_json_output([
        sys.executable, "-c", script, str(marker),
    ], dict(os.environ), "stream-json")
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert events[0] == {"schema_version": 1, "type": "stdout", "text": "ready\n"}
    assert events[-1]["success"] is True


@pytest.mark.parametrize("mode", ["text", "stream-json"])
def test_json_output_reports_process_start_failure(mode, capsys, tmp_path):
    assert dcode._run_json_output([str(tmp_path / "missing")], {}, mode) == 127
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "result"
    assert events[-1]["exit_code"] == 127
    assert events[-1]["success"] is False


def test_headless_validates_json_before_any_provisioning(monkeypatch):
    monkeypatch.setattr(dcode, "_target_inventory", lambda *args, **kwargs: pytest.fail("discovery ran"))
    with pytest.raises(dcode.LauncherError, match="--json"):
        dcode.run_headless(profile=None, default_endpoint=None, endpoints=[], message="task", json_output="invalid")


def test_cli_rejects_json_mode_before_discovery(monkeypatch):
    monkeypatch.setattr(dcode, "_target_inventory", lambda *args, **kwargs: pytest.fail("discovery ran"))
    result = CliRunner().invoke(app, ["run", "task", "--json", "invalid"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output


@pytest.mark.parametrize("stream", [False, True])
def test_headless_uses_manta_machine_output_with_upstream_streaming(monkeypatch, stream):
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(dcode, "mark_onboarding_complete", lambda: None)
    monkeypatch.setattr(dcode, "sync_agent_profiles", lambda: None)
    monkeypatch.setattr(dcode, "databricks_provider_params", lambda path: {})
    monkeypatch.setattr(dcode, "build_launch_env", lambda *args, **kwargs: {})
    monkeypatch.setattr(dcode, "_target_inventory", lambda *args, **kwargs: None)
    captured = {}

    def machine(argv, env, mode):
        captured.update(argv=argv, env=env, mode=mode)
        return 3

    monkeypatch.setattr(dcode, "_run_json_output", machine)
    assert dcode.run_headless(
        profile=None, default_endpoint=None, endpoints=[], message="task",
        no_stream=not stream, json_output="stream-json",
    ) == 3
    assert captured["mode"] == "stream-json"
    assert ("--no-stream" in captured["argv"]) is not stream
    assert "--json-output" not in captured["argv"]
    assert "--json" not in captured["argv"]
