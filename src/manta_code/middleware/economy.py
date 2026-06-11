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

Reliability: every hot-path operation is guarded. A missing tokenizer, an
unpriced endpoint, an absent ``interrupt`` primitive, or a ledger write error all
degrade to "carry on" rather than breaking the user's run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from ..agents.usage import (
    Price,
    TokenBreakdown,
    UsageRecord,
    extract_breakdown,
    price_for,
    record_usage,
)

logger = logging.getLogger("manta.economy")

#: Fraction of the cap at which we emit a soft heads-up.
SOFT_THRESHOLD = 0.8


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
    Unknown shapes default to ``approve`` (trust-first: a malformed resume
    must not destroy work the human probably just approved).
    """
    try:
        decisions = resume_value.get("decisions") if isinstance(resume_value, dict) else None
        if isinstance(decisions, list) and decisions:
            kind = decisions[0].get("type")
            if kind == "reject":
                return "reject"
    except Exception:  # noqa: BLE001
        pass
    return "approve"


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
    return None


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
            if isinstance(tid, str):
                return tid
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
    ) -> None:
        super().__init__()
        self._agent = agent
        self._max_tokens = max_tokens
        self._max_usd = max_usd
        self._daily_max_usd = daily_max_usd
        self._pricing = pricing
        self._ledger_path = ledger_path
        #: Per-thread running totals: thread_id -> {"tokens", "usd"}.
        self._totals: dict[str, dict[str, float]] = {}
        #: Threads we have already prompted for continuation (ask once per cap).
        self._approved: set[str] = set()
        #: Threads where the human rejected continuing: end turns gracefully.
        self._stopped: set[str] = set()

    @property
    def name(self) -> str:
        return f"Manta.Economy.{self._agent}"

    @property
    def has_budget(self) -> bool:
        return self._max_tokens is not None or self._max_usd is not None

    # --- budget bookkeeping ------------------------------------------------

    def _running(self, thread: str) -> dict[str, float]:
        return self._totals.setdefault(thread, {"tokens": 0.0, "usd": 0.0})

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
        turn gracefully), else ``None``. If no approval channel exists, we
        log and continue rather than aborting.
        """
        if thread in self._stopped:
            return "stop"
        running = self._running(thread)
        over_agent_cap = self.has_budget and self._over_cap(running)
        over_daily_cap = self._over_daily_cap()
        if (not over_agent_cap and not over_daily_cap) or thread in self._approved:
            return None

        if over_agent_cap:
            description = (
                f"Agent '{self._agent}' reached its budget "
                f"({int(running['tokens'])} tokens / ${running['usd']:.2f}). "
                "Approve to continue, reject to stop here."
            )
            args: dict[str, Any] = {
                "agent": self._agent,
                "spent_usd": round(running["usd"], 4),
                "spent_tokens": int(running["tokens"]),
                "max_usd": self._max_usd,
                "max_tokens": self._max_tokens,
            }
        else:
            description = (
                f"Today's Manta spend has reached the configured daily budget "
                f"(${self._daily_max_usd:.2f}). Approve to continue, reject "
                "to stop here."
            )
            args = {"daily_max_usd": self._daily_max_usd}

        try:
            from ..tasks.events import unattended_run

            if unattended_run():
                # No human to ask: log + event, never silently kill the work.
                logger.warning("Manta budget cap reached (unattended): %s", description)
                self._record_budget_event(description)
                self._approved.add(thread)
                return None
        except Exception:  # noqa: BLE001
            pass

        try:
            from langgraph.errors import GraphInterrupt
            from langgraph.types import interrupt
        except Exception:  # noqa: BLE001 - no HITL primitive available
            logger.warning("Manta budget cap reached but no approval channel: %s", description)
            self._approved.add(thread)
            return None
        try:
            resume = interrupt(_hitl_payload("manta_budget_continue", description, args))
            # interrupt() only *returns* on the post-decision re-execution;
            # the first pass raises GraphInterrupt (re-raised below).
            if _resume_decision(resume) == "reject":
                self._stopped.add(thread)
                return "stop"
            self._approved.add(thread)
        except GraphInterrupt:
            # The pause mechanism itself: must propagate to the runtime.
            raise
        except Exception:  # noqa: BLE001 - interrupt outside a graph context
            logger.warning(
                "Manta budget cap reached but the run cannot be paused here; continuing."
            )
            self._approved.add(thread)
        return None

    def _over_daily_cap(self) -> bool:
        if self._daily_max_usd is None:
            return False
        try:
            from ..agents.usage import today_total_usd

            return today_total_usd() >= self._daily_max_usd
        except Exception:  # noqa: BLE001
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
            breakdown: TokenBreakdown = (
                extract_breakdown(getattr(message, "usage_metadata", None))
                if message is not None
                else TokenBreakdown()
            )
            model = _model_name(getattr(request, "model", None))
            price = price_for(model, self._pricing)
            cost = breakdown.cost_usd(price)
            scaffold, net_new = estimate_scaffolding(request)
            thread = _thread_id(request)

            # Task attribution (ADR 0010 Phase C): background tasks export
            # MANTA_TASK_ID, so their spend is drillable per task — not just
            # per agent. Interactive sessions fall back to the agent label.
            import os

            task = os.environ.get("MANTA_TASK_ID") or self._agent
            record_usage(
                UsageRecord(
                    agent=self._agent,
                    model=model,
                    input_tokens=breakdown.input_tokens,
                    output_tokens=breakdown.output_tokens,
                    cache_read=breakdown.cache_read,
                    cache_creation=breakdown.cache_creation,
                    cost_usd=cost,
                    scaffold_tokens=scaffold,
                    net_new_tokens=net_new,
                    thread_id=thread,
                    task=task,
                ),
                path=self._ledger_path,
            )

            running = self._running(thread)
            running["tokens"] += breakdown.total_tokens
            if cost is not None:
                running["usd"] += cost
            if self._over_soft(running) and thread not in self._approved:
                logger.info(
                    "Manta budget heads-up: '%s' at %d tokens / $%.2f.",
                    self._agent,
                    int(running["tokens"]),
                    running["usd"],
                )
        except Exception:  # noqa: BLE001 - accounting must never break a run
            logger.debug("Manta token accounting failed", exc_info=True)

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
        if self._maybe_pause(_thread_id(request)) == "stop":
            return self._stop_message()
        response = handler(request)
        self._account(request, response)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if self._maybe_pause(_thread_id(request)) == "stop":
            return self._stop_message()
        response = await handler(request)
        self._account(request, response)
        return response


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
