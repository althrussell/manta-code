#!/usr/bin/env python3
"""Qualify a detached task's budget pause and restart against real inference.

Run from a qualified environment (spends tokens):
    python scripts/qualify_task_resume.py --live --profile labs --output resume.json

The agent reads a marker in a disposable project. Manta/Deep Agents state and
temporary server files stay in that disposable directory; the selected
Databricks credentials are read through normal profile authentication.
Only the requested sanitized JSON result is retained. Requires POSIX `ps`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


class QualificationError(RuntimeError):
    """A required continuation property was not observed."""


_SAFE_ERRORS = frozenset({
    "Runner or server did not exit after the checkpoint",
    "Qualification subprocess cleanup did not finish",
    "Task did not finish within the qualification timeout",
    "Task did not persist a resumable budget checkpoint",
    "Resumed task did not satisfy the continuation checks",
    "Process inventory omitted creation times or commands",
})


class Processes:
    """Track descendants and fixture servers, including already orphaned ones."""

    def __init__(self, server_temporary: Path):
        self.known: dict[int, str] = {}
        self.runners: set[int] = set()
        config_prefix = re.escape(str(server_temporary.resolve() / "deepagents_server_"))
        executables = "|".join(re.escape(path) for path in {
            sys.executable, str(Path(sys.executable).resolve())})
        self.fixture_server = re.compile(
            rf"^(?:{executables})\s+-m\s+langgraph_cli\s+dev\s.*?"
            rf"--config\s+{config_prefix}[^/\s]+/langgraph\.json(?:\s|$)"
        )

    def active(self) -> dict[int, str]:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,stat=,lstart=,args="],
            check=True, capture_output=True, text=True, timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=8)
            if len(fields) != 9:
                raise QualificationError("Process inventory omitted creation times or commands")
            pid, parent, state, *rest = fields
            born, command = " ".join(rest[:5]), rest[5]
            if not state.startswith("Z"):
                rows[int(pid)] = (int(parent), born, command)
        live = {pid for pid, born in self.known.items()
                if pid in rows and rows[pid][1] == born}
        live.update(pid for pid in self.runners if pid in rows and pid not in self.known)
        live.update(pid for pid, (_, _, command) in rows.items()
                    if self.fixture_server.search(command))
        while True:
            children = {pid for pid, (parent, _, _) in rows.items() if parent in live}
            if children <= live:
                break
            live.update(children)
        self.known.update({pid: rows[pid][1] for pid in live})
        for pid in self.runners:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        return {pid: rows[pid][1] for pid in live}

    def track(self, pid: int) -> None:
        self.runners.add(pid)
        self.active()

    def wait_exit(self, seconds: int = 30) -> None:
        deadline = time.monotonic() + seconds
        while self.active():
            if time.monotonic() >= deadline:
                raise QualificationError("Runner or server did not exit after the checkpoint")
            time.sleep(0.25)

    def stop(self) -> None:
        # Compare process creation times on every pass so a reused PID is never
        # signalled. The server has its own session, so killing one group is
        # insufficient. Only captured descendants and servers with this unique
        # fixture's temporary --config path are targeted.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in reversed(self.active()):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.1)
        if self.active():
            raise QualificationError("Qualification subprocess cleanup did not finish")


def qualify(args: argparse.Namespace, directory: Path, report: dict) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source))
    sys.dont_write_bytecode = True
    # Clear inherited task/workspace identities before importing path-caching
    # runtime modules. These processes must belong only to this fixture.
    for key in tuple(os.environ):
        if (key.startswith(("MANTA_", "DEEPAGENTS_CODE_SERVER_"))
                or (key.startswith("DATABRICKS_") and key != "DATABRICKS_CONFIG_FILE")
                or key in {
            "DEEPAGENTS_HOME_IS_DEFAULT", "LANGGRAPH_API_URL", "DEEPAGENTS_CODE_SERVER_URL",
        }):
            os.environ.pop(key, None)
    project = directory / "project"
    project.mkdir()
    temporary = directory / "tmp"
    temporary.mkdir()
    os.environ.update(
        MANTA_HOME=str(directory / "manta"), DEEPAGENTS_HOME=str(directory / "deepagents"),
        MANTA_PROFILE=args.profile, DATABRICKS_CONFIG_PROFILE=args.profile, MANTA_NOTIFY="0",
        PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(temporary),
        LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false",
    )
    tempfile.tempdir = str(temporary)
    os.chdir(project)
    marker = f"MANTA_RESUME_{uuid4().hex}"
    (project / "note.txt").write_text(marker + "\n", encoding="utf-8")
    from manta_code.agents.registry import AgentDef, save_agent
    from manta_code.tasks import executor, store

    save_agent(AgentDef(
        name="resume-fixture", model=args.model, memory=False, read_only=True,
        budget_max_tokens=1,
        system_prompt="Use read_file to read the requested file, then return only its exact line.",
    ))
    processes = Processes(temporary)
    processes.active()  # Refuse to start inference if process tracking is unavailable.

    def wait(task_id: str):
        deadline = time.monotonic() + args.timeout + 30
        while time.monotonic() < deadline:
            processes.active()
            record = store.get_task(task_id)
            if record and record.state not in store.ACTIVE_STATES:
                return record
            time.sleep(0.25)
        raise QualificationError("Task did not finish within the qualification timeout")

    try:
        task = executor.submit_task("resume-fixture",
            "Read note.txt with read_file and return only its exact line.",
            profile=args.profile, timeout=args.timeout, max_turns=12)
        processes.track(task.pid)
        report["task_id"] = task.id
        paused = wait(task.id)
        report.update(paused_state=paused.state, thread_id=paused.thread_id,
                      had_resume_mapping=bool(paused.checkpoint.get("resume_value")))
        if paused.state != "paused_budget" or not paused.thread_id or not report["had_resume_mapping"]:
            raise QualificationError("Task did not persist a resumable budget checkpoint")
        processes.wait_exit()
        report["paused_processes_exited"] = True
        resumed = executor.resume_task(task.id)
        processes.track(resumed.pid)
        report.update(resumed_same_task=resumed.id == task.id,
                      resumed_new_runner=resumed.pid != task.pid)
        finished = wait(task.id)
        processes.wait_exit()
        lines = finished.result.strip().splitlines()
        report.update(final_state=finished.state, resumed_same_thread=finished.thread_id == paused.thread_id,
                      output_exact_marker=bool(lines) and lines[-1].strip() == marker,
                      fixture_unchanged=(project / "note.txt").read_text(encoding="utf-8") == marker + "\n")
        required = ("paused_processes_exited", "resumed_same_task", "resumed_new_runner",
                    "resumed_same_thread", "output_exact_marker", "fixture_unchanged")
        if finished.state != "done" or not all(report[key] for key in required):
            raise QualificationError("Resumed task did not satisfy the continuation checks")
        report["passed"] = True
    finally:
        processes.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly authorize real model inference")
    parser.add_argument("--profile", required=True, help="Configured Databricks profile, such as labs")
    parser.add_argument("--output", type=Path, required=True, help="Sanitized JSON result path")
    parser.add_argument("--model", default="databricks:system.ai.gpt-6-1-sol")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per task attempt (default: 300)")
    args = parser.parse_args()
    if not args.live:
        parser.error("Real inference requires --live; no qualification was started")
    if os.name != "posix" or args.timeout < 1 or not args.model.startswith("databricks:system.ai."):
        parser.error("Requires POSIX, a positive timeout, and a databricks:system.ai. model")
    output = args.output.expanduser().resolve()
    original_cwd = Path.cwd()
    original_env = dict(os.environ)
    original_tempdir = tempfile.tempdir
    original_sys_path = sys.path[:]
    original_bytecode = sys.dont_write_bytecode
    report = {"schema_version": 1, "qualification": "detached_task_resume", "live": True,
              "profile": args.profile, "model": args.model, "passed": False}
    def interrupted(*_):
        raise KeyboardInterrupt
    original_sigterm = signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="manta-task-resume-") as temporary:
            directory = Path(temporary).resolve()
            try:
                qualify(args, directory, report)
            except (Exception, KeyboardInterrupt) as error:
                report["passed"] = False
                # Runtime errors/logs may include profile credentials. Emit only
                # static harness messages or the exception class, never a raw
                # log tail or an arbitrary exception string.
                report["error"] = (error.args[0] if isinstance(error, QualificationError)
                    and len(error.args) == 1 and isinstance(error.args[0], str) and error.args[0] in _SAFE_ERRORS
                    else type(error).__name__)
                print(f"Qualification failed: {report['error']}", file=sys.stderr)
            finally:
                os.chdir(original_cwd)
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        tempfile.tempdir = original_tempdir
        sys.path[:] = original_sys_path
        sys.dont_write_bytecode = original_bytecode
        signal.signal(signal.SIGTERM, original_sigterm)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
