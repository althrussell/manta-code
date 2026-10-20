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

import hashlib
import json
import math
import re
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
    """USD per million tokens; cache rates must be explicitly supplied.

    Cache discounts and write premiums are provider-specific. A missing cache
    rate is unknown, rather than an Anthropic-derived rate for every vendor.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_creation: float | None = None

    def __post_init__(self) -> None:
        for value in (self.input, self.output, self.cache_read, self.cache_creation):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("token prices must be finite and nonnegative")

    def read_rate(self) -> float | None:
        return self.cache_read

    def write_rate(self) -> float | None:
        return self.cache_creation


#: Historical endpoint estimates. Substring matching is retained only for
#: unqualified legacy endpoint names. Unity services require an exact identity.
DEFAULT_PRICING: dict[str, Price] = {
    "claude-opus": Price(15.0, 75.0, cache_read=1.5, cache_creation=18.75),
    "claude-sonnet": Price(3.0, 15.0, cache_read=0.3, cache_creation=3.75),
    "claude-haiku": Price(0.80, 4.0, cache_read=0.08, cache_creation=1.0),
    "claude": Price(3.0, 15.0, cache_read=0.3, cache_creation=3.75),
    "gpt-5-4-mini": Price(input=0.25, output=2.0),
    "gpt-5-mini": Price(input=0.25, output=2.0),
    "gpt-5-4": Price(input=1.25, output=10.0),
    "gpt-5": Price(input=1.25, output=10.0),
    "gpt-oss-120b": Price(input=0.15, output=0.60),
    "gpt-oss": Price(input=0.10, output=0.40),
    "gemini-2-5-pro": Price(input=1.25, output=10.0),
    "gemini": Price(input=0.50, output=3.0),
    "llama": Price(input=0.20, output=0.60),
}

#: Exact service prices are deliberately separate from historical family rates.
#: Add a rate here only with a verified service-specific published price.
DEFAULT_SERVICE_PRICING: dict[str, Price] = {}
PRICING_VERSION = "legacy-estimates-2026-10-20"
_effective_pricing_cache: dict[str, Price] | None = None
_config_pricing_keys: set[str] = set()


def _pricing_identity(model: str) -> str:
    name = model.strip().lower()
    if name.startswith("databricks:"):
        name = name.removeprefix("databricks:")
    return name.removeprefix("/model-services/").removeprefix("model-services/")


def _is_service(model: str) -> bool:
    return bool(re.fullmatch(r"[^./: ]+\.[^./: ]+\.[^./: ]+", model))


def effective_pricing() -> dict[str, Price]:
    """Merge exact service/config rates and historical endpoint estimates."""
    global _effective_pricing_cache, _config_pricing_keys  # noqa: PLW0603
    if _effective_pricing_cache is not None:
        return _effective_pricing_cache
    table = {**DEFAULT_PRICING, **DEFAULT_SERVICE_PRICING}
    _config_pricing_keys = set()
    try:
        from ..config import load_config

        for key, fields in (load_config().pricing or {}).items():
            try:
                identity = _pricing_identity(key)
                table[identity] = Price(**fields)
                _config_pricing_keys.add(identity)
            except Exception:  # noqa: BLE001 - malformed pricing cannot stop inference
                continue
    except Exception:  # noqa: BLE001
        pass
    _effective_pricing_cache = table
    return table


def clear_pricing_cache() -> None:
    global _effective_pricing_cache, _config_pricing_keys  # noqa: PLW0603
    _effective_pricing_cache = None
    _config_pricing_keys = set()


@dataclass(frozen=True)
class PricingMatch:
    price: Price | None = None
    source: str = "unknown"
    version: str = ""
    identity: str = ""


def pricing_for(
    model: str | None,
    pricing: dict[str, Price] | None = None,
    *,
    observed_backend: str | None = None,
) -> PricingMatch:
    """Resolve a rate with auditable provenance, without guessing service families.

    Exact requested identities (including explicit overrides) win, followed by
    an exact observed backend identity. Only legacy endpoint requests may use
    family substrings. A service's opaque name cannot reveal its backend rate.
    """
    if not model:
        return PricingMatch()
    explicit = pricing is not None
    raw_table = effective_pricing() if pricing is None else pricing
    table = {_pricing_identity(key): value for key, value in raw_table.items()}
    name = _pricing_identity(model)
    backend = _pricing_identity(observed_backend) if observed_backend else ""
    key = name if name in table else backend if backend in table else ""
    exact = bool(key)
    if not key and not _is_service(name):
        matches = [candidate for candidate in table if not _is_service(candidate) and candidate in name]
        key = max(matches, key=len, default="")
    if not key:
        return PricingMatch()
    price = table[key]
    configured = explicit or key in _config_pricing_keys
    method = "exact" if exact else "legacy-substring"
    source = f"{'override' if explicit else 'config' if configured else 'builtin'}:{method}:{key}"
    if configured:
        payload = {"key": key, "input": price.input, "output": price.output,
                   "cache_read": price.cache_read, "cache_creation": price.cache_creation}
        version = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    else:
        version = PRICING_VERSION
    return PricingMatch(price, source, version, key)


def price_for(model: str | None, pricing: dict[str, Price] | None = None) -> Price | None:
    """Compatibility accessor for the resolved rate; see :func:`pricing_for`."""
    return pricing_for(model, pricing).price


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
    #: False when either authoritative token total was absent or malformed.
    usage_complete: bool = True
    #: Reasoning is a subset of output, never an additional charge.
    reasoning_tokens: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input(self) -> int:
        """Input tokens neither read from nor written to the cache."""
        return max(self.input_tokens - self.cache_read - self.cache_creation, 0)

    def cost_usd(self, price: Price | None) -> float | None:
        """Cost of this call in USD, or ``None`` when pricing is unknown."""
        if price is None or not self.usage_complete:
            return None
        if self.cache_read and price.read_rate() is None:
            return None
        if self.cache_creation and price.write_rate() is None:
            return None
        per = 1_000_000.0
        return (
            self.uncached_input * price.input
            + self.cache_read * (price.read_rate() or 0.0)
            + self.cache_creation * (price.write_rate() or 0.0)
            + self.output_tokens * price.output
        ) / per


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def extract_breakdown(usage_metadata: Any) -> TokenBreakdown:
    """Read authoritative totals; missing counts never become a free call.

    Standard LangChain metadata is already normalized by the selected protocol
    adapter. Reasoning detail is recorded as a subset of output tokens. Cache
    detail stays explicitly unavailable when the provider does not report it.
    """
    if not isinstance(usage_metadata, dict):
        return TokenBreakdown(usage_complete=False)
    inputs = _token_count(usage_metadata.get("input_tokens"))
    outputs = _token_count(usage_metadata.get("output_tokens"))
    details = usage_metadata.get("input_token_details") or {}
    cache_read = cache_creation = 0
    cache_available = False
    valid_details = True
    if isinstance(details, dict):
        cache_available = "cache_read" in details or "cache_creation" in details
        for key in ("cache_read", "cache_creation"):
            if key in details and _token_count(details[key]) is None:
                valid_details = False
        cache_read = _token_count(details.get("cache_read")) or 0
        cache_creation = _token_count(details.get("cache_creation")) or 0
    output_details = usage_metadata.get("output_token_details") or {}
    reasoning = _token_count(output_details.get("reasoning")) if isinstance(output_details, dict) else None
    complete = inputs is not None and outputs is not None and valid_details
    if inputs is not None and cache_read + cache_creation > inputs:
        complete = False
    if outputs is not None and reasoning is not None and reasoning > outputs:
        complete = False
    return TokenBreakdown(
        input_tokens=inputs or 0,
        output_tokens=outputs or 0,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cache_available=cache_available,
        usage_complete=complete,
        reasoning_tokens=reasoning,
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
    requested_target: str = ""
    requested_service: str = ""
    observed_backend: str | None = None
    surface: str = ""
    protocol: str = ""
    request_id: str | None = None
    usage_complete: bool | None = True
    cache_available: bool | None = None
    reasoning_tokens: int | None = None
    pricing_source: str = ""
    pricing_version: str = ""

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
    # Additive migrations keep historical costs/tokens intact. Historical usage
    # completeness is unknown (NULL), never retroactively asserted as complete.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}
    additions = {
        "requested_target": "TEXT NOT NULL DEFAULT ''",
        "requested_service": "TEXT NOT NULL DEFAULT ''",
        "observed_backend": "TEXT",
        "surface": "TEXT NOT NULL DEFAULT ''",
        "protocol": "TEXT NOT NULL DEFAULT ''",
        "request_id": "TEXT",
        "usage_complete": "INTEGER",
        "cache_available": "INTEGER",
        "reasoning_tokens": "INTEGER",
        "pricing_source": "TEXT NOT NULL DEFAULT ''",
        "pricing_version": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE usage ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                # Another Manta process may have completed this migration.
                if name not in {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}:
                    conn.close()
                    raise
    conn.commit()
    return conn


#: (utc_day, total_usd, fetched_at_monotonic) — short-TTL cache so N economy
#: middleware instances don't each hit SQLite per model call (ADR 0011).
_day_total_cache: tuple[str, float, float] | None = None
_day_total_cache_path: str | None = None
_DAY_TOTAL_TTL_SECONDS = 30.0


def _utc_day_start(now: float | None = None) -> tuple[str, float]:
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(time.time() if now is None else now, tz=_dt.UTC)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.strftime("%Y-%m-%d"), day_start.timestamp()


def today_total_usd(*, path: Path | None = None) -> float:
    """Total USD recorded in the ledger for the current UTC day.

    This is only the known subtotal: unknown charges cannot count toward a
    client USD cap. Use :func:`today_totals` for coverage and presentation.
    Cached with a short TTL and bumped locally by :func:`record_usage`, so the
    per-model-call daily-budget check is a dict read, with periodic refresh to
    pick up other processes' spend (background runners share the ledger).
    """
    global _day_total_cache, _day_total_cache_path  # noqa: PLW0603
    cache_path = str(usage_db_path(path).absolute())
    day, day_start = _utc_day_start()
    now = time.monotonic()
    if (
        _day_total_cache is not None
        and _day_total_cache_path == cache_path
        and _day_total_cache[0] == day
        and now - _day_total_cache[2] < _DAY_TOTAL_TTL_SECONDS
    ):
        return _day_total_cache[1]
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001
        return _day_total_cache[1] if _day_total_cache and _day_total_cache_path == cache_path else 0.0
    try:
        row = conn.execute(
            "SELECT SUM(cost_usd) FROM usage WHERE ts >= ? AND ts < ?", (day_start, day_start + 86400)
        ).fetchone()
        total = float(row[0] or 0.0)
    except Exception:  # noqa: BLE001
        total = _day_total_cache[1] if _day_total_cache and _day_total_cache_path == cache_path else 0.0
    finally:
        conn.close()
    _day_total_cache = (day, total, now)
    _day_total_cache_path = cache_path
    return total


def _bump_today_total(record: UsageRecord, path: Path | None) -> None:
    global _day_total_cache  # noqa: PLW0603
    if record.cost_usd is None or _day_total_cache is None:
        return
    day, start = _utc_day_start()
    if (
        _day_total_cache[0] == day
        and _day_total_cache_path == str(usage_db_path(path).absolute())
        and start <= record.ts < start + 86400
    ):
        _day_total_cache = (day, _day_total_cache[1] + record.cost_usd, _day_total_cache[2])


def record_usage(record: UsageRecord, *, path: Path | None = None) -> None:
    """Append one usage row. Best-effort: never raises into the model loop."""
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001 - accounting must not break a run
        return
    try:
        columns = tuple(UsageRecord.__dataclass_fields__)
        conn.execute(
            f"INSERT INTO usage ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(getattr(record, name) for name in columns),
        )
        conn.commit()
        _bump_today_total(record, path)
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def model_name(model: Any) -> str:
    """Read the requested model identity without loading provider libraries."""
    if model is None:
        return ""
    if isinstance(model, str):
        return model
    for attr in ("model", "model_name", "endpoint", "name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def model_call_identity(model: Any, message: Any | None) -> dict[str, Any]:
    """Separate the requested service from backend identity actually reported.

    ``model_name`` echoed as the requested service does not identify a backend.
    Request/response object ids are not gateway request ids either.
    """
    name = model_name(model)
    target = name.removeprefix("databricks:")
    policy = getattr(model, "target_policy", None)
    policy_target = getattr(policy, "target", None)
    if policy_target is not None:
        target = str(getattr(policy_target, "name", target))
    kind = getattr(policy_target, "kind", None)
    service = target if kind == "service" or (target.count(".") == 2 and "/" not in target) else ""
    metadata = getattr(message, "response_metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    observed = None
    for key in ("resolved_model", "backend_model", "model_name", "model"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value or value.removeprefix("databricks:") == target:
            continue
        # Generic model fields may identify another gateway service rather than
        # a resolved provider model. Only explicit backend fields can disambiguate.
        reported = value.removeprefix("databricks:")
        if key in {"model_name", "model"} and (reported.count(".") == 2 or "model-services/" in reported):
            continue
        observed = value
        break
    request_id = metadata.get("request_id") or metadata.get("_request_id")
    if not isinstance(request_id, str):
        request_id = None
    headers = metadata.get("headers") or {}
    if not request_id and isinstance(headers, dict):
        normalized = {str(key).lower(): value for key, value in headers.items()}
        for key in ("x-request-id", "x-databricks-request-id"):
            if isinstance(normalized.get(key), str):
                request_id = normalized[key]
                break
    return {
        "requested_target": target,
        "requested_service": service,
        "observed_backend": observed,
        "surface": getattr(policy, "surface", None) or getattr(model, "transport_surface", ""),
        "protocol": getattr(policy, "api", None) or getattr(model, "transport_api", ""),
        "request_id": request_id,
    }

def record_model_response(
    model: Any,
    message: Any,
    *,
    agent: str = "orchestrator",
    task: str = "",
    thread_id: str = "",
    path: Path | None = None,
    pricing: dict[str, Price] | None = None,
    scaffold_tokens: int = 0,
    net_new_tokens: int = 0,
) -> UsageRecord:
    """Account an actual response, including direct qualification model calls.

    Uses only returned metadata and configured prices, performs no inference or
    metadata lookup, and preserves missing usage as unknown. The returned record
    is also useful for a caller's running budget; ledger writes are best-effort.
    """
    identity = model_call_identity(model, message)
    breakdown = extract_breakdown(getattr(message, "usage_metadata", None))
    match = pricing_for(model_name(model), pricing, observed_backend=identity["observed_backend"])
    record = UsageRecord(
        agent=agent, model=model_name(model), task=task, thread_id=thread_id,
        input_tokens=breakdown.input_tokens, output_tokens=breakdown.output_tokens,
        cache_read=breakdown.cache_read, cache_creation=breakdown.cache_creation,
        cost_usd=breakdown.cost_usd(match.price), usage_complete=breakdown.usage_complete,
        cache_available=breakdown.cache_available, reasoning_tokens=breakdown.reasoning_tokens,
        pricing_source=match.source, pricing_version=match.version,
        scaffold_tokens=scaffold_tokens, net_new_tokens=net_new_tokens, **identity,
    )
    record_usage(record, path=path)
    return record


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
    priced_calls: int = 0
    unknown_price_calls: int = 0
    missing_usage_calls: int = 0

    @property
    def usage_complete(self) -> bool:
        return self.missing_usage_calls == 0

    @property
    def completeness_label(self) -> str:
        label = f"{self.priced_calls}/{self.calls} calls priced"
        if self.missing_usage_calls:
            label += f"; {self.missing_usage_calls} usage unavailable/unconfirmed"
        return label

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
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown_cost, "
            "SUM(CASE WHEN usage_complete IS NOT 1 THEN 1 ELSE 0 END) AS missing_usage "
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
            cost_known=int(r["unknown_cost"] or 0) == 0 and int(r["missing_usage"] or 0) == 0,
            priced_calls=int(r["calls"]) - int(r["unknown_cost"] or 0),
            unknown_price_calls=int(r["unknown_cost"] or 0),
            missing_usage_calls=int(r["missing_usage"] or 0),
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
        agg.priced_calls += r.priced_calls
        agg.unknown_price_calls += r.unknown_price_calls
        agg.missing_usage_calls += r.missing_usage_calls
    return agg


def today_totals(*, path: Path | None = None) -> AggRow:
    """Today's known subtotal and coverage; unknown charges are not zero spend."""
    return totals(since=_utc_day_start()[1], path=path)


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


# --- receipts (ADR 0012): make the economy *felt* ----------------------------


@dataclass(frozen=True)
class Receipts:
    """A window's spend story: actual cost vs an all-premium baseline."""

    days: int
    calls: int
    total_tokens: int
    actual_usd: float
    premium_baseline_usd: float
    advice_delivered: int
    advice_accepted: int
    cost_known: bool = True
    priced_calls: int = 0
    missing_usage_calls: int = 0

    @property
    def estimated_savings_usd(self) -> float | None:
        if not self.cost_known:
            return None
        return max(0.0, self.premium_baseline_usd - self.actual_usd)


#: Baseline rate used for the counterfactual: "what if every call had run on
#: the premium model" (claude-opus list prices).
_PREMIUM_BASELINE = Price(input=15.0, output=75.0)


def receipts(*, days: int = 7, path: Path | None = None) -> Receipts:
    """Compute the spend receipts for the window (estimates clearly labelled).

    The savings figure is a counterfactual — the same token volumes priced at
    the premium baseline — not a measurement; it answers "what did
    cheap-by-default routing save me" in the only honest way available.
    """
    since = time.time() - days * 86400
    rows = aggregate(by="model", since=since, path=path)
    calls = sum(r.calls for r in rows)
    tokens = sum(r.total_tokens for r in rows)
    actual = sum(r.cost_usd for r in rows)
    baseline = sum(
        (r.input_tokens * _PREMIUM_BASELINE.input + r.output_tokens * _PREMIUM_BASELINE.output)
        / 1_000_000
        for r in rows
    )
    advice = recent_advice(since=since, limit=500, path=path)
    accepted = sum(1 for a in advice if a.delivered.startswith("interrupt:"))
    return Receipts(
        days=days,
        calls=calls,
        total_tokens=tokens,
        actual_usd=actual,
        premium_baseline_usd=baseline,
        advice_delivered=len(advice),
        advice_accepted=accepted,
        cost_known=all(row.cost_known for row in rows),
        priced_calls=sum(row.priced_calls for row in rows),
        missing_usage_calls=sum(row.missing_usage_calls for row in rows),
    )


# --- in-session cost views (/cost in the TUI) --------------------------------


@dataclass(frozen=True)
class CallRow:
    """One model call from the ledger, for per-call drill-down."""

    ts: float
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def thread_totals(thread_id: str, *, path: Path | None = None) -> list[AggRow]:
    """Per-agent spend for one conversation thread (the current session)."""
    if not thread_id:
        return []
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            "SELECT agent AS key, COUNT(*) AS calls, SUM(input_tokens) AS i, "
            "SUM(output_tokens) AS o, SUM(cache_read) AS cr, "
            "SUM(cache_creation) AS cc, SUM(cost_usd) AS cost, "
            "SUM(cost_usd IS NULL) AS unknown, "
            "SUM(usage_complete IS NOT 1) AS missing_usage FROM usage WHERE thread_id = ? "
            "GROUP BY agent ORDER BY cost DESC",
            (thread_id,),
        ).fetchall()
        return [
            AggRow(
                key=r["key"],
                calls=r["calls"],
                input_tokens=int(r["i"] or 0),
                output_tokens=int(r["o"] or 0),
                cache_read=int(r["cr"] or 0),
                cache_creation=int(r["cc"] or 0),
                cost_usd=float(r["cost"] or 0.0),
                cost_known=not r["unknown"] and not r["missing_usage"],
                priced_calls=r["calls"] - (r["unknown"] or 0),
                unknown_price_calls=r["unknown"] or 0,
                missing_usage_calls=r["missing_usage"] or 0,
            )
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def recent_calls(
    thread_id: str, *, limit: int = 6, path: Path | None = None
) -> list[CallRow]:
    """The thread's most recent model calls, newest first (per-call costs)."""
    if not thread_id:
        return []
    try:
        conn = connect(path)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            "SELECT ts, agent, model, input_tokens, output_tokens, cost_usd "
            "FROM usage WHERE thread_id = ? ORDER BY ts DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        return [
            CallRow(
                ts=r["ts"],
                agent=r["agent"],
                model=r["model"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cost_usd=r["cost_usd"],
            )
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
