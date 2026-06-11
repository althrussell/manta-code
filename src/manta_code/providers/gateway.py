"""Databricks AI Gateway surface discovery (ADR 0010, Phase D — pillar 2).

On Databricks, the AI Gateway is not a separate service to call — it is the
governance layer *on serving endpoints*: usage tracking, rate limits,
guardrails, fallbacks, and inference tables attach to each endpoint, and
external-model endpoints broker other vendors (Anthropic, OpenAI, Google, …)
through the same governed surface with unified auth.

Manta already routes every Databricks model call through these endpoints (the
``databricks`` provider), so calls are gateway-governed wherever the endpoint
is. What was missing is *visibility* and *brokerage awareness*: this module
discovers, per chat endpoint, which gateway features are active and which
underlying vendor serves it, powering ``manta gateway`` and doctor checks.

Discovery needs one ``get()`` per endpoint (the list API returns slim
objects), so callers should treat it as an on-demand operation, not a
launch-path one. Best-effort throughout: failures yield empty results.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayEndpointInfo:
    """One chat endpoint's gateway posture."""

    name: str
    #: Active gateway features: usage-tracking, rate-limits, guardrails,
    #: fallbacks, inference-tables.
    features: tuple[str, ...] = ()
    #: Underlying vendor for external-model endpoints (e.g. ``anthropic``);
    #: ``None`` for Databricks-hosted (foundation/custom) models.
    external_provider: str | None = None
    #: Whether this is a Databricks foundation-model (FMAPI) endpoint.
    foundation_model: bool = False

    @property
    def gateway_governed(self) -> bool:
        return bool(self.features)

    @property
    def source(self) -> str:
        if self.external_provider:
            return f"{self.external_provider} (external, via gateway)"
        if self.foundation_model:
            return "databricks foundation model"
        return "databricks custom model"


@dataclass
class GatewaySurface:
    """The workspace's brokered chat-model surface."""

    endpoints: list[GatewayEndpointInfo] = field(default_factory=list)

    @property
    def governed(self) -> list[GatewayEndpointInfo]:
        return [e for e in self.endpoints if e.gateway_governed]

    @property
    def external(self) -> list[GatewayEndpointInfo]:
        return [e for e in self.endpoints if e.external_provider]

    @property
    def providers(self) -> list[str]:
        """Distinct underlying vendors reachable through the gateway surface."""
        names = {"databricks"} if any(
            e.external_provider is None for e in self.endpoints
        ) else set()
        names.update(e.external_provider for e in self.endpoints if e.external_provider)
        return sorted(names)


def _features_of(ai_gateway: object) -> tuple[str, ...]:
    if ai_gateway is None:
        return ()
    features: list[str] = []
    usage = getattr(ai_gateway, "usage_tracking_config", None)
    if usage is not None and getattr(usage, "enabled", False):
        features.append("usage-tracking")
    if getattr(ai_gateway, "rate_limits", None):
        features.append("rate-limits")
    if getattr(ai_gateway, "guardrails", None):
        features.append("guardrails")
    if getattr(ai_gateway, "fallback_config", None):
        features.append("fallbacks")
    if getattr(ai_gateway, "inference_table_config", None):
        features.append("inference-tables")
    return tuple(features)


def _inspect_endpoint(detail: object) -> GatewayEndpointInfo:
    """Build an info record from a full endpoint ``get()`` response."""
    name = getattr(detail, "name", "") or ""
    features = _features_of(getattr(detail, "ai_gateway", None))
    external_provider: str | None = None
    foundation = False
    config = getattr(detail, "config", None)
    for entity in (getattr(config, "served_entities", None) or []) if config else []:
        if getattr(entity, "foundation_model", None) is not None:
            foundation = True
        external = getattr(entity, "external_model", None)
        if external is not None:
            provider = getattr(external, "provider", None)
            external_provider = (
                str(provider.value)
                if hasattr(provider, "value")
                else (str(provider) if provider else None)
            )
    return GatewayEndpointInfo(
        name=name,
        features=features,
        external_provider=external_provider,
        foundation_model=foundation,
    )


def discover_gateway_surface(
    profile: str | None = None,
    *,
    names: list[str] | None = None,
    limit: int = 100,
) -> GatewaySurface:
    """Inspect the workspace's chat endpoints for their gateway posture.

    ``names`` restricts inspection (e.g. just the agents' pinned endpoints for
    doctor); otherwise every chat endpoint is inspected up to ``limit``.
    Best-effort: returns an empty surface on any client/auth failure, and
    skips endpoints whose detail fetch fails.
    """
    from ..auth import list_serving_chat_endpoints, resolve_workspace_client

    try:
        client = resolve_workspace_client(profile)
        targets = names if names is not None else list_serving_chat_endpoints(profile)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return GatewaySurface()

    surface = GatewaySurface()
    for name in targets[:limit]:
        try:
            detail = client.serving_endpoints.get(name)
        except Exception:  # noqa: BLE001 - skip endpoints we cannot read
            continue
        surface.endpoints.append(_inspect_endpoint(detail))
    return surface
