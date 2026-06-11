"""Proactive model advice (ADR 0010, Phase C — vision pillar 3).

Watches how a session is *actually going* and recommends model changes, with
tiered, trust-first delivery:

- **Low-stakes advice is an inline note.** When a rule fires, a short
  ``> Manta: …`` advisory is appended to the agent's final answer for that
  turn (never to mid-loop tool-calling messages), recorded in the advice
  ledger, and surfaced in ``manta status``. Nothing pauses.
- **High-stakes advice pauses for a decision.** A budget-threatening premium
  streak reuses the same LangGraph ``interrupt`` approve-to-continue pattern
  the budget governor uses — the user always decides; work is never lost.

Rules (deterministic, v1 — the cold-start prior for the closed-loop routing
the vision aims at; every firing is logged so outcomes can be learned later):

- **Escalate on repeated failures** — a cheap/standard model accumulating tool
  errors is fighting the task: suggest a stronger reasoner.
- **Downgrade on a boilerplate streak** — a premium model emitting a run of
  short outputs is burning premium tokens on routine work: suggest dropping
  down for this loop.
- **Budget trade-off (interrupt)** — spend has crossed the soft threshold of a
  declared budget while running premium: pause once and present the switch.

Reliability: every hot-path operation is guarded; a broken rule or ledger
degrades to "carry on". Disable entirely with ``MANTA_ADVICE=0``.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from ..agents.usage import Price, price_for

logger = logging.getLogger("manta.advice")

#: Env var to disable proactive advice.
_ENV_TOGGLE = "MANTA_ADVICE"

#: Input price (USD/1M tokens) at or above which a model counts as premium.
PREMIUM_INPUT_RATE = 3.0

#: Input price at or below which a model counts as cheap.
CHEAP_INPUT_RATE = 0.30

#: Tool errors within the recent window that trigger escalation advice.
FAILURE_THRESHOLD = 3
FAILURE_WINDOW = 10

#: Consecutive short premium outputs that trigger downgrade advice.
STREAK_THRESHOLD = 5
SHORT_OUTPUT_TOKENS = 256

#: Fraction of a declared budget at which the premium trade-off interrupt fires.
BUDGET_ADVICE_THRESHOLD = 0.6

#: Minimum seconds between two notes of the same kind on one thread.
NOTE_COOLDOWN_SECONDS = 300.0


def model_tier(model: str | None, pricing: dict[str, Price] | None = None) -> str:
    """Classify ``model`` as ``premium`` / ``standard`` / ``cheap`` / ``unknown``.

    Derived from the pricing table's input rate so the classification stays in
    one place and follows pricing corrections automatically.
    """
    price = price_for(model, pricing)
    if price is None:
        return "unknown"
    if price.input >= PREMIUM_INPUT_RATE:
        return "premium"
    if price.input <= CHEAP_INPUT_RATE:
        return "cheap"
    return "standard"


@dataclass
class Advice:
    """One recommendation produced by a rule."""

    kind: str  # escalate | downgrade | budget_tradeoff
    severity: str  # note | interrupt
    message: str


@dataclass
class _ThreadSignals:
    """Rolling per-thread session signals the rules read."""

    tool_errors: deque[bool] = field(default_factory=lambda: deque(maxlen=FAILURE_WINDOW))
    premium_short_streak: int = 0
    spent_usd: float = 0.0
    spent_tokens: float = 0.0
    last_note_at: dict[str, float] = field(default_factory=dict)
    interrupted: bool = False

    @property
    def recent_failures(self) -> int:
        return sum(1 for e in self.tool_errors if e)


class AdviceMiddleware(AgentMiddleware):
    """Session-signal collector + rule-based advisor for one agent.

    Args:
        agent: ledger label (matches the economy middleware's attribution).
        max_tokens / max_usd: the agent's declared budget, if any — enables
            the high-stakes budget trade-off rule.
        pricing: pricing-table override (tests).
    """

    def __init__(
        self,
        *,
        agent: str = "orchestrator",
        max_tokens: int | None = None,
        max_usd: float | None = None,
        pricing: dict[str, Price] | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._max_tokens = max_tokens
        self._max_usd = max_usd
        self._pricing = pricing
        self._threads: dict[str, _ThreadSignals] = {}

    @property
    def name(self) -> str:
        return f"Manta.Advice.{self._agent}"

    # --- signal collection ---------------------------------------------------

    def _signals(self, thread: str) -> _ThreadSignals:
        return self._threads.setdefault(thread, _ThreadSignals())

    def _observe_tool_result(self, thread: str, result: Any) -> None:
        self._signals(thread).tool_errors.append(
            getattr(result, "status", None) == "error"
        )

    def _observe_model_response(self, thread: str, model: str, response: Any) -> None:
        signals = self._signals(thread)
        usage = self._usage_metadata(response)
        output_tokens = int(usage.get("output_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        signals.spent_tokens += input_tokens + output_tokens
        price = price_for(model, self._pricing)
        if price is not None:
            signals.spent_usd += (
                input_tokens * price.input + output_tokens * price.output
            ) / 1_000_000
        if model_tier(model, self._pricing) == "premium" and 0 < output_tokens < SHORT_OUTPUT_TOKENS:
            signals.premium_short_streak += 1
        else:
            signals.premium_short_streak = 0

    @staticmethod
    def _usage_metadata(response: Any) -> dict[str, Any]:
        result = getattr(response, "result", None)
        candidates = list(reversed(result)) if isinstance(result, list) else [response]
        for msg in candidates:
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict):
                return usage
        return {}

    # --- rules -----------------------------------------------------------------

    def _evaluate_notes(self, thread: str, model: str) -> Advice | None:
        signals = self._signals(thread)
        tier = model_tier(model, self._pricing)
        if tier != "premium" and signals.recent_failures >= FAILURE_THRESHOLD:
            return Advice(
                kind="escalate",
                severity="note",
                message=(
                    f"this step has hit {signals.recent_failures} tool errors on "
                    f"`{model}` — consider escalating to a stronger reasoner for "
                    "the tricky part (/model), then dropping back."
                ),
            )
        if tier == "premium" and signals.premium_short_streak >= STREAK_THRESHOLD:
            return Advice(
                kind="downgrade",
                severity="note",
                message=(
                    f"the last {signals.premium_short_streak} calls on `{model}` "
                    "were short, routine outputs — a cheaper model would likely do "
                    "this loop for a fraction of the cost (/model)."
                ),
            )
        return None

    def _evaluate_interrupt(self, thread: str, model: str) -> Advice | None:
        if self._max_usd is None and self._max_tokens is None:
            return None
        signals = self._signals(thread)
        if signals.interrupted:
            return None
        if model_tier(model, self._pricing) != "premium":
            return None
        over = False
        if self._max_usd is not None:
            over = signals.spent_usd >= self._max_usd * BUDGET_ADVICE_THRESHOLD
        if not over and self._max_tokens is not None:
            over = signals.spent_tokens >= self._max_tokens * BUDGET_ADVICE_THRESHOLD
        if not over:
            return None
        return Advice(
            kind="budget_tradeoff",
            severity="interrupt",
            message=(
                f"Agent '{self._agent}' has used a large share of its budget "
                f"(${signals.spent_usd:.2f} so far) while running the premium "
                f"model `{model}`. Continue on premium, or switch to a cheaper "
                "model for the rest of this task?"
            ),
        )

    # --- delivery ----------------------------------------------------------------

    def _cooled_down(self, thread: str, kind: str) -> bool:
        signals = self._signals(thread)
        last = signals.last_note_at.get(kind, 0.0)
        if time.time() - last < NOTE_COOLDOWN_SECONDS:
            return False
        signals.last_note_at[kind] = time.time()
        return True

    def _record(self, thread: str, model: str, advice: Advice, delivered: str) -> None:
        try:
            from ..agents.usage import AdviceRecord, record_advice

            record_advice(
                AdviceRecord(
                    agent=self._agent,
                    thread_id=thread,
                    model=model,
                    kind=advice.kind,
                    severity=advice.severity,
                    message=advice.message,
                    delivered=delivered,
                )
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..tasks.store import EventRecord, record_event

            record_event(
                EventRecord(
                    agent=self._agent,
                    kind="advice",
                    detail=f"{advice.kind}: {advice.message[:140]}",
                    task_id=os.environ.get("MANTA_TASK_ID") or None,
                )
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _annotation_target(response: Any) -> Any | None:
        """The message an advisory can be appended to, or ``None``.

        Only a message with string content and no pending tool calls (the
        user-facing end of the turn) is annotatable. Mid-loop tool-calling
        turns return ``None`` — and the caller must then *not* consume the
        cooldown or record delivery, otherwise advice raised during a tool
        loop (exactly when escalation advice fires) would be marked delivered
        without ever reaching the user.
        """
        result = getattr(response, "result", None)
        target = result[-1] if isinstance(result, list) and result else response
        if getattr(target, "tool_calls", None):
            return None
        content = getattr(target, "content", None)
        if not isinstance(content, str) or not content.strip():
            return None
        return target

    @staticmethod
    def _annotate(response: Any, target: Any, advice: Advice) -> Any:
        """Append the advisory note to ``target`` (from :meth:`_annotation_target`)."""
        annotated = target.model_copy(
            update={
                "content": f"{target.content}\n\n> **Manta advice:** {advice.message}"
            }
        )
        result = getattr(response, "result", None)
        if isinstance(result, list) and result:
            response.result[-1] = annotated
            return response
        return annotated

    def _maybe_interrupt(self, request: Any) -> None:
        thread = _thread_id(request)
        model = _model_name(getattr(request, "model", None))
        advice = self._evaluate_interrupt(thread, model)
        if advice is None:
            return
        signals = self._signals(thread)
        try:
            from langgraph.errors import GraphInterrupt
            from langgraph.types import interrupt
        except Exception:  # noqa: BLE001 - no HITL primitive available
            signals.interrupted = True
            logger.info("Manta advice (no approval channel): %s", advice.message)
            return
        try:
            interrupt(
                {
                    "type": "manta_advice",
                    "agent": self._agent,
                    "kind": advice.kind,
                    "message": advice.message,
                }
            )
            # interrupt() only returns on the post-decision re-execution (the
            # first pass raises GraphInterrupt, re-raised below, pausing the
            # run). Record exactly once, here, so the resume doesn't
            # double-write the advice row.
            signals.interrupted = True
            self._record(thread, model, advice, delivered="interrupt")
        except GraphInterrupt:
            raise  # the pause mechanism itself: must propagate
        except Exception:  # noqa: BLE001 - interrupt outside a graph context
            signals.interrupted = True
            logger.info("Manta advice (cannot pause here): %s", advice.message)

    def _after_response(self, request: Any, response: Any) -> Any:
        try:
            thread = _thread_id(request)
            model = _model_name(getattr(request, "model", None))
            self._observe_model_response(thread, model, response)
            advice = self._evaluate_notes(thread, model)
            if advice is None:
                return response
            # Annotatability gates everything: a mid-loop tool-calling turn
            # leaves the advice pending (no cooldown, no ledger row) so it
            # actually lands on the turn's final user-facing answer.
            target = self._annotation_target(response)
            if target is None:
                return response
            if self._cooled_down(thread, advice.kind):
                self._record(thread, model, advice, delivered="note")
                return self._annotate(response, target, advice)
        except Exception:  # noqa: BLE001 - advice must never break a run
            logger.debug("Manta advice evaluation failed", exc_info=True)
        return response

    # --- middleware hooks -----------------------------------------------------

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._maybe_interrupt(request)
        response = handler(request)
        return self._after_response(request, response)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._maybe_interrupt(request)
        response = await handler(request)
        return self._after_response(request, response)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        result = handler(request)
        try:
            self._observe_tool_result(_thread_id(request), result)
        except Exception:  # noqa: BLE001
            pass
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        result = await handler(request)
        try:
            self._observe_tool_result(_thread_id(request), result)
        except Exception:  # noqa: BLE001
            pass
        return result


def _thread_id(request: Any) -> str:
    from .economy import _thread_id as economy_thread_id

    return economy_thread_id(request)


def _model_name(model: Any) -> str:
    from .economy import _model_name as economy_model_name

    return economy_model_name(model)


def _env_enabled() -> bool:
    value = os.getenv(_ENV_TOGGLE)
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def orchestrator_advice_middleware() -> AgentMiddleware | None:
    """Advice middleware for the base orchestrator, or ``None`` if disabled."""
    if not _env_enabled():
        return None
    try:
        return AdviceMiddleware(agent="orchestrator")
    except Exception:  # noqa: BLE001
        return None


def agent_advice_middleware(defn: Any) -> AgentMiddleware | None:
    """Advice middleware for an :class:`AgentDef`, or ``None`` if disabled."""
    if not _env_enabled():
        return None
    try:
        return AdviceMiddleware(
            agent=getattr(defn, "name", "agent"),
            max_tokens=getattr(defn, "budget_max_tokens", None),
            max_usd=getattr(defn, "budget_max_usd", None),
        )
    except Exception:  # noqa: BLE001
        return None
