"""Cost-aware model routing (ADR 0008, Phase 4).

"Cheap-by-default, premium-when-it-matters." Manta's primary routing lever is the
per-agent model pin (the planning/review agents pin a premium model, the SWE/
orchestrator default to a cheap one — see :mod:`manta_code.agents.defaults`). This
module adds an *optional, dynamic* second lever: a middleware that escalates a
single model call to a premium endpoint when the upcoming step looks genuinely
hard (a planning/architecture/debugging ask), then drops back to cheap.

It is deliberately conservative and inert unless wired with a model resolver: the
heuristic is a pure, unit-tested function, and if escalation can't resolve a
model it leaves the request untouched. Nothing here can break a launch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger("manta.routing")

#: Words that signal a step worth spending a premium model on.
ESCALATION_HINTS: frozenset[str] = frozenset(
    {
        "plan",
        "design",
        "architect",
        "architecture",
        "refactor",
        "debug",
        "root cause",
        "root-cause",
        "trade-off",
        "tradeoff",
        "strategy",
        "complex",
        "migrate",
        "migration",
    }
)


def should_escalate(text: str, *, hints: frozenset[str] | None = None) -> bool:
    """Return ``True`` if ``text`` describes a step worth a premium model.

    Pure and case-insensitive so the routing policy can be tested without a model
    or runtime. Matches whole-word-ish on the hint set.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in (hints or ESCALATION_HINTS))


def _latest_human_text(request: Any) -> str:
    messages = list(getattr(request, "messages", None) or [])
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "human" or msg.__class__.__name__ == "HumanMessage":
            content = getattr(msg, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


class ModelRoutingMiddleware(AgentMiddleware):
    """Escalate to a premium model for hard steps, otherwise stay cheap.

    Args:
        resolve_model: callable mapping an endpoint name to a model object the
            runtime can use (e.g. Manta's Databricks chat factory). If ``None``,
            the middleware never escalates (pure no-op) so it is safe to install
            unconditionally.
        premium_endpoint: the endpoint to escalate to.
        hints: override the escalation keyword set.
    """

    def __init__(
        self,
        *,
        resolve_model: Callable[[str], Any] | None = None,
        premium_endpoint: str = "databricks-claude-opus-4-8",
        hints: frozenset[str] | None = None,
    ) -> None:
        super().__init__()
        self._resolve_model = resolve_model
        self._premium_endpoint = premium_endpoint
        self._hints = hints or ESCALATION_HINTS

    @property
    def name(self) -> str:
        return "Manta.Routing"

    def _route(self, request: Any) -> Any:
        if self._resolve_model is None:
            return request
        if not should_escalate(_latest_human_text(request), hints=self._hints):
            return request
        try:
            model = self._resolve_model(self._premium_endpoint)
            if model is None:
                return request
            logger.info("Manta routing: escalating step to %s", self._premium_endpoint)
            return request.override(model=model)
        except Exception:  # noqa: BLE001 - routing must never break a call
            return request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._route(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._route(request))


def databricks_model_resolver() -> Callable[[str], Any] | None:
    """Return a resolver building a Databricks chat model from an endpoint name.

    Guarded: returns ``None`` if the Databricks chat class can't be imported, so
    routing stays a safe no-op instead of breaking model calls.
    """
    try:
        from ..databricks_chat import MantaChatDatabricks
    except Exception:  # noqa: BLE001
        return None

    def _resolve(endpoint: str) -> Any:
        return MantaChatDatabricks(model=endpoint)

    return _resolve


def default_routing_middleware(
    premium_endpoint: str = "databricks-claude-opus-4-8",
) -> list[AgentMiddleware]:
    """Cost-aware routing middleware for the orchestrator (empty if unavailable)."""
    resolver = databricks_model_resolver()
    if resolver is None:
        return []
    return [ModelRoutingMiddleware(resolve_model=resolver, premium_endpoint=premium_endpoint)]
