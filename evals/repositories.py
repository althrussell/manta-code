"""Repository outcomes, judged by executable acceptance tests outside the repo.

The agent receives the task and repository, never the hidden grader. A successful
process exit alone earns no credit. Each run records changes, test evidence,
elapsed time and ledger completeness in a standalone JSON artifact.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RepositoryTask:
    id: str
    prompt: str
    files: dict[str, str]
    acceptance: str
    interpreter: str = "python"


TASKS = (
    RepositoryTask(
        "python-bug", "Fix totals.py: sum decimal prices exactly and round the final total to cents with ROUND_HALF_UP. Preserve the total(prices) API returning a string.",
        {"totals.py": "def total(prices):\n    return str(round(sum(float(p) for p in prices), 2))\n"},
        "from totals import total\nassert total(['0.10','0.20']) == '0.30'\nassert total(['1.005']) == '1.01'\nassert total([]) == '0.00'\nassert total(['100000000000.01','0.02']) == '100000000000.03'\n",
    ),
    RepositoryTask(
        "python-refactor", "Refactor records.py grouping to consume any iterable exactly once, preserve first-seen group order, and retain records without mutating them. Keep group_by(records, key) API returning a dict.",
        {"records.py": "def group_by(records, key):\n    keys = {r[key] for r in records}\n    return {k: [r for r in records if r[key] == k] for k in keys}\n"},
        "from records import group_by\nrows=[{'kind':'b','v':1},{'kind':'a','v':2},{'kind':'b','v':3}]\nbefore=repr(rows)\nresult=group_by((r for r in rows),'kind')\nassert list(result) == ['b','a']\nassert result['b'] == [rows[0],rows[2]]\nassert repr(rows)==before\nassert group_by(iter([]),'kind') == {}\n",
    ),
    RepositoryTask(
        "frontend-render", "Fix list.cjs render(items): generate a <ul> of <li>text</li> elements in input order. Escape &, <, >, double and single quotes. Empty input returns <ul></ul>. Keep CommonJS exports.render.",
        {"list.cjs": "exports.render = items => '<ul>' + items.map(x => '<li>'+x+'</li>').join('') + '</ul>';\n"},
        """const assert=require('node:assert/strict');const {render}=require(process.cwd()+'/list.cjs');assert.equal(render([]),'<ul></ul>');assert.equal(render(['a','b']),'<ul><li>a</li><li>b</li></ul>');const s=render([`<img src="x">&'`]);assert.equal(s,'<ul><li>&lt;img src=&quot;x&quot;&gt;&amp;&#39;</li></ul>');""",
        "node",
    ),
    RepositoryTask(
        "databricks-bundle", "Repair databricks.yml so it declares one job named nightly with one notebook task using ./src/job.py, and dev/default plus prod targets. Use serverless compute (no cluster declarations), preserve bundle name fixture. Write JSON-compatible YAML. Do not deploy anything.",
        {"databricks.yml": '{"bundle":{"name":"fixture"},"resources":{"jobs":{"nightly":{"name":"nightly","tasks":[]}}}}\n', "src/job.py": "# Databricks notebook source\nprint('fixture')\n"},
        "import json\nfrom pathlib import Path\nd=json.loads(Path('databricks.yml').read_text())\nassert d['bundle']['name']=='fixture'\nt=d['targets'];assert t['dev']['default'] is True and 'prod' in t\nj=d['resources']['jobs']['nightly'];assert j['name']=='nightly'\ntasks=j['tasks'];assert len(tasks)==1 and tasks[0]['task_key']\nassert tasks[0]['notebook_task']['notebook_path']=='./src/job.py'\nassert not any(k in tasks[0] for k in ('new_cluster','existing_cluster_id','job_cluster_key'))\nassert 'job_clusters' not in j\n",
    ),
)


@dataclass
class RunOutput:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    task_tag: str = ""


@dataclass
class RepositoryResult:
    task_id: str
    passed: bool
    agent_exit_code: int
    acceptance_exit_code: int
    acceptance_output: str
    changed_files: list[str]
    diff: str
    seconds: float
    task_tag: str
    cost_usd: float | None
    tokens: int
    model: str
    runtime_output: str = ""


Solver = Callable[[Path, RepositoryTask], RunOutput]


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text(errors="replace") for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}


def evaluate(task: RepositoryTask, solver: Solver, *, timeout: int = 30) -> RepositoryResult:
    with tempfile.TemporaryDirectory(prefix=f"manta-eval-{task.id}-") as folder:
        root = Path(folder)
        for name, content in task.files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        before = _files(root)
        started = time.monotonic()
        output = solver(root, task)
        after = _files(root)
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        diff = "".join("".join(difflib.unified_diff(
            before.get(name, "").splitlines(True), after.get(name, "").splitlines(True),
            fromfile="a/" + name, tofile="b/" + name,
        )) for name in changed)
        if task.interpreter == "node":
            executable = shutil.which("node")
            command = [executable, "-e", task.acceptance] if executable else None
        else:
            code = "import os,sys;sys.path.insert(0,os.getcwd())\n" + task.acceptance
            command = [sys.executable, "-I", "-c", code]
        if command is None:
            check_code, check_output = 127, "Node.js is required for frontend qualification"
        else:
            try:
                check = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
                check_code, check_output = check.returncode, check.stdout + check.stderr
            except subprocess.TimeoutExpired:
                check_code, check_output = 124, "Acceptance tests exceeded their timeout"
        cost, tokens, model = None, 0, ""
        if output.task_tag:
            from .live import _ledger_window
            cost, tokens, model = _ledger_window(time.time() - (time.monotonic() - started), output.task_tag)
        from manta_code.agents.memory import redact_text
        return RepositoryResult(task.id, output.exit_code == 0 and check_code == 0 and bool(changed),
                                output.exit_code, check_code, check_output[-8000:], changed, diff,
                                time.monotonic() - started, output.task_tag, cost, tokens, model,
                                redact_text((output.stdout + "\n" + output.stderr)[-16000:]))


def live_solver(profile: str | None, model: str | None, timeout: int, max_turns: int) -> Solver:
    def solve(root: Path, task: RepositoryTask) -> RunOutput:
        tag = "repo-eval-" + uuid.uuid4().hex
        env = dict(os.environ, MANTA_TASK_ID=tag, MANTA_ALLOW_ASKS="1")
        # Keep importing this source/installed package after changing to a fixture.
        package_root = str(Path(__file__).resolve().parent.parent / "src")
        env["PYTHONPATH"] = os.pathsep.join([package_root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        command = [sys.executable, "-m", "manta_code.main", "run", task.prompt,
                   "--timeout", str(timeout), "--max-turns", str(max_turns), "-a", "swe"]
        if profile:
            command += ["-p", profile]
        if model:
            command += ["-M", model]
        try:
            run = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                 timeout=timeout + 30, check=False)
            return RunOutput(run.returncode, run.stdout[-16000:], run.stderr[-16000:], tag)
        except subprocess.TimeoutExpired:
            return RunOutput(124, stderr="Agent run exceeded timeout", task_tag=tag)
    return solve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True, help="Run paid inference in disposable fixtures")
    parser.add_argument("--profile")
    parser.add_argument("--model")
    parser.add_argument("--task", action="append", choices=[t.id for t in TASKS])
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    selected = [t for t in TASKS if not args.task or t.id in args.task]
    solver = live_solver(args.profile, args.model, args.timeout, args.max_turns)
    results = []
    for task in selected:
        result = evaluate(task, solver)
        results.append(result)
        print(f"{task.id}: {'PASS' if result.passed else 'FAIL'} ({result.seconds:.1f}s)", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schema_version": 1, "results": [asdict(r) for r in results]}, indent=2))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
