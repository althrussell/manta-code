#!/usr/bin/env python3
"""Qualify real Gateway agent authoring, isolated work, decisions and integration.

    python scripts/qualify_visible_team.py --live --profile labs --output team.json

Runs exclusively in a disposable Git project, Manta home and Deep Agents home.
Only sanitized acceptance flags are retained; normal SDK profile authentication
reads existing credentials without copying them into the fixture or report.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from qualify_task_resume import Processes, QualificationError


def qualify(args, directory: Path, report: dict) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source))
    sys.dont_write_bytecode = True
    for key in tuple(os.environ):
        if (key.startswith(("MANTA_", "DEEPAGENTS_CODE_SERVER_"))
            or (key.startswith("DATABRICKS_") and key != "DATABRICKS_CONFIG_FILE")
            or key in {"DEEPAGENTS_HOME_IS_DEFAULT", "LANGGRAPH_API_URL"}):
            os.environ.pop(key, None)
    project, temporary = directory / "project", directory / "tmp"
    project.mkdir()
    temporary.mkdir()
    os.environ.update(MANTA_HOME=str(directory / "manta"), DEEPAGENTS_HOME=str(directory / "deepagents"),
        MANTA_PROFILE=args.profile, DATABRICKS_CONFIG_PROFILE=args.profile, MANTA_NOTIFY="0",
        PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(temporary),
        LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    tempfile.tempdir = str(temporary)
    os.chdir(project)

    def git(*argv):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(project), *argv], check=True,
            capture_output=True, text=True, timeout=30).stdout

    git("init", "-q")
    git("config", "user.name", "Manta Qualification")
    git("config", "user.email", "fixture@example.invalid")
    (project / "seed.txt").write_text("visible team fixture\n")
    (project / "notes.txt").write_text("caller baseline\n")
    (project / "test_team.py").write_text("from solution import greeting\n\ndef test_greeting():\n    assert greeting() == 'hello team'\n")
    git("add", ".")
    git("commit", "-qm", "Disposable team fixture")
    (project / "notes.txt").write_text("caller staged\n")
    git("add", "notes.txt")
    (project / "notes.txt").write_text("caller staged\ncaller unstaged\n")
    (project / "user-only.txt").write_text("preserve untracked\n")
    original_index = git("diff", "--cached", "--binary")
    from manta_code.agents.registry import AgentDef
    from manta_code.agents.workspace import AgentWorkspace
    from manta_code.agents.profiles import sync_agent_profiles
    from manta_code.tasks import executor, store
    from manta_code.tasks.approvals import respond_task

    workspace = AgentWorkspace()
    workspace.create(AgentDef(name="team-fixture", model=args.model, memory=False,
        read_only=False, approval=["write_file"], reasoning_effort="low", max_output_tokens=4096,
        task_shell_allow_list=[sys.executable],
        manta_tools=["tasks"], system_prompt=("Follow the assigned fixture steps exactly. Ask for the greeting with "
        "manta_request_input before writing. Use write_file to create solution.py, never shell writes. "
        "Run the provided pytest command with execute; attach solution.py with manta_task_artifact. "
        "Do not delegate, install packages, change any other file, or access external resources.")))
    sync_agent_profiles(workspace.list())
    report["author_sync_dispatch"] = True
    processes = Processes(temporary)
    processes.active()

    def wait(task_id):
        deadline = time.monotonic() + args.timeout + 30
        while time.monotonic() < deadline:
            processes.active()
            record = store.get_task(task_id)
            if record and record.state not in store.ACTIVE_STATES:
                return record
            time.sleep(0.25)
        raise QualificationError("Task did not reach the required state")

    def continuation(record, decision, value=""):
        processes.wait_exit()
        resumed = respond_task(record.id, pending_id=record.checkpoint["pending_id"], decision=decision, value=value)
        if resumed.pid:
            processes.track(resumed.pid)
        assert resumed.id == record.id
        return wait(record.id)

    try:
        command = shlex.join([sys.executable, "-m", "pytest", "-q", "test_team.py"])
        task = executor.submit_task("team-fixture", "Read seed.txt. Ask the user exactly 'Which greeting should the fixture return?' "
            "with manta_request_input, then wait. After the answer, create solution.py defining greeting() returning that exact "
            f"text, using write_file. Run exactly {command!r} as one execute command, with no cd, prefixes, shell operators or redirections. Attach solution.py using manta_task_artifact. Report results.",
            profile=args.profile, timeout=args.timeout, max_turns=20)
        if task.pid:
            processes.track(task.pid)
        report["isolated_worktree"] = task.manifest.get("isolation") == "worktree"
        paused = wait(task.id)
        report["input_checkpoint"] = paused.state == "waiting_input" and bool(paused.checkpoint.get("pending_id"))
        assert report["input_checkpoint"]
        thread = paused.thread_id
        approved = continuation(paused, "answer", "hello team")
        report["approval_checkpoint"] = approved.state == "waiting_approval" and bool(approved.checkpoint.get("pending_id"))
        report["source_unchanged_before_approval"] = not (project / "solution.py").exists()
        assert report["approval_checkpoint"] and report["source_unchanged_before_approval"]
        requests = approved.checkpoint["interrupts"]
        assert all(action["name"] == "write_file" for request in requests.values() for action in request["action_requests"])
        finished = continuation(approved, "approve")
        processes.wait_exit()
        report.update(final_state=finished.state, same_task_and_thread=finished.id == task.id and finished.thread_id == thread,
                      source_isolated_until_integration=not (project / "solution.py").exists())
        assert finished.state == "done" and report["same_task_and_thread"] and report["source_isolated_until_integration"]
        result = executor.review_task(task.id)
        report["observed_test_exit_codes"] = [item["exit_code"] for item in result.get("tests", [])]
        report["observed_execute_events"] = sum(
            1 for event in store.recent_events(task_id=task.id, limit=1000)
            if event.detail == "execute"
        )
        report["observed_tests_passed"] = bool(result.get("tests")) and all(item["exit_code"] == 0 for item in result["tests"])
        report["artifact_attached"] = any(item["path"] == "solution.py" for item in result.get("artifacts", []))
        report["reviewed_change"] = result["changed_files"] == ["solution.py"] and bool(result["patch_sha256"])
        assert report["reviewed_change"], "Only the solution may change; the acceptance test is held fixed"
        report["spend_completeness_visible"] = "usage_complete" in result["spend"] and "cost_complete" in result["spend"]
        executor.integrate_task(task.id, expected_patch_sha256=result["patch_sha256"])
        test = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_team.py"], capture_output=True, timeout=30)
        report["integrated_acceptance_passed"] = test.returncode == 0
        report["caller_changes_preserved"] = (git("diff", "--cached", "--binary") == original_index
            and (project / "notes.txt").read_text() == "caller staged\ncaller unstaged\n"
            and (project / "user-only.txt").read_text() == "preserve untracked\n")
        workspace.set_enabled("team-fixture", False)
        sync_agent_profiles(workspace.list())
        report["persistent_disable"] = "team-fixture" not in {item.name for item in AgentWorkspace().list()}
        required = [key for key, value in report.items() if isinstance(value, bool) and key not in {"passed", "live"}]
        report["passed"] = all(report[key] for key in required)
        assert report["passed"]
    finally:
        processes.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", default="databricks:system.ai.gpt-6-1-sol")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live or args.timeout < 1 or os.name != "posix" or not args.model.startswith("databricks:system.ai."):
        parser.error("Requires --live, POSIX, positive timeout and a system.ai Databricks model")
    output = args.output.expanduser().resolve()
    cwd, environ, temporary, paths = Path.cwd(), dict(os.environ), tempfile.tempdir, sys.path[:]
    original_bytecode = sys.dont_write_bytecode
    report = {"schema_version": 1, "qualification": "visible_agent_team", "live": True,
              "profile": args.profile, "model": args.model, "passed": False}
    def interrupted(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="manta-visible-team-") as name:
            try:
                qualify(args, Path(name).resolve(), report)
            except (Exception, KeyboardInterrupt) as exc:
                report["passed"] = False
                report["error"] = type(exc).__name__
            finally:
                os.chdir(cwd)
    finally:
        os.environ.clear()
        os.environ.update(environ)
        tempfile.tempdir, sys.path[:] = temporary, paths
        sys.dont_write_bytecode = original_bytecode
        signal.signal(signal.SIGTERM, previous)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
