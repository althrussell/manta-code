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
    cost_usd: float = 0.0
    tokens: int = 0
    model: str = ""


#: A solver turns a task into an output (text + cost).
Solver = Callable[[EvalTask], EvalOutput]


@dataclass
class TaskResult:
    task_id: str
    category: str
    score: float
    notes: str
    cost_usd: float
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
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.results)

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
    def cost_delta(self) -> float:
        return self.candidate.total_cost - self.baseline.total_cost

    @property
    def cost_savings_pct(self) -> float | None:
        if self.baseline.total_cost == 0:
            return None
        return -self.cost_delta / self.baseline.total_cost * 100.0

    @property
    def quality_ok(self) -> bool:
        return self.quality_delta >= -QUALITY_TOLERANCE

    @property
    def cheaper_or_equal(self) -> bool:
        return self.cost_delta <= 1e-9

    @property
    def wins(self) -> bool:
        """The candidate wins iff it holds quality *and* doesn't cost more."""
        return self.quality_ok and self.cheaper_or_equal


def compare(baseline: SuiteResult, candidate: SuiteResult) -> Comparison:
    return Comparison(baseline=baseline, candidate=candidate)


def render_comparison(cmp: Comparison) -> str:
    """A compact text report (used by the CLI/demo and easy to assert on)."""
    b, c = cmp.baseline, cmp.candidate
    lines = [
        f"Eval over {len(c.results)} tasks",
        f"  {b.config_name:<16} quality={b.quality:.2f} cost=${b.total_cost:.4f} "
        f"tokens={b.total_tokens}",
        f"  {c.config_name:<16} quality={c.quality:.2f} cost=${c.total_cost:.4f} "
        f"tokens={c.total_tokens}",
    ]
    savings = cmp.cost_savings_pct
    if savings is not None:
        lines.append(
            f"  -> quality {cmp.quality_delta:+.2f}, cost {savings:+.0f}% "
            f"({'WIN' if cmp.wins else 'no win'})"
        )
    else:
        lines.append(f"  -> {'WIN' if cmp.wins else 'no win'}")
    return "\n".join(lines)
