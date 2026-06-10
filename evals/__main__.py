"""Run the evals: ``python -m evals`` (canned) or ``python -m evals --live``.

Default mode compares the premium-only baseline against Manta's cost-aware
routing on the canned benchmark and exits non-zero if the candidate fails to
win (holds quality at lower-or-equal cost) — the CI tripwire.

``--live`` (ADR 0010 Phase D) runs the benchmark through the real runtime
(``manta run`` against the live workspace) and reports graded quality plus
**real ledger cost** per task. Spends real tokens; never run from CI.
"""

from __future__ import annotations

import argparse
import sys

from .harness import compare, render_comparison, run_suite
from .solvers import cost_aware_solver, premium_solver
from .tasks import BENCHMARK


def _run_canned() -> int:
    baseline = run_suite(premium_solver(), config_name="premium-only")
    candidate = run_suite(cost_aware_solver(), config_name="cost-aware")
    cmp = compare(baseline, candidate)
    print(render_comparison(cmp))
    return 0 if cmp.wins else 1


def _run_live(task_ids: list[str] | None, timeout: int, max_turns: int) -> int:
    from .live import live_solver

    tasks = BENCHMARK
    if task_ids:
        tasks = [t for t in BENCHMARK if t.id in set(task_ids)]
        if not tasks:
            known = ", ".join(t.id for t in BENCHMARK)
            print(f"no matching tasks; known: {known}", file=sys.stderr)
            return 2

    suite = run_suite(
        live_solver(timeout=timeout, max_turns=max_turns),
        config_name="live",
        tasks=tasks,
    )
    print(f"Live eval — {len(suite.results)} task(s) through the real runtime\n")
    for r in suite.results:
        print(
            f"  {r.task_id:<18} score={r.score:.2f}  "
            f"${r.cost_usd:.4f}  {r.tokens} tok  [{r.model}]"
        )
        if r.notes:
            print(f"    {r.notes}")
    print(
        f"\nQuality {suite.quality:.2f}  •  total ${suite.total_cost:.4f}  "
        f"•  {suite.total_tokens} tokens (real ledger figures)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the benchmark through the real runtime (spends tokens)",
    )
    parser.add_argument(
        "--task", action="append", help="restrict --live to specific task id(s)"
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-turns", type=int, default=12)
    args = parser.parse_args(argv)

    if args.live:
        return _run_live(args.task, args.timeout, args.max_turns)
    return _run_canned()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
