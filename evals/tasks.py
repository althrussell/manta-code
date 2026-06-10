"""The benchmark: a small set of real Databricks coding tasks.

Each :class:`EvalTask` pairs a realistic prompt with a deterministic grader. The
set is intentionally small and representative — fix a broken job, author a DAB,
answer a lineage question, optimize a query, write a safe read-only query — so it
runs fast in CI while still exercising the capabilities Manta leads with.
"""

from __future__ import annotations

from dataclasses import dataclass

from .graders import (
    Grader,
    contains_all,
    has_code_block,
    sql_is_read_only,
    weighted,
)


@dataclass
class EvalTask:
    """One benchmark task."""

    id: str
    category: str
    prompt: str
    grader: Grader
    #: Difficulty hint used to decide when premium routing *should* trigger.
    hard: bool = False


BENCHMARK: list[EvalTask] = [
    EvalTask(
        id="fix-broken-job",
        category="jobs",
        prompt=(
            "Our Databricks job 'revenue_daily' failed last night with an "
            "OOM error on the driver. Diagnose likely causes and propose a fix."
        ),
        grader=weighted(
            [
                (contains_all(["driver", "memory"]), 2.0),
                (contains_all(["cluster", "retry"]), 1.0),
                (has_code_block(), 1.0),
            ]
        ),
        hard=True,
    ),
    EvalTask(
        id="write-dab",
        category="dab",
        prompt=(
            "Write a Databricks Asset Bundle (databricks.yml) that defines a job "
            "running a notebook on a serverless cluster across dev and prod targets."
        ),
        grader=weighted(
            [
                (contains_all(["bundle", "resources", "targets"]), 2.0),
                (contains_all(["dev", "prod"]), 1.0),
                (has_code_block(), 1.0),
            ]
        ),
    ),
    EvalTask(
        id="lineage-question",
        category="lineage",
        prompt=(
            "The table main.sales.revenue_daily looks wrong. Explain how you would "
            "use Unity Catalog lineage to find which upstream tables feed it and "
            "which downstream dashboards consume it."
        ),
        grader=weighted(
            [
                (contains_all(["lineage", "upstream", "downstream"]), 2.0),
                (contains_all(["unity catalog", "table"]), 1.0),
            ]
        ),
    ),
    EvalTask(
        id="optimize-query",
        category="performance",
        prompt=(
            "This Spark SQL query over a 2TB Delta table is slow and shuffles a lot. "
            "Suggest concrete optimizations."
        ),
        grader=weighted(
            [
                (contains_all(["partition", "shuffle"]), 1.5),
                # Any modern layout/perf lever earns credit.
                (
                    contains_all(["broadcast"]),
                    0.75,
                ),
                (contains_all(["cluster"]), 0.75),
            ]
        ),
        hard=True,
    ),
    EvalTask(
        id="readonly-sql",
        category="sql",
        prompt=(
            "Write a SQL query (read-only, no mutations) that returns the top 10 "
            "customers by total revenue from main.sales.orders."
        ),
        grader=weighted(
            [
                (sql_is_read_only(), 2.0),
                (contains_all(["select", "order by", "limit"]), 1.0),
            ]
        ),
    ),
]


def benchmark_by_id() -> dict[str, EvalTask]:
    return {t.id: t for t in BENCHMARK}
