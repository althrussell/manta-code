from __future__ import annotations

import shutil

import pytest

from evals.repositories import RunOutput, TASKS, evaluate


SOLUTIONS = {
    "python-bug": {"totals.py": "from decimal import Decimal, ROUND_HALF_UP\ndef total(prices):\n    return str(sum((Decimal(p) for p in prices),Decimal(0)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))\n"},
    "python-refactor": {"records.py": "def group_by(records,key):\n    groups={}\n    for record in records:\n        groups.setdefault(record[key],[]).append(record)\n    return groups\n"},
    "frontend-render": {"list.cjs": "exports.render=items=>'<ul>'+items.map(x=>'<li>'+x.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;')+'</li>').join('')+'</ul>';"},
    "databricks-bundle": {"databricks.yml": '{"bundle":{"name":"fixture"},"targets":{"dev":{"default":true},"prod":{}},"resources":{"jobs":{"nightly":{"name":"nightly","tasks":[{"task_key":"run","notebook_task":{"notebook_path":"./src/job.py"}}]}}}}'},
}


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_hidden_acceptance_rejects_unchanged_repository(task):
    result = evaluate(task, lambda root, task: RunOutput(0, "I fixed everything"))
    assert result.passed is False
    assert result.changed_files == []


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_repository_outcomes_accept_valid_edits(task):
    if task.interpreter == "node" and not shutil.which("node"):
        pytest.skip("Node.js required for frontend fixture")

    def solve(root, task):
        for name, content in SOLUTIONS[task.id].items():
            (root / name).write_text(content)
        return RunOutput(0)

    result = evaluate(task, solve)
    assert result.passed, result.acceptance_output
    assert result.diff and result.changed_files
    assert result.cost_usd is None  # No inference/usage was invented by the fixture.


def test_valid_changes_with_failed_agent_run_are_failure():
    task = TASKS[0]

    def solve(root, task):
        (root / "totals.py").write_text(SOLUTIONS[task.id]["totals.py"])
        return RunOutput(1, "looks complete")

    result = evaluate(task, solve)
    assert result.acceptance_exit_code == 0
    assert result.passed is False
