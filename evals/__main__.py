"""Run the bundled demo comparison: ``python -m evals``.

Compares the premium-only baseline against Manta's cost-aware routing on the
benchmark and prints the verdict. Exits non-zero if the candidate fails to win
(holds quality at lower-or-equal cost), so the harness doubles as a CI tripwire.
"""

from __future__ import annotations

import sys

from .harness import compare, render_comparison, run_suite
from .solvers import cost_aware_solver, premium_solver


def main() -> int:
    baseline = run_suite(premium_solver(), config_name="premium-only")
    candidate = run_suite(cost_aware_solver(), config_name="cost-aware")
    cmp = compare(baseline, candidate)
    print(render_comparison(cmp))
    return 0 if cmp.wins else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
