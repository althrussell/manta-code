"""Trust-first token economy middleware (ADR 0008, Phase 4).

This middleware turns every model call into an accounting event and, optionally,
a budget checkpoint. The framing is *trust and leverage*, not handcuffs:

- **Accounting (always on).** After each model call it reads the response's
  ``usage_metadata``, splits input tokens into cache-read / cache-creation /
  uncached, prices each bucket, estimates the scaffolding-vs-net-new split from
  the request, and appends a row to the local usage ledger
  (:mod:`manta_code.agents.usage`). Nothing leaves the machine.
- **Budget (opt-in).** When an agent declares ``budget_max_tokens`` /
  ``budget_max_usd``, the middleware tracks the running total for the thread and,
  *before* a call that would run past the cap, **pauses and asks to continue**
  via LangGraph ``interrupt`` — never silently ending the run and losing work.
  At a soft threshold (80% of the cap) it logs a heads-up.

Token estimates can be unavailable, and unpriced requests remain visibly unknown.
Checkpoint enforcement fails closed if the approval primitive cannot pause.
"""

from __future__ import annotations

import logging
import asyncio
import threading
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from ..agents.usage import (
    Price,
    model_call_identity,
    record_model_response,
)

logger = logging.getLogger("manta.economy")

#: Fraction of the cap at which we emit a soft heads-up.
SOFT_THRESHOLD = 0.8
_DECISIONS: dict[tuple, tuple[set[tuple[str, str]], set[tuple[str, str]]]] = {}
_DECISIONS_LOCK = threading.Lock()


def _hitl_payload(action: str, description: str, args: dict[str, Any]) -> dict[str, Any]:
    """An upstream-valid ``HITLRequest`` interrupt payload.

    Design review (ADR 0011) found the TUI validates non-``ask_user``
    interrupts against langchain's ``HITLRequest`` TypedDict and rejects
    anything else — Manta's previous custom dicts never rendered an approval
    prompt at all. ``action_requests`` + ``review_configs`` is the only shape
    that reaches the human.
    """
    return {
        "action_requests": [
            {"name": action, "args": args, "description": description}
        ],
        "review_configs": [
            {"action_name": action, "allowed_decisions": ["approve", "reject"]}
        ],
    }


def _resume_decision(resume_value: Any) -> str:
    """Extract the human's decision from an ``interrupt()`` resume value.

    Upstream resumes with ``{"decisions": [{"type": "approve"|"reject", ...}]}``.
    Unknown shapes reject continuation and retain the completed work.
    """
    try:
        decisions = resume_value.get("decisions") if isinstance(resume_value, dict) else None
        if isinstance(decisions, list) and decisions:
            kind = decisions[0].get("type")
            if kind == "reject":
                return "reject"
    except Exception:  # noqa: BLE001
        pass
    return "approve" if isinstance(resume_value, dict) and any(
        isinstance(d, dict) and d.get("type") == "approve"
        for d in (resume_value.get("decisions") or [])
    ) else "reject"


def _model_name(model: Any) -> str:
    """Best-effort human/endpoint name for a request's model object."""
    if model is None:
        return ""
    if isinstance(model, str):
        return model
    for attr in ("model", "model_name", "endpoint", "name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def _ai_message_with_usage(response: Any) -> Any | None:
    """Pull the usage-bearing AIMessage out of a ModelResponse (or message)."""
    # ModelResponse.result is a list[BaseMessage]; handler may also return a bare
    # AIMessage. Walk results in reverse so the model's own message wins.
    result = getattr(response, "result", None)
    candidates: list[Any]
    if isinstance(result, list):
        candidates = list(reversed(result))
    elif result is not None:
        candidates = [result]
    else:
        candidates = [response]
    for msg in candidates:
        if getattr(msg, "usage_metadata", None):
            return msg
    # Missing usage is itself an accounting event. Keep the AI message so its
    # request/backend metadata is preserved even when token totals are absent.
    for msg in candidates:
        if getattr(msg, "type", None) == "ai" or hasattr(msg, "usage_metadata"):
            return msg
    return None


def _call_identity(model: Any, message: Any | None) -> dict[str, Any]:
    return model_call_identity(model, message)


def _tool_text(tools: Any) -> str:
    """Flatten tool schemas to text for a tokenizer-based scaffolding estimate."""
    if not tools:
        return ""
    parts: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None) or (
            tool.get("name") if isinstance(tool, dict) else None
        )
        desc = getattr(tool, "description", None) or (
            tool.get("description") if isinstance(tool, dict) else None
        )
        schema = getattr(tool, "args", None) or getattr(tool, "args_schema", None)
        parts.append(f"{name or ''} {desc or ''} {schema or ''}")
    return "\n".join(parts)


def estimate_scaffolding(request: Any) -> tuple[int, int]:
    """Estimate (scaffold_tokens, net_new_tokens) for a model request.

    Scaffolding = system prompt + tool/skill/memory schemas (the fixed overhead
    paid before any task work); net-new = the conversation messages. Both are
    tokenizer approximations (the provider only returns a total input count), so
    callers should treat them as estimates. Returns ``(0, 0)`` if anything fails.
    """
    try:
        from langchain_core.messages import SystemMessage
        from langchain_core.messages.utils import count_tokens_approximately

        system = getattr(request, "system_message", None)
        system_text = ""
        if system is not None:
            system_text = str(getattr(system, "content", "") or "")
        scaffold_text = system_text + "\n" + _tool_text(getattr(request, "tools", None))
        scaffold = count_tokens_approximately([SystemMessage(content=scaffold_text)])

        messages = list(getattr(request, "messages", None) or [])
        net_new = count_tokens_approximately(messages) if messages else 0
        return int(scaffold), int(net_new)
    except Exception:  # noqa: BLE001 - estimate is best-effort
        return 0, 0


def _thread_id(request: Any) -> str:
    runtime = getattr(request, "runtime", None)
    for attr in ("thread_id",):
        value = getattr(runtime, attr, None)
        if isinstance(value, str) and value:
            return value
    # Fall back to a config-derived id when available.
    config = getattr(runtime, "config", None) or {}
    if isinstance(config, dict):
        cfg = config.get("configurable") or {}
        if isinstance(cfg, dict):
            tid = cfg.get("thread_id")
            if isinstance(tid, str) and tid:
                return tid
    # The real runtime carries no thread attribute; inside a graph node the
    # live RunnableConfig is reachable via langgraph's contextvar. Without
    # this, every ledger row had an empty thread_id — per-session /cost and
    # per-thread budget tracking silently degraded to a single bucket.
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        tid = (cfg.get("configurable") or {}).get("thread_id")
        if isinstance(tid, str) and tid:
            return tid
    except Exception:  # noqa: BLE001 - outside a graph context (tests)
        pass
    return ""


class TokenEconomyMiddleware(AgentMiddleware):
    """Account every model call; optionally enforce a per-thread budget.

    One instance is attached per agent (the orchestrator gets one labelled
    ``orchestrator``; each Manta subagent gets one labelled with its name) so the
    ledger attributes cost to the right agent without double counting — different
    instances wrap different model calls.
    """

    def __init__(
        self,
        *,
        agent: str = "orchestrator",
        max_tokens: int | None = None,
        max_usd: float | None = None,
        pricing: dict[str, Price] | None = None,
        ledger_path: Any | None = None,
        daily_max_usd: float | None = None,
        budget_mode: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._max_tokens = max_tokens
        self._max_usd = max_usd
        self._daily_max_usd = daily_max_usd
        if budget_mode is None:
            from ..config import load_config
            budget_mode = load_config().budget.mode
        if budget_mode not in {"checkpoint", "advisory"}:
            raise ValueError("budget_mode must be checkpoint or advisory")
        self._budget_mode = budget_mode
        self._pricing = pricing
        self._ledger_path = ledger_path
        #: Per-thread running totals: thread_id -> {"tokens", "usd"}.
        self._totals: dict[str, dict[str, float]] = {}
        # Agent consent is independent from the shared daily threshold. Keep
        # decisions outside graph instances so a helper or graph rebuild sees
        # the same consent, without waiving another cap or a future UTC day.
        from ..agents.usage import usage_db_path
        ledger = str(usage_db_path(ledger_path).resolve())
        self._agent_decision_id = (ledger, "agent", agent, max_tokens, max_usd, budget_mode)
        self._daily_decision_id = (ledger, "daily", daily_max_usd, budget_mode)
        with _DECISIONS_LOCK:
            self._agent_approved, self._agent_stopped = _DECISIONS.setdefault(
                self._agent_decision_id, (set(), set())
            )
            self._daily_approved, self._daily_stopped = _DECISIONS.setdefault(
                self._daily_decision_id, (set(), set())
            )
        self._uncertain_budget_threads: set[tuple[str, str]] = set()
        self._daily_coverage_checked: set[tuple[str, str]] = set()
        self._account_error: str | None = None
        self._attributed_models: dict[int, tuple[Any, Any]] = {}

    @property
    def name(self) -> str:
        return f"Manta.Economy.{self._agent}"

    @property
    def has_budget(self) -> bool:
        return self._max_tokens is not None or self._max_usd is not None

    @property
    def _strict_budget(self) -> bool:
        return self._budget_mode == "checkpoint" and (self.has_budget or self._daily_max_usd is not None)

    # --- budget bookkeeping ------------------------------------------------

    def _running(self, thread: str) -> dict[str, float]:
        running = self._totals.setdefault(thread, {"tokens": 0.0, "usd": 0.0, "unknown_calls": 0.0})
        if thread:
            from ..agents.usage import thread_totals
            for row in thread_totals(thread, path=self._ledger_path, strict=self._strict_budget):
                if row.key == self._agent:
                    running["tokens"] = max(running["tokens"], row.total_tokens)
                    running["usd"] = max(running["usd"], row.cost_usd)
                    running["unknown_calls"] = max(running["unknown_calls"], row.unknown_price_calls)
        return running

    def _over_cap(self, running: dict[str, float]) -> bool:
        if self._max_tokens is not None and running["tokens"] >= self._max_tokens:
            return True
        if self._max_usd is not None and running["usd"] >= self._max_usd:
            return True
        return False

    def _over_soft(self, running: dict[str, float]) -> bool:
        if self._max_tokens is not None and running["tokens"] >= self._max_tokens * SOFT_THRESHOLD:
            return True
        if self._max_usd is not None and running["usd"] >= self._max_usd * SOFT_THRESHOLD:
            return True
        return False

    def _maybe_pause(self, thread: str) -> str | None:
        """If over a cap, pause for an approve/reject decision.

        Uses LangGraph ``interrupt`` (HITLRequest-shaped, so upstream actually
        renders the prompt) — the run is *paused*, never killed. Returns
        ``"stop"`` when the human rejected continuing (the caller ends the
        turn gracefully), else ``None``. Checkpoint mode requires a working
        approval channel; advisory mode records the threshold and continues.
        """
        from langgraph.config import get_config

        try:
            config = get_config()
        except RuntimeError:
            config = {}
        if config.get("configurable", {}).get("__pregel_scratchpad") is None:
            return self._run_budget_check(thread)
        from langgraph.func import task

        @task(name=f"manta_budget_check_{self._agent}")
        def check(thread_id: str) -> str | None:
            # One stable parent task slot contains the variable review loop.
            # Replaying it cannot shift/repeat later completed durable tools.
            return self._run_budget_check(thread_id)

        return check(thread).result()

    def _run_budget_check(self, thread: str) -> str | None:
        while True:
            pending = self._pending_budget_scope(thread)
            try:
                from langgraph.errors import GraphInterrupt
            except Exception as exc:
                raise RuntimeError("Budget checkpoint unavailable; request was not sent") from exc
            try:
                # Node replay may occur after midnight or after another request
                # crosses a second cap. Persist the reviewed scope before the
                # interrupt so its old decision cannot authorize the new scope.
                # Even a currently clear/stopped threshold must restore the
                # original scope first. Otherwise a delayed rejection can be
                # skipped when the day rolls over or ledger/cap is corrected.
                scope = self._checkpoint_budget_scope(pending)
                if scope == "stop":
                    return "stop"
                if scope is None:
                    if pending is None:
                        return None
                    continue
                if scope["budget_mode"] == "advisory":
                    logger.warning("Manta advisory budget reached: %s", scope["description"])
                    self._record_budget_event(scope["description"])
                    self._remember_budget_scope(scope)
                    continue
                resume = self._review_budget_scope(scope)
                rejected = _resume_decision(resume) == "reject"
                self._remember_budget_scope(scope, rejected=rejected)
                if rejected:
                    return "stop"
            except GraphInterrupt:
                raise
            except Exception as exc:
                raise RuntimeError("Budget checkpoint failed; request was not sent") from exc
            # Recheck before dispatch. A newly exceeded cap/day needs its own
            # interrupt, whose durable scope occupies the next replay position.

    def _checkpoint_budget_scope(self, scope: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
        from langgraph.config import get_config

        try:
            config = get_config()
        except RuntimeError:  # Outside a graph: interrupt() fails closed below.
            return scope
        if config.get("configurable", {}).get("__pregel_scratchpad") is None:
            return scope
        from langgraph.func import task

        @task(name=f"manta_budget_scope_{self._agent}")
        def checkpoint(value: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
            return value

        return checkpoint(scope).result()

    def _review_budget_scope(self, scope: dict[str, Any]) -> Any:
        from langgraph.config import get_config
        from langgraph.types import interrupt

        try:
            config = get_config()
        except RuntimeError:
            config = {}
        payload = _hitl_payload("manta_budget_continue", scope["description"], scope["args"])
        if config.get("configurable", {}).get("__pregel_scratchpad") is None:
            return interrupt(payload)
        from langgraph.func import task

        @task(name=f"manta_budget_review_{self._agent}")
        def review(value: dict[str, Any]) -> Any:
            # Separate task resume IDs prevent a newly crossed budget during
            # replay from consuming a later tool's positional approval.
            return interrupt(value)

        return review(payload).result()

    def _remember_budget_scope(self, scope: dict[str, Any], *, rejected: bool = False) -> None:
        with _DECISIONS_LOCK:
            for kind in ("agent", "daily"):
                if scope[f"{kind}_key"] is not None:
                    approved, stopped = _DECISIONS.setdefault(tuple(scope[f"{kind}_decision_id"]), (set(), set()))
                    (stopped if rejected else approved).add(tuple(scope[f"{kind}_key"]))

    def _pending_budget_scope(self, thread: str) -> dict[str, Any] | str | None:
        """Describe only the currently exceeded, unapproved thresholds."""
        from ..agents.usage import _utc_day_start

        agent_key = (thread, "")
        daily_key = (thread, _utc_day_start()[0] if self._daily_max_usd is not None else "")
        if agent_key in self._agent_stopped or daily_key in self._daily_stopped:
            return "stop"
        if self._strict_budget:
            from ..accounting import accounting_error
            if self._account_error or accounting_error(self._ledger_path):
                raise RuntimeError("Budget accounting unavailable; request was not sent. Run manta doctor and repair the usage ledger.")
        if self._daily_max_usd is not None and daily_key not in self._daily_coverage_checked:
            from ..agents.usage import today_totals

            if not today_totals(path=self._ledger_path, strict=self._strict_budget).cost_known:
                self._warn_incomplete_budget(thread)
            self._daily_coverage_checked.add(daily_key)
        running = self._running(thread)
        over_agent_cap = self.has_budget and self._over_cap(running) and agent_key not in self._agent_approved
        over_daily_cap = self._over_daily_cap() and daily_key not in self._daily_approved
        if not over_agent_cap and not over_daily_cap:
            return None

        descriptions: list[str] = []
        args: dict[str, Any] = {"budget_scopes": []}
        if over_agent_cap:
            descriptions.append(
                f"Agent '{self._agent}' reached its budget "
                f"({int(running['tokens'])} tokens / ${running['usd']:.2f}). "
            )
            args.update({
                "agent": self._agent,
                "spent_usd": round(running["usd"], 4),
                "spent_tokens": int(running["tokens"]),
                "max_usd": self._max_usd,
                "max_tokens": self._max_tokens,
                "spend_complete": running.get("unknown_calls", 0) == 0,
                "unpriced_calls": int(running.get("unknown_calls", 0)),
            })
            args["budget_scopes"].append("agent")
        if over_daily_cap:
            descriptions.append(
                f"Today's Manta spend has reached the configured daily budget "
                f"(${self._daily_max_usd:.2f}; UTC {daily_key[1]}). "
            )
            args.update({"daily_max_usd": self._daily_max_usd, "utc_day": daily_key[1]})
            args["budget_scopes"].append("daily")
        return {"description": "".join(descriptions) + "Approve to continue, reject to stop here.",
                "args": args, "agent_key": agent_key if over_agent_cap else None,
                "daily_key": daily_key if over_daily_cap else None,
                "agent_decision_id": self._agent_decision_id,
                "daily_decision_id": self._daily_decision_id, "budget_mode": self._budget_mode}

    def _over_daily_cap(self) -> bool:
        if self._daily_max_usd is None:
            return False
        try:
            from ..agents.usage import today_total_usd

            return today_total_usd(path=self._ledger_path, strict=self._strict_budget) >= self._daily_max_usd
        except Exception:  # noqa: BLE001
            if self._strict_budget:
                raise RuntimeError("Daily budget ledger unavailable; request was not sent")
            return False

    def _record_budget_event(self, description: str) -> None:
        try:
            from ..tasks.store import EventRecord, record_event

            import os

            record_event(
                EventRecord(
                    agent=self._agent,
                    kind="budget",
                    detail=description[:200],
                    task_id=os.environ.get("MANTA_TASK_ID") or None,
                )
            )
        except Exception:  # noqa: BLE001
            pass

    # --- accounting --------------------------------------------------------

    def _account(self, request: Any, response: Any) -> None:
        """Price a completed call and persist a ledger row. Never raises."""
        try:
            message = _ai_message_with_usage(response)
            scaffold, net_new = estimate_scaffolding(request)
            thread = _thread_id(request)

            # Task attribution (ADR 0010 Phase C): background tasks export
            # MANTA_TASK_ID, so their spend is drillable per task — not just
            # per agent. Interactive sessions fall back to the agent label.
            import os

            task = os.environ.get("MANTA_TASK_ID") or self._agent
            record = record_model_response(
                getattr(request, "model", None), message,
                agent=self._agent, task=task, thread_id=thread,
                scaffold_tokens=scaffold, net_new_tokens=net_new,
                pricing=self._pricing, path=self._ledger_path,
                strict=self._strict_budget,
            )
            cost = record.cost_usd

            running = self._totals.setdefault(thread, {"tokens": 0.0, "usd": 0.0, "unknown_calls": 0.0})
            running["tokens"] += record.total_tokens
            if cost is not None:
                running["usd"] += cost
            else:
                running["unknown_calls"] = running.get("unknown_calls", 0) + 1
                if self._max_usd is not None or self._daily_max_usd is not None:
                    self._warn_incomplete_budget(thread)
            if self._over_soft(running) and (thread, "") not in self._agent_approved:
                logger.info(
                    "Manta budget heads-up: '%s' at %d tokens / $%.2f.",
                    self._agent,
                    int(running["tokens"]),
                    running["usd"],
                )
        except Exception as exc:  # Preserve this response; stop before another budgeted request.
            self._account_error = type(exc).__name__
            logger.error("Manta token accounting failed; coverage incomplete", exc_info=True)

    def _warn_incomplete_budget(self, thread: str) -> None:
        from ..agents.usage import _utc_day_start

        key = (thread, _utc_day_start()[0] if self._daily_max_usd is not None else "")
        if key in self._uncertain_budget_threads:
            return
        description = (
            "Manta USD budget coverage is incomplete: missing token usage "
            "or unknown pricing cannot count toward the client USD cap. "
            "Known charges still count; gateway budgets and billing are separate."
        )
        logger.warning(description)
        self._record_budget_event(description)
        self._uncertain_budget_threads.add(key)

    def _stop_message(self) -> Any:
        from langchain_core.messages import AIMessage

        return AIMessage(
            content=(
                "Stopped at the budget cap as requested. Raise the cap "
                "(`manta agents edit` / `[budget] daily_max_usd`) or start a "
                "new session to continue."
            )
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        from ..accounting import accounting_scope, flush_usage
        flush_usage()
        if self._maybe_pause(_thread_id(request)) == "stop":
            return self._stop_message()
        request = self._attribute_model(request)
        scaffold, net_new = estimate_scaffolding(request)
        with accounting_scope(agent=self._agent, thread_id=_thread_id(request), model=request.model,
                              scaffold_tokens=scaffold, net_new_tokens=net_new,
                              pricing=self._pricing, ledger_path=self._ledger_path, budget_guard=self):
            response = handler(request)
        self._account(request, response)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        from ..accounting import accounting_scope, flush_usage
        await asyncio.to_thread(flush_usage)
        if await asyncio.to_thread(self._maybe_pause, _thread_id(request)) == "stop":
            return self._stop_message()
        request = self._attribute_model(request)
        scaffold, net_new = estimate_scaffolding(request)
        with accounting_scope(agent=self._agent, thread_id=_thread_id(request), model=request.model,
                              scaffold_tokens=scaffold, net_new_tokens=net_new,
                              pricing=self._pricing, ledger_path=self._ledger_path, budget_guard=self):
            response = await handler(request)
        await asyncio.to_thread(self._account, request, response)
        return response

    def _attribute_model(self, request: Any) -> Any:
        model = getattr(request, "model", None)
        if callable(getattr(model, "model_copy", None)) and callable(getattr(request, "override", None)):
            metadata = getattr(model, "metadata", None) or {}
            if metadata.get("manta_agent") == self._agent:
                return request
            if id(model) not in self._attributed_models:
                self._attributed_models[id(model)] = (model, model.model_copy(update={"metadata": {**metadata, "manta_agent": self._agent}}))
            return request.override(model=self._attributed_models[id(model)][1])
        return request


def _configured_daily_cap() -> float | None:
    try:
        from ..config import load_config

        return load_config().budget.daily_max_usd
    except Exception:  # noqa: BLE001
        return None


def orchestrator_middleware() -> list[AgentMiddleware]:
    """Orchestrator-level economy middleware (accounting + optional daily cap)."""
    return [
        TokenEconomyMiddleware(
            agent="orchestrator", daily_max_usd=_configured_daily_cap()
        )
    ]


def agent_budget_middleware(defn: Any) -> AgentMiddleware | None:
    """Per-agent economy/budget middleware compiled from an :class:`AgentDef`.

    Always returns a middleware (so per-agent usage is recorded), configured with
    the agent's optional token/dollar caps. Returns ``None`` only if construction
    fails, so a broken definition can't block agent build.
    """
    try:
        return TokenEconomyMiddleware(
            agent=getattr(defn, "name", "agent"),
            max_tokens=getattr(defn, "budget_max_tokens", None),
            max_usd=getattr(defn, "budget_max_usd", None),
            daily_max_usd=_configured_daily_cap(),
        )
    except Exception:  # noqa: BLE001
        return None
