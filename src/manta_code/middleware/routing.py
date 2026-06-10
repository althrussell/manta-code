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


class ModelPinMiddleware(AgentMiddleware):
    """Force every model call onto a fixed endpoint (a profile's ``model`` pin).

    Used when a Manta agent with a pinned model is the **primary** (top-level)
    agent: deepagents keeps the session's launch model for the primary loop, so
    without this the planner/review profile would run on the orchestrator's model.
    This overrides the model per call (the same ``request.override(model=...)``
    mechanism :class:`ModelRoutingMiddleware` uses), resolving the endpoint once
    and caching it. Fully guarded — if the model can't be resolved it leaves the
    request untouched, so a bad pin degrades to "use the session model" rather
    than breaking the call.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        resolve_model: Callable[[str], Any],
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._resolve_model = resolve_model
        self._model: Any = None

    @property
    def name(self) -> str:
        return "Manta.ModelPin"

    def _pin(self, request: Any) -> Any:
        if not self._endpoint:
            return request
        try:
            if self._model is None:
                self._model = self._resolve_model(self._endpoint)
            if self._model is None:
                return request
            return request.override(model=self._model)
        except Exception:  # noqa: BLE001 - pin must never break a call
            return request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._pin(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._pin(request))


def _strip_provider(model: str) -> str:
    """Return the bare endpoint name from a ``provider:endpoint`` model spec."""
    return model.split(":", 1)[1] if ":" in model else model


def agent_model_pin_middleware(defn: Any) -> AgentMiddleware | None:
    """Model-pin middleware for a profile's ``model``, or ``None`` if unset.

    The pin resolves through the provider registry (:mod:`manta_code.providers`),
    so an agent can pin any registered provider's model — ``databricks:<endpoint>``
    today, gateway routes in Phase D — or any provider langchain resolves natively
    (``anthropic:…``, ``openai:…``) via the registry's fallback. Returns ``None``
    when the agent has no model pin or no resolver is available, so it is always
    safe to call and append.
    """
    model = getattr(defn, "model", None)
    if not model:
        return None
    try:
        from .. import providers
    except Exception:  # noqa: BLE001
        return None

    ref = providers.parse_model_ref(model)
    if ref is not None:
        resolver = providers.resolver_for(ref.provider)
        if resolver is not None:
            return ModelPinMiddleware(endpoint=ref.model, resolve_model=resolver)
        # Unregistered provider: let langchain resolve the full spec lazily.
        return ModelPinMiddleware(
            endpoint=model,
            resolve_model=lambda spec: providers.resolve_model_ref(spec),
        )
    # Bare endpoint name with no provider prefix: treat as Databricks (the
    # historical default) so existing agent definitions keep working.
    resolver = databricks_model_resolver()
    if resolver is None:
        return None
    return ModelPinMiddleware(endpoint=_strip_provider(model), resolve_model=resolver)


def databricks_model_resolver() -> Callable[[str], Any] | None:
    """Return a resolver building a Databricks chat model from an endpoint name.

    Backed by the provider registry; returns ``None`` if the Databricks
    resolver is unregistered/unavailable, so routing stays a safe no-op
    instead of breaking model calls.
    """
    try:
        from .. import providers
    except Exception:  # noqa: BLE001
        return None
    return providers.resolver_for(providers.DATABRICKS_PROVIDER)


def default_routing_middleware(
    premium_endpoint: str = "databricks-claude-opus-4-8",
) -> list[AgentMiddleware]:
    """Cost-aware routing middleware for the orchestrator (empty if unavailable)."""
    resolver = databricks_model_resolver()
    if resolver is None:
        return []
    return [ModelRoutingMiddleware(resolve_model=resolver, premium_endpoint=premium_endpoint)]
