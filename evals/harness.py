"""Run the benchmark under a config, grade it, and compare configs.

The harness separates three concerns:

- **Solving** — a :data:`Solver` produces an :class:`EvalOutput` for a task. This
  is the only part that talks to a model; tests/CI inject deterministic mock
  solvers, a live run injects one that calls a Manta agent.
- **Grading** — each task's grader scores the output (see :mod:`evals.graders`).
- **Comparing** — :func:`compare` decides whether a candidate config *wins*:
  quality at least as good as the baseline, at lower (or equal) cost.

This is what turns "we added cost-aware routing" into a defensible "it cut cost
N% at equal quality," and a regression in either dimension fails CI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .tasks import BENCHMARK, EvalTask

#: Quality is allowed to dip by at most this much for a candidate to still "win".
QUALITY_TOLERANCE = 0.02


@dataclass
class EvalOutput:
    """A solver's answer plus its measured cost."""

    text: str
    cost_usd: float | None = None
    tokens: int = 0
    model: str = ""
    exit_code: int = 0


#: A solver turns a task into an output (text + cost).
Solver = Callable[[EvalTask], EvalOutput]


@dataclass
class TaskResult:
    task_id: str
    category: str
    score: float
    notes: str
    cost_usd: float | None
    tokens: int
    model: str


@dataclass
class SuiteResult:
    config_name: str
    results: list[TaskResult] = field(default_factory=list)

    @property
    def quality(self) -> float:
        """Mean grader score across tasks (0..1)."""
        if not self.results:
            return 0.0
        return sum(r.score for r in self.results) / len(self.results)

    @property
    def total_cost(self) -> float | None:
        if any(r.cost_usd is None for r in self.results):
            return None
        return sum(r.cost_usd for r in self.results if r.cost_usd is not None)

    @property
    def known_cost(self) -> float:
        return sum(r.cost_usd for r in self.results if r.cost_usd is not None)

    @property
    def priced_tasks(self) -> int:
        return sum(r.cost_usd is not None for r in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.results)


def run_suite(
    solver: Solver,
    *,
    config_name: str,
    tasks: Sequence[EvalTask] | None = None,
) -> SuiteResult:
    """Solve and grade every task with ``solver``; return a :class:`SuiteResult`."""
    suite = SuiteResult(config_name=config_name)
    for task in tasks or BENCHMARK:
        output = solver(task)
        grade = task.grader(output.text)
        if output.exit_code:
            from .graders import Grade
            grade = Grade(0, f"Execution failed (exit {output.exit_code}); " + grade.notes)
        suite.results.append(
            TaskResult(
                task_id=task.id,
                category=task.category,
                score=grade.score,
                notes=grade.notes,
                cost_usd=output.cost_usd,
                tokens=output.tokens,
                model=output.model,
            )
        )
    return suite


@dataclass
class Comparison:
    """The verdict comparing a candidate config against a baseline."""

    baseline: SuiteResult
    candidate: SuiteResult

    @property
    def quality_delta(self) -> float:
        return self.candidate.quality - self.baseline.quality

    @property
    def cost_delta(self) -> float | None:
        baseline, candidate = self.baseline.total_cost, self.candidate.total_cost
        return candidate - baseline if baseline is not None and candidate is not None else None

    @property
    def cost_savings_pct(self) -> float | None:
        baseline, delta = self.baseline.total_cost, self.cost_delta
        if baseline is None or baseline == 0 or delta is None:
            return None
        return -delta / baseline * 100.0

    @property
    def quality_ok(self) -> bool:
        return self.quality_delta >= -QUALITY_TOLERANCE

    @property
    def cheaper_or_equal(self) -> bool | None:
        delta = self.cost_delta
        return delta <= 1e-9 if delta is not None else None

    @property
    def wins(self) -> bool | None:
        """The candidate wins iff it holds quality *and* doesn't cost more."""
        return self.cheaper_or_equal if self.quality_ok else False


def compare(baseline: SuiteResult, candidate: SuiteResult) -> Comparison:
    return Comparison(baseline=baseline, candidate=candidate)


def render_comparison(cmp: Comparison) -> str:
    """A compact text report (used by the CLI/demo and easy to assert on)."""
    b, c = cmp.baseline, cmp.candidate
    def cost_label(suite: SuiteResult) -> str:
        total = suite.total_cost
        return f"${total:.4f}" if total is not None else f"unknown (known ${suite.known_cost:.4f}; {suite.priced_tasks}/{len(suite.results)} tasks priced)"
    lines = [
        f"Eval over {len(c.results)} tasks",
        f"  {b.config_name:<16} quality={b.quality:.2f} cost={cost_label(b)} "
        f"tokens={b.total_tokens}",
        f"  {c.config_name:<16} quality={c.quality:.2f} cost={cost_label(c)} "
        f"tokens={c.total_tokens}",
    ]
    savings = cmp.cost_savings_pct
    if savings is not None:
        lines.append(
            f"  -> quality {cmp.quality_delta:+.2f}, cost {savings:+.0f}% "
            f"({'WIN' if cmp.wins else 'no win'})"
        )
    elif cmp.cost_delta is None:
        lines.append(f"  -> quality {cmp.quality_delta:+.2f} ({'held' if cmp.quality_ok else 'regressed'}); no cost/savings verdict: pricing incomplete")
    else:
        lines.append(f"  -> {'WIN' if cmp.wins else 'no win'}")
    return "\n".join(lines)
