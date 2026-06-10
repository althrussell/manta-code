"""Tests for the eval harness (Phase 5)."""

from __future__ import annotations

from evals import BENCHMARK, EvalOutput, compare, run_suite
from evals.graders import (
    Grade,
    contains_all,
    has_code_block,
    penalize_if,
    sql_is_read_only,
    weighted,
)
from evals.harness import render_comparison
from evals.solvers import cost_aware_solver, premium_solver
from evals.tasks import EvalTask


def test_grade_clamped():
    assert Grade(5.0).score == 1.0
    assert Grade(-1.0).score == 0.0


def test_contains_all_fractional():
    g = contains_all(["alpha", "beta"])
    assert g("alpha only").score == 0.5
    assert g("alpha and beta").score == 1.0
    assert g("neither").score == 0.0


def test_has_code_block_and_penalize():
    assert has_code_block()("```py\nx\n```").score == 1.0
    assert has_code_block()("no code").score == 0.0
    assert penalize_if(r"drop table")("DROP TABLE x").score == 0.0
    assert penalize_if(r"drop table")("select 1").score == 1.0


def test_sql_is_read_only_grader():
    assert sql_is_read_only()("SELECT * FROM t").score == 1.0
    assert sql_is_read_only()("DELETE FROM t").score == 0.0


def test_weighted_average():
    g = weighted([(lambda o: Grade(1.0), 3.0), (lambda o: Grade(0.0), 1.0)])
    assert abs(g("x").score - 0.75) < 1e-9


def test_benchmark_answers_score_well():
    # The canned answers should grade highly on every task.
    suite = run_suite(premium_solver(), config_name="premium")
    assert len(suite.results) == len(BENCHMARK)
    assert suite.quality >= 0.95


def test_cost_aware_wins_against_premium():
    baseline = run_suite(premium_solver(), config_name="premium-only")
    candidate = run_suite(cost_aware_solver(), config_name="cost-aware")
    cmp = compare(baseline, candidate)
    # Equal quality (same answers) ...
    assert abs(cmp.quality_delta) < 1e-9
    # ... at strictly lower cost (cheap model on the easy tasks).
    assert candidate.total_cost < baseline.total_cost
    assert cmp.cheaper_or_equal
    assert cmp.wins
    savings = cmp.cost_savings_pct
    assert savings is not None and savings > 0


def test_render_comparison_text():
    baseline = run_suite(premium_solver(), config_name="premium-only")
    candidate = run_suite(cost_aware_solver(), config_name="cost-aware")
    text = render_comparison(compare(baseline, candidate))
    assert "premium-only" in text
    assert "cost-aware" in text
    assert "WIN" in text


def test_quality_regression_is_not_a_win():
    good = run_suite(premium_solver(), config_name="good")

    def bad_solver(task: EvalTask) -> EvalOutput:
        return EvalOutput(text="", cost_usd=0.0)  # empty answers grade ~0

    bad = run_suite(bad_solver, config_name="bad")
    cmp = compare(good, bad)
    # Cheaper, but quality collapsed -> must not count as a win.
    assert cmp.cheaper_or_equal
    assert not cmp.quality_ok
    assert not cmp.wins


def test_demo_main_returns_zero():
    from evals.__main__ import main

    assert main([]) == 0


def test_live_mode_rejects_unknown_task(capsys):
    from evals.__main__ import main

    assert main(["--live", "--task", "not-a-task"]) == 2
    assert "no matching tasks" in capsys.readouterr().err


def test_live_solver_grades_run_output(monkeypatch):
    # The live solver shells out to `manta run` and prices from the ledger;
    # stub both so the wiring is testable offline.
    import subprocess

    from evals import live as L
    from evals.tasks import BENCHMARK

    class _Done:
        returncode = 0
        stdout = "SELECT * FROM sales ORDER BY total DESC LIMIT 10"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(L, "_ledger_window", lambda since: (0.0123, 456, "databricks-gpt-oss-120b"))

    task = next(t for t in BENCHMARK if t.id == "readonly-sql")
    output = L.live_solver()(task)
    assert output.cost_usd == 0.0123
    assert output.tokens == 456
    grade = task.grader(output.text)
    assert grade.score > 0.5  # the real grader runs on the captured stdout


def test_live_mode_renders_summary(monkeypatch, capsys):
    import subprocess

    from evals import live as L

    class _Done:
        returncode = 0
        stdout = "SELECT 1"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(L, "_ledger_window", lambda since: (0.01, 100, "m"))
    from evals.__main__ import main

    assert main(["--live", "--task", "readonly-sql"]) == 0
    out = capsys.readouterr().out
    assert "Live eval" in out
    assert "real ledger figures" in out
