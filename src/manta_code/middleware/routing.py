"""Cost-aware model routing (ADR 0008, Phase 4).

Manta's primary routing lever is the per-agent model pin, centralized in
:mod:`manta_code.model_defaults`. This module adds an optional dynamic lever:
a middleware that selects the planning model for a planning, architecture, or
debugging step, then returns to the role model. Relative prices require known
rates and are not inferred from role names.

It is deliberately conservative and inert unless wired with a model resolver: the
heuristic is a pure, unit-tested function, and if escalation can't resolve a
model it leaves the request untouched. Nothing here can break a launch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from ..model_defaults import ROLE_TARGETS

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


def _explicit_runtime_model(request: Any) -> bool:
    """A model chosen through /model takes precedence over role defaults."""
    context = getattr(getattr(request, "runtime", None), "context", None)
    return bool(context.get("model") if isinstance(context, dict) else getattr(context, "model", None))


class ModelRoutingMiddleware(AgentMiddleware):
    """Select the planning model for hard steps, otherwise retain the role model.

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
        premium_endpoint: str = ROLE_TARGETS["planning"],
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
        if _explicit_runtime_model(request):
            return request
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
    mechanism :class:`ModelRoutingMiddleware` uses), resolving the target once
    and caching it. An invalid explicit pin fails clearly before inference.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        resolve_model: Callable[[str], Any],
        agent: str | None = None,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._resolve_model = resolve_model
        self._model: Any = None
        self._agent = agent

    @property
    def name(self) -> str:
        return "Manta.ModelPin"

    def _pin(self, request: Any) -> Any:
        if _explicit_runtime_model(request):
            return request
        if not self._endpoint:
            return request
        try:
            if self._model is None:
                self._model = self._resolve_model(self._endpoint)
                if self._agent:
                    from ..runtime import model_for_agent
                    self._model = model_for_agent(self._model, self._agent)
            if self._model is None:
                raise ValueError("the model resolver returned no model")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot resolve pinned model {self._endpoint!r}: {exc}. "
                "Correct the agent's model pin or select an available target."
            ) from exc
        return request.override(model=self._model)

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
    from ..runtime import current_launch_policy
    launch = current_launch_policy()
    if launch and launch.get("enforce_model_policy", True):
        from deepagents_code.agent import _has_resolvable_model_provider, _resolve_retry_owned_model
        from deepagents_code.model_config import ModelConfig
        spec = model if ":" in model else f"databricks:{model}"
        ModelConfig.load().require_model_allowed(spec)
        if _has_resolvable_model_provider(spec):
            # Capture the explicit CLI flag during construction: invocation
            # runs after the launch ContextVar has been reset. dcode disables
            # provider SDK retries and tags its single middleware retry owner.
            retries = launch.get("cli_max_retries")
            return ModelPinMiddleware(
                endpoint=spec,
                resolve_model=lambda target: _resolve_retry_owned_model(target, retries),
                agent=getattr(defn, "name", None),
            )
    try:
        from .. import providers
    except Exception:  # noqa: BLE001
        return None

    ref = providers.parse_model_ref(model)
    if ref is not None:
        resolver = providers.resolver_for(ref.provider)
        if resolver is not None:
            return ModelPinMiddleware(endpoint=ref.model, resolve_model=resolver, agent=getattr(defn, "name", None))
        # Unregistered provider: let langchain resolve the full spec lazily.
        return ModelPinMiddleware(
            endpoint=model,
            resolve_model=lambda spec: providers.resolve_model_ref(spec),
            agent=getattr(defn, "name", None),
        )
    # Bare endpoint name with no provider prefix: treat as Databricks (the
    # historical default) so existing agent definitions keep working.
    resolver = databricks_model_resolver()
    if resolver is None:
        return None
    return ModelPinMiddleware(endpoint=_strip_provider(model), resolve_model=resolver, agent=getattr(defn, "name", None))


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
    premium_endpoint: str = ROLE_TARGETS["planning"],
) -> list[AgentMiddleware]:
    """Cost-aware routing middleware for the orchestrator (empty if unavailable)."""
    resolver = databricks_model_resolver()
    if resolver is None:
        return []
    return [ModelRoutingMiddleware(resolve_model=resolver, premium_endpoint=premium_endpoint)]
