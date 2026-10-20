"""Live eval solver: run the benchmark through the real Manta runtime.

ADR 0010 Phase D: the canned harness proves the comparison *logic*; this
solver proves the *system*. Each task runs through ``manta run`` (the enforced
headless path — boot shim, build hook, agents, economy middleware all active)
against the live workspace, gets graded by the same deterministic graders, and
reports recorded token usage and local cost estimates from the same ledger
``manta cost`` shows. Unpriced calls or incomplete usage leave cost unknown;
these estimates do not establish Databricks billing charges.

Spends real tokens; run deliberately (``python -m evals --live``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

from .harness import EvalOutput, Solver
from .tasks import EvalTask

#: Bounded headless-run defaults for one eval task.
LIVE_TASK_TIMEOUT = 300
LIVE_TASK_MAX_TURNS = 12


def _ledger_window(since: float, run_tag: str) -> tuple[float | None, int, str]:
    """Total (cost, tokens, dominant model) the tagged eval run recorded.

    The child run exports ``MANTA_TASK_ID=<run_tag>`` so the economy
    middleware stamps its ledger rows with the tag — filtering on it keeps a
    concurrently running session or background task from polluting the eval's
    cost figures. The ``since`` bound is belt-and-braces.
    """
    try:
        from manta_code.agents import usage

        conn = usage.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, SUM(cost_usd) AS cost, "
                "SUM(input_tokens + output_tokens) AS tokens, "
                "SUM(CASE WHEN cost_usd IS NULL OR usage_complete IS NOT 1 THEN 1 ELSE 0 END) AS incomplete "
                "FROM usage WHERE ts >= ? AND task = ?", (since, run_tag),
            ).fetchone()
            if not row or not row["calls"]:
                return None, 0, ""
            model_row = conn.execute(
                "SELECT model FROM usage WHERE ts >= ? AND task = ? "
                "GROUP BY model ORDER BY SUM(input_tokens + output_tokens) DESC LIMIT 1",
                (since, run_tag),
            ).fetchone()
            cost = float(row["cost"] or 0) if not row["incomplete"] else None
            return cost, int(row["tokens"] or 0), str(model_row["model"] or "") if model_row else ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a missing ledger only loses cost detail
        return None, 0, ""


def live_solver(
    *,
    timeout: int = LIVE_TASK_TIMEOUT,
    max_turns: int = LIVE_TASK_MAX_TURNS,
) -> Solver:
    """A :data:`~evals.harness.Solver` that runs tasks via ``manta run``."""

    def solve(task: EvalTask) -> EvalOutput:
        start = time.time()
        run_tag = f"eval-{task.id}-{uuid.uuid4().hex[:6]}"
        env = dict(os.environ)
        env["MANTA_TASK_ID"] = run_tag
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "manta_code.main",
                "run",
                task.prompt,
                "--timeout",
                str(timeout),
                "--max-turns",
                str(max_turns),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        text = completed.stdout.strip()
        if completed.returncode != 0 and not text:
            text = f"(run failed: exit {completed.returncode})\n{completed.stderr[-2000:]}"
        cost, tokens, model = _ledger_window(start, run_tag)
        return EvalOutput(text=text, cost_usd=cost, tokens=tokens, model=model)

    return solve
