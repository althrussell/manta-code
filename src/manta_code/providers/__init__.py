"""Provider abstraction: model refs and a resolver registry (ADR 0010, Phase A).

Manta's vision is multi-provider: "the best model for *this* step, whoever
makes it". Before this package, the ``databricks:`` scheme was a hardcoded
assumption in the subagent resolver, the routing middleware, and the model-pin
middleware. This package makes the provider a lookup, not an assumption:

- :class:`ModelRef` — a parsed ``provider:model`` spec.
- :func:`register_resolver` / :func:`resolver_for` — a registry mapping a
  provider name to a factory that turns a bare model name into a chat-model
  instance. Databricks is registered by default; the AI Gateway provider
  (Phase D) registers here too.
- :func:`resolve_model_ref` — resolve a spec through the registry, optionally
  falling back to langchain's ``init_chat_model`` for providers langchain
  already knows (anthropic, openai, google_genai, …) so a Manta agent can pin
  any provider's model, not just Databricks endpoints.

Everything is guarded in the codebase's house style: resolution failures
return ``None`` rather than raising, so callers degrade to "use the session
model" instead of breaking a call or a launch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("manta.providers")

#: Provider key for Databricks serving endpoints / AI Gateway routes.
DATABRICKS_PROVIDER = "databricks"

#: Resolver factories by provider name. A factory maps a bare model name (the
#: part after ``provider:``) to a chat-model instance, or returns ``None`` when
#: it cannot (missing optional dependency, bad endpoint).
_RESOLVERS: dict[str, Callable[[str], Any | None]] = {}


@dataclass(frozen=True)
class ModelRef:
    """A parsed ``provider:model`` spec (e.g. ``databricks:databricks-gpt-5-5``)."""

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_model_ref(spec: str | None) -> ModelRef | None:
    """Parse ``provider:model`` into a :class:`ModelRef`.

    Returns ``None`` for empty specs and for bare model names with no provider
    prefix (callers decide how to default those). The model part may itself
    contain colons (some gateway routes do), so only the first colon splits.
    """
    if not spec or not isinstance(spec, str):
        return None
    if ":" not in spec:
        return None
    provider, model = spec.split(":", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None
    return ModelRef(provider=provider, model=model)


def register_resolver(
    provider: str, factory: Callable[[str], Any | None]
) -> None:
    """Register (or replace) the resolver factory for ``provider``."""
    _RESOLVERS[provider] = factory


def resolver_for(provider: str) -> Callable[[str], Any | None] | None:
    """Return the registered resolver factory for ``provider``, or ``None``."""
    return _RESOLVERS.get(provider)


def registered_providers() -> tuple[str, ...]:
    """Names of all providers with a registered resolver."""
    return tuple(_RESOLVERS)


def _databricks_factory(model: str) -> Any | None:
    """Build a Databricks chat model from a serving-endpoint name.

    Lazy import keeps this package importable without the ``[agent]`` extra;
    a missing dependency resolves to ``None`` (callers fall back) rather than
    raising.
    """
    try:
        from ..databricks_chat import MantaChatDatabricks
    except Exception:  # noqa: BLE001 - optional extra missing
        return None
    try:
        return MantaChatDatabricks(model=model)
    except Exception:  # noqa: BLE001 - bad endpoint must not break the caller
        logger.debug("databricks resolver failed for %s", model, exc_info=True)
        return None


register_resolver(DATABRICKS_PROVIDER, _databricks_factory)


def _langchain_fallback(spec: str) -> Any | None:
    """Resolve ``spec`` via langchain's ``init_chat_model`` (guarded).

    Covers providers langchain ships first-class support for (anthropic,
    openai, google_genai, …) so per-agent model pins are not limited to
    providers Manta registers explicitly. Returns ``None`` on any failure.
    """
    try:
        from langchain.chat_models import init_chat_model

        return init_chat_model(spec)
    except Exception:  # noqa: BLE001 - fallback is best-effort
        logger.debug("langchain fallback failed for %s", spec, exc_info=True)
        return None


def resolve_model_ref(spec: str, *, fallback: bool = True) -> Any | None:
    """Resolve a ``provider:model`` spec to a chat-model instance.

    Registered providers win. When ``fallback`` is set and the provider is not
    registered, langchain's ``init_chat_model`` is tried with the full spec.
    Returns ``None`` when the spec has no provider prefix or nothing could
    resolve it — never raises.
    """
    ref = parse_model_ref(spec)
    if ref is None:
        return None
    factory = _RESOLVERS.get(ref.provider)
    if factory is not None:
        try:
            return factory(ref.model)
        except Exception:  # noqa: BLE001 - resolver bugs must not break calls
            logger.debug("resolver for %s raised", ref.provider, exc_info=True)
            return None
    if fallback:
        return _langchain_fallback(spec)
    return None
