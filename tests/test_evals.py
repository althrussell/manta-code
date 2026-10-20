"""Tests for the eval harness (Phase 5)."""

from __future__ import annotations

import pytest

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


@pytest.fixture
def known_demo_prices(monkeypatch):
    """Synthetic rates test comparison math, never service billing claims."""
    from evals import solvers
    monkeypatch.setattr(solvers, "_price", lambda model, tokens: .1 if model == solvers.PREMIUM_MODEL else .01)


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


def test_cost_aware_wins_against_premium(known_demo_prices):
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


def test_render_comparison_text(known_demo_prices):
    baseline = run_suite(premium_solver(), config_name="premium-only")
    candidate = run_suite(cost_aware_solver(), config_name="cost-aware")
    text = render_comparison(compare(baseline, candidate))
    assert "premium-only" in text
    assert "cost-aware" in text
    assert "WIN" in text


def test_quality_regression_is_not_a_win(known_demo_prices):
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
    monkeypatch.setattr(
        L, "_ledger_window", lambda since, tag: (0.0123, 456, "databricks-gpt-oss-120b")
    )

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
    monkeypatch.setattr(L, "_ledger_window", lambda since, tag: (0.01, 100, "m"))
    from evals.__main__ import main

    assert main(["--live", "--task", "readonly-sql"]) == 0
    out = capsys.readouterr().out
    assert "Live eval" in out
    assert "real ledger figures" in out


def test_unknown_model_price_is_not_zero_or_a_cost_win(monkeypatch, capsys):
    from evals import solvers
    from evals.__main__ import main
    monkeypatch.setattr(solvers, "PREMIUM_MODEL", "custom.team.unpriced-premium")
    monkeypatch.setattr(solvers, "CHEAP_MODEL", "custom.team.unpriced-cheap")
    assert solvers._price("custom.team.unpriced-premium", 6000) is None
    baseline = run_suite(solvers.premium_solver(), config_name="baseline")
    candidate = run_suite(solvers.cost_aware_solver(), config_name="candidate")
    comparison = compare(baseline, candidate)
    assert baseline.total_cost is candidate.total_cost is None
    assert comparison.cost_delta is comparison.cost_savings_pct is None
    assert comparison.cheaper_or_equal is comparison.wins is None
    assert comparison.quality_ok
    assert "no cost/savings verdict" in render_comparison(comparison)
    assert "WIN" not in render_comparison(comparison)
    assert main([]) == 0  # explicitly a quality-only demo when unpriced
    assert "pricing incomplete" in capsys.readouterr().out


def test_partly_priced_suite_preserves_known_cost_without_claiming_total():
    from evals.harness import SuiteResult, TaskResult
    suite = SuiteResult("partial", [TaskResult("a", "x", 1, "", .1, 10, "known"), TaskResult("b", "x", 1, "", None, 20, "unknown")])
    assert suite.total_cost is None
    assert suite.known_cost == .1
    assert suite.priced_tasks == 1
    assert suite.total_tokens == 30
    assert "1/2 tasks priced" in render_comparison(compare(suite, suite))


def test_live_ledger_window_is_task_scoped_and_incomplete_cost_unknown(monkeypatch, tmp_path):
    from evals import live
    from manta_code.agents import usage
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    usage.record_usage(usage.UsageRecord(task="eval-one", model="wanted", input_tokens=10, cost_usd=.2, ts=100, usage_complete=True))
    usage.record_usage(usage.UsageRecord(task="unrelated", model="other", input_tokens=9999, cost_usd=99, ts=101, usage_complete=True))
    assert live._ledger_window(90, "eval-one") == (.2, 10, "wanted")
    usage.record_usage(usage.UsageRecord(task="eval-one", model="unpriced", input_tokens=20, cost_usd=None, ts=102))
    assert live._ledger_window(90, "eval-one") == (None, 30, "unpriced")
    assert live._ledger_window(90, "missing") == (None, 0, "")
