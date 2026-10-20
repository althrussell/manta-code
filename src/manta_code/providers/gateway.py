"""On-demand gateway metadata inspection for services and legacy endpoints.

Reads control-plane metadata only. A missing or inaccessible policy is unknown,
and never establishes that a target lacks governance or inference permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GatewayEndpointInfo:
    """Visible gateway posture of a Databricks inference target."""

    name: str
    features: tuple[str, ...] = ()
    external_provider: str | None = None
    foundation_model: bool = False
    kind: str = "endpoint"
    supported_api_types: tuple[str, ...] = ()
    metadata_status: str = "success"
    metadata_detail: str = ""
    destinations: tuple[str, ...] = ()
    fallback_destinations: tuple[str, ...] = ()

    @property
    def gateway_governed(self) -> bool:
        """Whether some governance configuration was actually visible."""
        return bool(self.features)

    @property
    def governance(self) -> str:
        return ", ".join(self.features) if self.features else "unknown (no visible policy)"

    @property
    def source(self) -> str:
        if self.kind == "service":
            return "Unity Catalog model service"
        if self.external_provider:
            return f"{self.external_provider} (external, via gateway)"
        if self.foundation_model:
            return "databricks foundation model"
        return "databricks custom model"


@dataclass
class GatewaySurface:
    """Targets plus independent discovery source outcomes."""

    endpoints: list[GatewayEndpointInfo] = field(default_factory=list)
    outcomes: tuple[Any, ...] = ()

    @property
    def governed(self) -> list[GatewayEndpointInfo]:
        return [e for e in self.endpoints if e.gateway_governed]

    @property
    def external(self) -> list[GatewayEndpointInfo]:
        return [e for e in self.endpoints if e.external_provider]

    @property
    def providers(self) -> list[str]:
        names = {"databricks"} if any(
            e.external_provider is None and e.kind == "endpoint" for e in self.endpoints
        ) else set()
        names.update(e.external_provider for e in self.endpoints if e.external_provider)
        return sorted(names)


def _get(value: object, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _features_of(ai_gateway: object) -> tuple[str, ...]:
    if ai_gateway is None:
        return ()
    features: list[str] = []
    usage = _get(ai_gateway, "usage_tracking_config")
    if usage is not None and _get(usage, "enabled", False):
        features.append("usage-tracking")
    if _get(ai_gateway, "rate_limits"):
        features.append("rate-limits")
    if _get(ai_gateway, "guardrails"):
        features.append("guardrails")
    if _get(ai_gateway, "fallback_config"):
        features.append("fallbacks")
    if _get(ai_gateway, "inference_table_config"):
        features.append("inference-tables")
    return tuple(features)


def _inspect_endpoint(detail: object) -> GatewayEndpointInfo:
    """Build a posture record from a full legacy endpoint response."""
    external_provider: str | None = None
    foundation = False
    for entity in _get(_get(detail, "config"), "served_entities", None) or []:
        foundation = foundation or _get(entity, "foundation_model") is not None
        external = _get(entity, "external_model")
        if external is not None:
            provider = _get(external, "provider")
            external_provider = str(_get(provider, "value", provider)) if provider else None
    return GatewayEndpointInfo(
        name=_get(detail, "name", "") or "",
        features=_features_of(_get(detail, "ai_gateway")),
        external_provider=external_provider,
        foundation_model=foundation,
    )


def _destination_labels(destinations: object) -> tuple[str, ...]:
    """Expose routing labels without dumping provider credentials/config."""
    result: list[str] = []
    for destination in destinations or []:
        name = str(_get(destination, "name", "unnamed"))
        percentage = _get(destination, "traffic_percentage")
        label = f"{name} ({percentage}%)" if percentage is not None else name
        if _get(destination, "is_deleted", False):
            label += " [deleted backing resource]"
        result.append(label)
    return tuple(result)


def _inspect_service(detail: object) -> GatewayEndpointInfo:
    """Inspect a FULL model-service response (SDK get has no view argument)."""
    resource = _get(detail, "name", "") or ""
    name = resource.removeprefix("model-services/")
    config = _get(detail, "config")
    routing = _get(config, "routing")
    destinations = _destination_labels(_get(routing, "destinations"))
    fallback = _destination_labels(_get(_get(routing, "fallback"), "destinations"))
    features: list[str] = []
    if _get(config, "rate_limits"):
        features.append("rate-limits")
    if _get(config, "inference_table"):
        features.append("inference-tables")
    if fallback:
        features.append("fallbacks")
    return GatewayEndpointInfo(
        name=name, kind="service", features=tuple(features),
        supported_api_types=tuple(_get(detail, "supported_api_types", None) or []),
        destinations=destinations, fallback_destinations=fallback,
    )


def discover_gateway_surface(
    profile: str | None = None,
    *,
    names: list[str] | None = None,
    limit: int = 100,
    schemas: list[str] | None = None,
    refresh: bool = False,
) -> GatewaySurface:
    """Fetch detailed posture on demand, retaining denied/unknown target rows.

    Explicit names support services outside configured discovery schemas. No
    inference call or direct-provider request is made by this operation.
    """
    import time

    from ..config import load_config
    from .discovery import DiscoveryOutcome, _bounded_client, discover_databricks_targets, lookup_databricks_target
    from .targets import parse_target

    try:
        timeout = load_config().databricks.discovery_timeout_seconds
        client = _bounded_client(profile, None, time.monotonic() + timeout)
    except Exception as exc:  # noqa: BLE001 - optional SDK/auth may be absent
        from .discovery import classify_discovery_error

        return GatewaySurface(outcomes=(DiscoveryOutcome(
            "authentication", classify_discovery_error(exc),
            message="SDK authentication unavailable; inference access remains unverified.",
        ),))

    surface = GatewaySurface()
    if names is None:
        inventory = discover_databricks_targets(
            profile, schemas=schemas, refresh=refresh, client=client,
        )
        names = list(inventory.names)
        surface.outcomes = inventory.outcomes
    for name in names[:max(0, limit)]:
        try:
            target = parse_target(name)
        except ValueError:
            surface.endpoints.append(GatewayEndpointInfo(
                name=name, metadata_status="invalid", metadata_detail="invalid Databricks target",
            ))
            continue
        lookup = lookup_databricks_target(name, profile=profile, client=client)
        if lookup.exists is not True:
            surface.endpoints.append(GatewayEndpointInfo(
                name=target.name, kind=target.kind.value,
                metadata_status=lookup.outcome.status,
                metadata_detail=lookup.outcome.message,
            ))
            continue
        try:
            if target.kind.value == "service":
                info = _inspect_service(lookup.detail)
            else:
                info = _inspect_endpoint(lookup.detail)
            surface.endpoints.append(info)
        except Exception as exc:  # noqa: BLE001 - independent target outcomes
            from .discovery import classify_discovery_error

            surface.endpoints.append(GatewayEndpointInfo(
                name=target.name, kind=target.kind.value, metadata_status=classify_discovery_error(exc),
                metadata_detail="Detailed metadata unavailable; inference access is unverified.",
            ))
    return surface
