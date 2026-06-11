"""Live eval solver: run the benchmark through the real Manta runtime.

ADR 0010 Phase D: the canned harness proves the comparison *logic*; this
solver proves the *system*. Each task runs through ``manta run`` (the enforced
headless path — boot shim, build hook, agents, economy middleware all active)
against the live workspace, gets graded by the same deterministic graders, and
reports **real cost** read back from the local usage ledger — the same ledger
``manta cost`` shows, so the eval's dollars are the user's dollars.

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


def _ledger_window(since: float, run_tag: str) -> tuple[float, int, str]:
    """Total (cost, tokens, dominant model) the tagged eval run recorded.

    The child run exports ``MANTA_TASK_ID=<run_tag>`` so the economy
    middleware stamps its ledger rows with the tag — filtering on it keeps a
    concurrently running session or background task from polluting the eval's
    cost figures. The ``since`` bound is belt-and-braces.
    """
    try:
        from manta_code.agents import usage

        rows = [
            r
            for r in usage.aggregate(by="task", since=since)
            if r.key == run_tag
        ]
        if not rows:
            return 0.0, 0, ""
        cost = sum(r.cost_usd for r in rows)
        tokens = sum(r.total_tokens for r in rows)
        # Dominant model needs the model grouping, restricted to this window;
        # fall back to blank if the cross-check fails.
        model_rows = usage.aggregate(by="model", since=since)
        model = max(model_rows, key=lambda r: r.total_tokens).key if model_rows else ""
        return cost, tokens, model
    except Exception:  # noqa: BLE001 - a missing ledger only loses cost detail
        return 0.0, 0, ""


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
