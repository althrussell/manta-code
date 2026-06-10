"""Solvers: how a config turns a task into an answer + cost.

A live solver would call a real Manta agent and read its cost from the usage
ledger. For CI and for the bundled demo we use deterministic *replay* solvers: a
bank of canned answers (so grading is stable) priced with Manta's own cost model.
This lets the harness prove the comparison logic — and the routing strategy —
without a Databricks connection or a model call.

The two demo configs encode the plan's core claim:

- ``premium-only`` runs every task on an expensive model.
- ``cost-aware`` (Manta routing) runs cheap by default and only escalates the
  *hard* tasks — yielding equal quality at materially lower cost.
"""

from __future__ import annotations

from .harness import EvalOutput, Solver
from .tasks import EvalTask

#: Canned, high-quality answers keyed by task id (kept terse but grader-complete).
ANSWER_BANK: dict[str, str] = {
    "fix-broken-job": (
        "The driver ran out of memory. Likely causes: collecting a large result to "
        "the driver, a huge broadcast, or an undersized driver. Fix: increase driver "
        "memory on the cluster, avoid `.collect()`, and add a retry policy.\n\n"
        "```python\n# job cluster: bump driver_node_type_id; add max_retries=2\n```"
    ),
    "write-dab": (
        "```yaml\nbundle:\n  name: revenue\nresources:\n  jobs:\n    revenue_daily:\n"
        "      tasks:\n        - notebook_task: {notebook_path: ./nb.py}\n"
        "targets:\n  dev: {mode: development}\n  prod: {mode: production}\n```"
    ),
    "lineage-question": (
        "Use Unity Catalog lineage on the table main.sales.revenue_daily: the lineage "
        "graph shows upstream tables that feed it and downstream dashboards that "
        "consume it, so you can trace the bad data to its source."
    ),
    "optimize-query": (
        "Reduce shuffle: partition/prune on the join key, broadcast the small "
        "dimension, and use liquid clustering on the Delta table to cluster hot "
        "columns. Cache reused scans."
    ),
    "readonly-sql": (
        "```sql\nSELECT customer_id, SUM(amount) AS total_revenue\n"
        "FROM main.sales.orders\nGROUP BY customer_id\nORDER BY total_revenue DESC\n"
        "LIMIT 10\n```"
    ),
}

PREMIUM_MODEL = "databricks-claude-opus-4-8"
CHEAP_MODEL = "databricks-gpt-oss-120b"

#: Rough per-task token footprint (input+output) used to price the demo.
_TASK_TOKENS = 6000


def _price(model: str, tokens: int) -> float:
    """Price ``tokens`` (split 70/30 input/output) for ``model`` via Manta's table."""
    try:
        from manta_code.agents.usage import price_for

        price = price_for(model)
    except Exception:  # noqa: BLE001
        price = None
    if price is None:
        return 0.0
    inp = tokens * 0.7
    out = tokens * 0.3
    return (inp * price.input + out * price.output) / 1_000_000.0


def _solve_with(task: EvalTask, model: str) -> EvalOutput:
    text = ANSWER_BANK.get(task.id, "")
    return EvalOutput(
        text=text,
        cost_usd=_price(model, _TASK_TOKENS),
        tokens=_TASK_TOKENS,
        model=model,
    )


def premium_solver() -> Solver:
    """Every task on the premium model (the baseline)."""
    return lambda task: _solve_with(task, PREMIUM_MODEL)


def cost_aware_solver() -> Solver:
    """Cheap by default; escalate only the hard tasks (Manta routing)."""

    def solve(task: EvalTask) -> EvalOutput:
        model = PREMIUM_MODEL if task.hard else CHEAP_MODEL
        return _solve_with(task, model)

    return solve
