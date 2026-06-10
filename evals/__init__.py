"""Manta eval harness (ADR 0008, Phase 5).

A small, local benchmark of real Databricks coding tasks with graded outcomes and
token/cost capture. Its job is to *prove* the wins the rest of the plan claims:

- that Manta's enforced agents + cost-aware routing match or beat a premium-only
  baseline on output quality, and
- that they do so at materially lower token/dollar cost.

The harness is deliberately solver-agnostic: a :data:`Solver` is any callable that
turns an :class:`~evals.tasks.EvalTask` into an :class:`~evals.harness.EvalOutput`
(text + cost + tokens). Tests and CI drive it with deterministic mock solvers; a
live solver that calls a real Manta agent can be dropped in without touching the
graders or the comparison logic. Everything is pure stdlib so it runs in CI with
no Databricks connection.
"""

from .graders import Grade
from .harness import (
    Comparison,
    EvalOutput,
    SuiteResult,
    TaskResult,
    compare,
    run_suite,
)
from .tasks import BENCHMARK, EvalTask

__all__ = [
    "Grade",
    "EvalTask",
    "BENCHMARK",
    "EvalOutput",
    "TaskResult",
    "SuiteResult",
    "Comparison",
    "run_suite",
    "compare",
]
