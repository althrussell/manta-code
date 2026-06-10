"""Local usage ledger + cost model (ADR 0008, Phase 4).

Every model call Manta makes can be priced and persisted locally so the user can
answer "which agents/models/tasks cost me the most, and how much of that is
scaffolding vs. real work?". The design goals are:

- **Local-only.** The ledger is a SQLite file under ``~/.manta/.state/usage.db``
  (honoring ``MANTA_HOME``). Usage data never leaves the machine.
- **Honest about cache.** Input tokens are split into *cache-read* (cheap),
  *cache-creation* (a one-time write), and *uncached*, each priced separately,
  so reports show real cost and a cache-hit rate — when the endpoint surfaces
  the detail. Where it does not, cache buckets are recorded as ``0`` and the
  hit-rate is reported as unavailable rather than guessed.
- **Honest about estimates.** The scaffolding (system prompt + tool schemas +
  skills + memory) vs. net-new split is a tokenizer estimate reconciled against
  the provider's true input total; it is labelled estimated in reports.

This module is pure and dependency-light (stdlib ``sqlite3`` + dataclasses) so it
is trivially unit-testable and usable from the ``manta cost`` CLI without the
heavy ``agent`` extra installed. The middleware in
:mod:`manta_code.middleware.economy` is the only writer in the hot path.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import user_manta_dir

#: Where the usage ledger lives (relative to the Manta home dir).
USAGE_DB_PATH = Path(".state") / "usage.db"


# --- pricing ---------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """USD price per **1M tokens** for each token bucket.

    ``cache_read`` and ``cache_creation`` default to derivations of ``input``
    (Anthropic's economics: cache reads ~0.1x input, cache writes ~1.25x input)
    so a model only needs to declare ``input``/``output`` to get sane cache
    pricing.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_creation: float | None = None

    def read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.1

    def write_rate(self) -> float:
        return self.cache_creation if self.cache_creation is not None else self.input * 1.25


#: Default per-endpoint pricing (USD / 1M tokens), matched by substring against
#: the model/endpoint name. Approximate list prices kept in one place so they are
#: easy to correct; unknown endpoints fall back to "cost unknown" (token-only).
DEFAULT_PRICING: dict[str, Price] = {
    "claude-opus": Price(input=15.0, output=75.0),
    "claude-sonnet": Price(input=3.0, output=15.0),
    "claude-haiku": Price(input=0.80, output=4.0),
    "claude": Price(input=3.0, output=15.0),
    "gpt-5-5": Price(input=1.25, output=10.0),
    "gpt-5": Price(input=1.25, output=10.0),
    "gpt-oss-120b": Price(input=0.15, output=0.60),
    "gpt-oss": Price(input=0.10, output=0.40),
    "gemini-2-5-pro": Price(input=1.25, output=10.0),
    "gemini": Price(input=0.50, output=3.0),
    "llama": Price(input=0.20, output=0.60),
}


def price_for(model: str | None, pricing: dict[str, Price] | None = None) -> Price | None:
    """Return the best matching :class:`Price` for ``model`` or ``None``.

    Matching is by longest substring key so ``...claude-opus-4-8`` matches the
    ``claude-opus`` entry before the generic ``claude`` entry.
    """
    if not model:
        return None
    table = pricing or DEFAULT_PRICING
    name = model.lower()
    best_key: str | None = None
    for key in table:
        if key in name and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return table[best_key] if best_key else None


# --- token breakdown -------------------------------------------------------


@dataclass
class TokenBreakdown:
    """Token counts for a single model call, split for honest cost accounting."""

    input_tokens: int = 0
    output_tokens: int = 0
    #: Subset of ``input_tokens`` served from the prompt cache (cheap).
    cache_read: int = 0
    #: Subset of ``input_tokens`` written to the cache this call (one-time).
    cache_creation: int = 0
    #: ``True`` when the provider surfaced cache detail; gates hit-rate reporting.
    cache_available: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input(self) -> int:
        """Input tokens neither read from nor written to the cache."""
        return max(self.input_tokens - self.cache_read - self.cache_creation, 0)

    def cost_usd(self, price: Price | None) -> float | None:
        """Cost of this call in USD, or ``None`` when pricing is unknown."""
        if price is None:
            return None
        per = 1_000_000.0
        return (
            self.uncached_input * price.input
            + self.cache_read * price.read_rate()
            + self.cache_creation * price.write_rate()
            + self.output_tokens * price.output
        ) / per


def extract_breakdown(usage_metadata: Any) -> TokenBreakdown:
    """Build a :class:`TokenBreakdown` from a langchain ``usage_metadata`` dict.

    Handles the standard shape (``input_tokens`` / ``output_tokens`` /
    ``input_token_details.{cache_read,cache_creation}``) and degrades to zeros for
    anything missing, so a provider that omits cache detail simply reports no
    cache rather than raising.
    """
    if not isinstance(usage_metadata, dict):
        return TokenBreakdown()
    input_tokens = int(usage_metadata.get("input_tokens") or 0)
    output_tokens = int(usage_metadata.get("output_tokens") or 0)
    details = usage_metadata.get("input_token_details") or {}
    cache_read = 0
    cache_creation = 0
    cache_available = False
    if isinstance(details, dict):
        if "cache_read" in details or "cache_creation" in details:
            cache_available = True
        cache_read = int(details.get("cache_read") or 0)
        cache_creation = int(details.get("cache_creation") or 0)
    return TokenBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cache_available=cache_available,
    )


# --- ledger ----------------------------------------------------------------


@dataclass
class UsageRecord:
    """One persisted model-call row."""

    agent: str = "orchestrator"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost_usd: float | None = None
    #: Tokenizer-estimated scaffolding (system prompt + tools + skills + memory).
    scaffold_tokens: int = 0
    #: Tokenizer-estimated net-new (conversation) tokens.
    net_new_tokens: int = 0
    thread_id: str = ""
    task: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_creation INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    scaffold_tokens INTEGER NOT NULL DEFAULT 0,
    net_new_tokens INTEGER NOT NULL DEFAULT 0,
    thread_id TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage(agent);
"""


def usage_db_path(path: Path | None = None) -> Path:
    return path or (user_manta_dir() / USAGE_DB_PATH)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and migrate) the usage ledger, returning a connection."""
    db_path = usage_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def record_usage(record: UsageRecord, *, path: Path | None = None) -> None:
    """Append one usage row. Best-effort: never raises into the model loop."""
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001 - accounting must not break a run
        return
    try:
        conn.execute(
            "INSERT INTO usage (ts, agent, model, input_tokens, output_tokens, "
            "cache_read, cache_creation, cost_usd, scaffold_tokens, net_new_tokens, "
            "thread_id, task) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.ts,
                record.agent,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.cache_read,
                record.cache_creation,
                record.cost_usd,
                record.scaffold_tokens,
                record.net_new_tokens,
                record.thread_id,
                record.task,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


# --- aggregation / reporting -----------------------------------------------


@dataclass
class AggRow:
    """One grouped aggregation row for ``manta cost`` rendering."""

    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    cost_usd: float
    cost_known: bool

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """Cache reads / total input, or ``None`` when there is no cache data."""
        if self.cache_read == 0 and self.cache_creation == 0:
            return None
        if self.input_tokens == 0:
            return None
        return self.cache_read / self.input_tokens


_GROUP_COLUMNS = {"agent": "agent", "model": "model", "task": "task"}


def aggregate(
    *,
    by: str = "agent",
    since: float | None = None,
    agent: str | None = None,
    path: Path | None = None,
) -> list[AggRow]:
    """Return usage grouped by ``by`` (``agent`` / ``model`` / ``task``).

    ``since`` filters to rows at/after a unix timestamp; ``agent`` drills into a
    single agent. Rows are sorted by cost (desc), then total tokens.
    """
    column = _GROUP_COLUMNS.get(by)
    if column is None:
        raise ValueError(f"cannot group by {by!r}; choose from {sorted(_GROUP_COLUMNS)}")
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001
        return []
    try:
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT {column} AS key, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read) AS cache_read, SUM(cache_creation) AS cache_creation, "
            "SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown_cost "
            f"FROM usage{clause} GROUP BY {column}"
        )
        rows = conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()

    out = [
        AggRow(
            key=str(r["key"] or "(none)"),
            calls=int(r["calls"]),
            input_tokens=int(r["input_tokens"] or 0),
            output_tokens=int(r["output_tokens"] or 0),
            cache_read=int(r["cache_read"] or 0),
            cache_creation=int(r["cache_creation"] or 0),
            cost_usd=float(r["cost_usd"] or 0.0),
            cost_known=int(r["unknown_cost"] or 0) == 0,
        )
        for r in rows
    ]
    out.sort(key=lambda a: (a.cost_usd, a.total_tokens), reverse=True)
    return out


@dataclass
class ScaffoldBreakdown:
    """Aggregate scaffolding-vs-net-new token split (estimated)."""

    scaffold_tokens: int
    net_new_tokens: int

    @property
    def total(self) -> int:
        return self.scaffold_tokens + self.net_new_tokens

    @property
    def overhead_ratio(self) -> float | None:
        """Scaffolding / net-new — how much fixed overhead per unit of work."""
        if self.net_new_tokens == 0:
            return None
        return self.scaffold_tokens / self.net_new_tokens


def scaffold_breakdown(
    *, since: float | None = None, agent: str | None = None, path: Path | None = None
) -> ScaffoldBreakdown:
    """Return the estimated scaffolding vs. net-new token split over a window."""
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001
        return ScaffoldBreakdown(0, 0)
    try:
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        row = conn.execute(
            "SELECT SUM(scaffold_tokens) AS s, SUM(net_new_tokens) AS n "
            f"FROM usage{clause}",
            params,
        ).fetchone()
    except Exception:  # noqa: BLE001
        return ScaffoldBreakdown(0, 0)
    finally:
        conn.close()
    return ScaffoldBreakdown(int(row["s"] or 0), int(row["n"] or 0))


def totals(*, since: float | None = None, path: Path | None = None) -> AggRow:
    """Return a single grand-total row across all agents (for ``manta budget``)."""
    rows = aggregate(by="agent", since=since, path=path)
    agg = AggRow("total", 0, 0, 0, 0, 0, 0.0, True)
    for r in rows:
        agg.calls += r.calls
        agg.input_tokens += r.input_tokens
        agg.output_tokens += r.output_tokens
        agg.cache_read += r.cache_read
        agg.cache_creation += r.cache_creation
        agg.cost_usd += r.cost_usd
        agg.cost_known = agg.cost_known and r.cost_known
    return agg


# --- advice ledger (ADR 0010, Phase C) --------------------------------------


@dataclass
class AdviceRecord:
    """One recommendation the advisor delivered (or tried to)."""

    agent: str
    kind: str  # escalate | downgrade | budget_tradeoff
    severity: str  # note | interrupt
    message: str
    model: str = ""
    thread_id: str = ""
    delivered: str = ""  # note | interrupt | log
    accepted: int | None = None  # 1/0 once outcomes are tracked; NULL = unknown
    ts: float = field(default_factory=time.time)


_ADVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered TEXT NOT NULL DEFAULT '',
    accepted INTEGER
);
CREATE INDEX IF NOT EXISTS idx_advice_ts ON advice(ts);
"""


def record_advice(record: AdviceRecord, *, path: Path | None = None) -> None:
    """Append one advice row. Best-effort: never raises into the model loop."""
    try:
        conn = connect(path)
        conn.executescript(_ADVICE_SCHEMA)
    except Exception:  # noqa: BLE001
        return
    try:
        conn.execute(
            "INSERT INTO advice (ts, agent, thread_id, model, kind, severity, "
            "message, delivered, accepted) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record.ts,
                record.agent,
                record.thread_id,
                record.model,
                record.kind,
                record.severity,
                record.message,
                record.delivered,
                record.accepted,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def recent_advice(
    *, since: float | None = None, limit: int = 20, path: Path | None = None
) -> list[AdviceRecord]:
    """Recent advisor recommendations, newest first."""
    try:
        conn = connect(path)
        conn.executescript(_ADVICE_SCHEMA)
    except Exception:  # noqa: BLE001
        return []
    try:
        clause, params = "", []
        if since is not None:
            clause = " WHERE ts >= ?"
            params.append(since)
        rows = conn.execute(
            f"SELECT * FROM advice{clause} ORDER BY ts DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        ).fetchall()
        return [
            AdviceRecord(
                agent=r["agent"],
                kind=r["kind"],
                severity=r["severity"],
                message=r["message"],
                model=r["model"],
                thread_id=r["thread_id"],
                delivered=r["delivered"],
                accepted=r["accepted"],
                ts=r["ts"],
            )
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


# --- offline advice (`manta cost --advise`) ----------------------------------

#: Scaffold:net-new ratio above which the overhead recommendation fires.
ADVISE_SCAFFOLD_RATIO = 1.5

#: Cache-hit rate below which the cache recommendation fires (when the model
#: reported cache detail at all).
ADVISE_CACHE_HIT_FLOOR = 0.3

#: Premium share of calls above which the model-mix recommendation fires.
ADVISE_PREMIUM_SHARE = 0.5


def advise(
    *, since: float | None = None, path: Path | None = None
) -> list[str]:
    """Offline spend-optimization recommendations computed from the ledger.

    Pure reads — the runtime advisor (middleware/advice.py) covers in-session
    signals; this covers the structural ones: scaffolding overhead, cache
    economics, and model mix.
    """
    recommendations: list[str] = []

    sb = scaffold_breakdown(since=since, path=path)
    ratio = sb.overhead_ratio
    if ratio is not None and ratio >= ADVISE_SCAFFOLD_RATIO:
        recommendations.append(
            f"Scaffolding is {ratio:.1f}x your net-new tokens — every call pays "
            "for system prompt + tool/skill schemas before any work. Prune "
            "unused skills/tools and keep agent prompts tight "
            "(`manta agents edit <name>`)."
        )

    rows = aggregate(by="model", since=since, path=path)
    cached_rows = [r for r in rows if (r.cache_read + r.cache_creation) > 0]
    for r in cached_rows:
        hit = r.cache_hit_rate
        if hit is not None and hit < ADVISE_CACHE_HIT_FLOOR:
            recommendations.append(
                f"Cache-hit rate on {r.key} is {hit * 100:.0f}% — prompt caching "
                "is mostly missing. Keep the system prompt and tool list "
                "byte-stable across turns so provider caching can engage."
            )

    if rows:
        premium_calls = sum(
            r.calls
            for r in rows
            if (price_for(r.key) or Price(0, 0)).input >= 3.0
        )
        total_calls = sum(r.calls for r in rows)
        if total_calls > 10 and premium_calls / total_calls >= ADVISE_PREMIUM_SHARE:
            recommendations.append(
                f"{premium_calls} of {total_calls} calls ran on premium models — "
                "check whether routine loops can run on a cheaper default "
                "(`manta agents show <name>` for pins; /model in-session)."
            )

    return recommendations
