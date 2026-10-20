"""Exact Databricks target identities and the shared, nonsecret model policy.

Policy precedence is constructor options, target options, Manta defaults,
upstream provider defaults, then automatic transport selection. Discovery is
never required for construction or permission to execute a configured target.
"""
from __future__ import annotations

import json
import os
import re
import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

POLICY_ENV = "MANTA_DATABRICKS_POLICY"
_TAG_HEADER = "Databricks-Ai-Gateway-Request-Tags"


class TargetKind(StrEnum):
    ENDPOINT = "endpoint"
    SERVICE = "service"


class TargetResolutionError(ValueError):
    """An explicit model selection cannot be honored safely."""


@dataclass(frozen=True)
class DatabricksTarget:
    name: str
    kind: TargetKind

    @property
    def public_ref(self) -> str:
        return f"databricks:{self.name}"

    @property
    def resource_name(self) -> str | None:
        return f"model-services/{self.name}" if self.kind == TargetKind.SERVICE else None


def parse_target(name: str | DatabricksTarget, kind: str | TargetKind | None = None) -> DatabricksTarget:
    """Accept a bare target, public reference or explicit model-service resource.

    Dotted legacy names can use ``kind='endpoint'``. Other management resource
    namespaces (registered models/provider services) are deliberately rejected.
    """
    if isinstance(name, DatabricksTarget):
        return name if kind is None else parse_target(name.name, kind)
    if not isinstance(name, str) or not name or name != name.strip():
        raise TargetResolutionError("Databricks target must be a nonempty exact name without surrounding whitespace.")
    if name.startswith("databricks:"):
        name = name.removeprefix("databricks:")
    resource = name.startswith("model-services/")
    if resource:
        name = name.removeprefix("model-services/")
    try:
        resolved = TargetKind(kind) if kind else (TargetKind.SERVICE if resource or name.count(".") == 2 else TargetKind.ENDPOINT)
    except ValueError as exc:
        raise TargetResolutionError("Target kind must be 'endpoint' or 'service'.") from exc
    if resource and resolved != TargetKind.SERVICE:
        raise TargetResolutionError("A model-services resource cannot have endpoint kind.")
    if not name or re.search(r"[\s/:`\"'\x00-\x1f]", name):
        raise TargetResolutionError(f"Invalid Databricks target {name!r}; supply an endpoint or exact catalog.schema.service.")
    if resolved == TargetKind.SERVICE:
        parts = name.split(".")
        if len(parts) != 3 or any(not p or len(p) > 255 or not re.fullmatch(r"[\w-]+", p) for p in parts):
            raise TargetResolutionError("A model service needs exactly three catalog.schema.service components (letters, digits, underscores or hyphens; each at most 255 characters).")
    elif len(name) > 255:
        raise TargetResolutionError("A serving endpoint name must be at most 255 characters.")
    return DatabricksTarget(name, resolved)


@dataclass(frozen=True)
class CapabilityEvidence:
    """Unknown stays absent; protocol support alone establishes no tool limits."""
    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    supported_api_types: tuple[str, ...] = ()

    @property
    def profile(self) -> dict[str, Any]:
        return {k: v for k, v in self.values.items() if v is not None and k not in {"forced_tool_choice", "preferred_api", "stream_usage"}}


# Exact, documented compatibility evidence, never a family-name heuristic.
# These latest GPT services require Responses (supported-models documentation).
_RESPONSES_ONLY = frozenset({"system.ai.gpt-6-1-sol"})


def curated_capabilities(target: DatabricksTarget) -> CapabilityEvidence:
    if target.name in {"system.ai.gemini-3-7-flash", "system.ai.gemini-4-argon"} and target.kind == TargetKind.SERVICE:
        source = "Manta labs Unity qualification, 2026-10-20"
        return CapabilityEvidence(
            values={"tool_calling": True, "preferred_api": "chat", "stream_usage": False},
            sources={
                "tool_calling": source + ": Chat two-request tool roundtrip passed",
                "preferred_api": source + ": Chat replays thoughtSignature; Responses rejects raw full-output replay with missing thought_signature",
                "stream_usage": source + ": Chat stream_options rejected by Gateway generation_config",
            },
        )
    if target.name == "system.ai.claude-sonnet-5-5" and target.kind == TargetKind.SERVICE:
        source = "Manta labs Unity Responses qualification, 2026-10-20"
        return CapabilityEvidence(values={"tool_calling": True, "tool_choice": False, "forced_tool_choice": False}, sources={"tool_calling": source + ": ordinary tool roundtrip passed", "tool_choice": source + ": forced tool choice rejected by service", "forced_tool_choice": source + ": forced tool choice rejected by service"})
    if target.name in _RESPONSES_ONLY and target.kind == TargetKind.SERVICE:
        return CapabilityEvidence(supported_api_types=("mlflow/v1/responses",), sources={"supported_api_types": "Databricks supported-models documentation, 2026-10-20"})
    return CapabilityEvidence()


def validate_request_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in value.items()):
        raise TargetResolutionError("Gateway request_tags must be a map of nonempty string keys to string values.")
    if any("\n" in k or "\r" in k or "\n" in v or "\r" in v for k, v in value.items()):
        raise TargetResolutionError("Gateway request tags cannot contain line breaks.")
    if len(json.dumps(value, ensure_ascii=True).encode()) > 8192:
        raise TargetResolutionError("Gateway request tags must fit in an 8 KiB header.")
    return dict(value)


def merge_request_tag_headers(headers: dict[str, Any] | None, tags: dict[str, str]) -> dict[str, Any]:
    result = dict(headers or {})
    existing: dict[str, str] = {}
    for key in list(result):
        if key.lower() == _TAG_HEADER.lower():
            try:
                existing.update(validate_request_tags(json.loads(result.pop(key))))
            except (ValueError, TypeError) as exc:
                raise TargetResolutionError(f"{_TAG_HEADER} must contain a JSON string-to-string map.") from exc
    # Explicit supplied headers win over configured defaults.
    merged = validate_request_tags({**tags, **existing})
    if merged:
        result[_TAG_HEADER] = json.dumps(merged, separators=(",", ":"), ensure_ascii=True)
    return result


def _nonsecret(value: Any) -> Any:
    if isinstance(value, dict):
        forbidden = {"api_key", "openai_api_key", "token", "access_token", "refresh_token", "oauth_token", "auth", "password", "authorization", "client_secret", "client_assertion", "credentials", "credential", "token_provider", "default_headers", "headers", "extra_headers", "workspace_client", "http_client", "http_async_client", "client", "async_client", "root_client", "root_async_client"}
        result = {k: _nonsecret(v) for k, v in value.items() if str(k).lower().replace("-", "_") not in forbidden and not str(k).lower().replace("-", "_").endswith(("_api_key", "_secret", "_password", "_access_token", "_refresh_token"))}
        # Preserve user gateway tags without exporting arbitrary/auth headers.
        for field_name in ("default_headers", "headers", "extra_headers"):
            if isinstance(value.get(field_name), dict):
                tag_headers = {k: v for k, v in value[field_name].items() if k.lower() == _TAG_HEADER.lower()}
                if tag_headers:
                    result[field_name] = merge_request_tag_headers(tag_headers, {})
        return result
    if isinstance(value, (list, tuple)):
        return [_nonsecret(v) for v in value]
    return value


def export_target_policy(config: Any, provider_params: dict[str, Any] | None = None, inventory: Any = None) -> str:
    """Serialize policy for child processes, excluding credentials and clients."""
    settings = getattr(config, "databricks", config)
    settings = settings.model_dump() if hasattr(settings, "model_dump") else dict(settings or {})
    capabilities = {}
    if inventory is not None:
        for item in inventory.targets:
            capabilities[f"{item.target.kind}:{item.name}"] = asdict(item.capabilities)
    context = {}
    if inventory is not None:
        context = {"profile": inventory.profile, "workspace": hashlib.sha256((inventory.workspace or "").encode()).hexdigest()}
    payload = {"settings": settings, "provider_params": provider_params or {}, "capabilities": capabilities, "capability_context": context}
    return json.dumps(_nonsecret(payload), separators=(",", ":"))


def _policy_inputs(config: Any = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if config is None and os.environ.get(POLICY_ENV):
        try:
            payload = json.loads(os.environ[POLICY_ENV])
            capabilities = dict(payload.get("capabilities", {}))
            capabilities["__context__"] = payload.get("capability_context", {})
            return payload.get("settings", {}), payload.get("provider_params", {}), capabilities
        except (ValueError, AttributeError) as exc:
            raise TargetResolutionError(f"Invalid {POLICY_ENV} policy JSON.") from exc
    if config is None:
        from ..config import load_config, load_toml
        from pathlib import Path
        config = load_config()
        upstream = load_toml(Path(os.environ.get("DEEPAGENTS_HOME", Path.home() / ".deepagents")) / "config.toml")
        provider = upstream.get("models", {}).get("providers", {}).get("databricks", {}).get("params", {})
    else:
        provider = {}
    settings = getattr(config, "databricks", config)
    return settings.model_dump() if hasattr(settings, "model_dump") else dict(settings or {}), provider, {}


@dataclass(frozen=True)
class TargetPolicy:
    target: DatabricksTarget
    surface: str
    api: str
    params: dict[str, Any]
    capabilities: CapabilityEvidence
    request_tags: dict[str, str]
    profile: str | None = None

    def model_kwargs(self) -> dict[str, Any]:
        result = dict(self.params)
        result.update(model=self.target.name, use_ai_gateway=self.surface == "unified", use_ai_gateway_native_api=self.surface == "openai", use_responses_api=self.api == "responses")
        result["default_headers"] = merge_request_tag_headers(result.get("default_headers"), self.request_tags)
        # Do not allow ChatOpenAI to infer OpenAI context limits from a service
        # name: only explicit or verified values enter model.profile.
        result["profile"] = self.capabilities.profile
        return result


def _merge_policy_layers(*layers: dict[str, Any]) -> dict[str, Any]:
    """Apply precedence across semantic aliases, reject same-layer conflicts."""
    result: dict[str, Any] = {}
    groups = ({"api", "use_responses_api"}, {"surface", "use_ai_gateway", "use_ai_gateway_native_api"}, {"max_tokens", "max_completion_tokens", "max_output_tokens"})
    for layer in layers:
        layer = dict(layer)
        if layer.get("api", "auto") != "auto" and layer.get("use_responses_api") is not None and (layer["api"] == "responses") != layer["use_responses_api"]:
            raise TargetResolutionError("Contradictory Databricks api and use_responses_api settings in the same policy layer.")
        if layer.get("use_ai_gateway") and layer.get("use_ai_gateway_native_api"):
            raise TargetResolutionError("Choose either unified or native OpenAI Gateway routing, not both.")
        flag = "openai" if layer.get("use_ai_gateway_native_api") else "unified" if layer.get("use_ai_gateway") else "legacy" if layer.get("use_ai_gateway") is False else None
        if layer.get("surface") and flag and layer["surface"] != flag:
            raise TargetResolutionError("Contradictory Databricks surface and gateway flags in the same policy layer.")
        limits = [layer[k] for k in groups[2] if k in layer and layer[k] is not None]
        if len(limits) > 1 and any(v != limits[0] for v in limits):
            raise TargetResolutionError("Conflicting max_tokens and max_output_tokens limits in the same policy layer.")
        for group in groups:
            if group.intersection(layer):
                for key in group:
                    result.pop(key, None)
        if "request_tags" in layer:
            layer["request_tags"] = {**result.get("request_tags", {}), **validate_request_tags(layer["request_tags"])}
        result.update(layer)
    return result


def resolve_target_policy(name: str, *, explicit: dict[str, Any] | None = None, config: Any = None, capability: CapabilityEvidence | None = None, constructor_defaults: dict[str, Any] | None = None) -> TargetPolicy:
    settings, provider, inventory = _policy_inputs(config)
    raw_explicit = dict(explicit or {})
    raw_target = dict(settings.get("targets", {}).get(name.removeprefix("databricks:").removeprefix("model-services/"), {}))
    target = parse_target(name, raw_explicit.pop("target_kind", raw_target.pop("kind", None)))
    # Match upstream provider-table semantics: flat scalar defaults, then an
    # exact per-model subtable. Dict-valued model options belong in that table.
    provider_params = {k: v for k, v in provider.items() if not isinstance(v, dict)}
    provider_params.update(provider.get(target.name, {}) if isinstance(provider.get(target.name), dict) else {})
    global_params = dict(settings.get("params", {}))
    if target.kind == TargetKind.SERVICE and settings.get("service_api", "auto") != "auto":
        global_params["api"] = settings["service_api"]
    params = _merge_policy_layers(provider_params, constructor_defaults or {}, global_params, raw_target.pop("params", {}), raw_target, raw_explicit)
    params.pop("model", None)
    params.pop("model_name", None)
    params.pop("endpoint", None)
    profile = params.pop("databricks_profile", None)
    from ..auth import resolve_profile
    profile = resolve_profile(profile)
    api = params.pop("api", "auto")
    surface = params.pop("surface", None)
    explicit_surface = surface is not None
    if api not in {"auto", "chat", "responses"}:
        raise TargetResolutionError("Databricks api must be auto, chat or responses.")
    if surface not in {None, "legacy", "unified", "openai"}:
        raise TargetResolutionError("Databricks surface must be legacy, unified or openai.")
    unified = params.pop("use_ai_gateway", None)
    native = params.pop("use_ai_gateway_native_api", None)
    response_flag = params.pop("use_responses_api", None)
    if unified and native:
        raise TargetResolutionError("Choose either unified or native OpenAI Gateway routing, not both.")
    flag_surface = "openai" if native else "unified" if unified else "legacy" if unified is False else None
    explicit_surface = explicit_surface or flag_surface is not None
    if surface and flag_surface and surface != flag_surface:
        raise TargetResolutionError("Contradictory Databricks surface and gateway flags.")
    surface = surface or flag_surface or ("unified" if target.kind == TargetKind.SERVICE else "legacy")
    if target.kind == TargetKind.SERVICE and surface == "legacy":
        raise TargetResolutionError(f"Model service {target.name!r} requires a Unity Gateway surface. Use kind='endpoint' only for an actual dotted legacy endpoint.")
    if target.kind == TargetKind.ENDPOINT and surface != "legacy":
        raise TargetResolutionError("Unity Gateway requires a model-service FQN; legacy endpoints use the serving surface.")
    if response_flag is not None:
        flag_api = "responses" if response_flag else "chat"
        if api != "auto" and api != flag_api:
            raise TargetResolutionError("Contradictory Databricks api and use_responses_api settings.")
        api = flag_api
    capability_key = f"{target.kind}:{target.name}" if f"{target.kind}:{target.name}" in inventory else target.name
    context = inventory.get("__context__", {})
    same_profile = (context.get("profile") or "DEFAULT") == (profile or "DEFAULT")
    explicit_workspace = getattr(getattr(params.get("workspace_client"), "config", None), "host", None)
    same_workspace = not explicit_workspace or not context.get("workspace") or context["workspace"] == hashlib.sha256(explicit_workspace.encode()).hexdigest()
    if capability is None and capability_key in inventory and same_profile and same_workspace:
        item = inventory[capability_key]
        capability = CapabilityEvidence(values=item.get("values", {}), sources=item.get("sources", {}), supported_api_types=tuple(item.get("supported_api_types", ())))
    curated = curated_capabilities(target)
    capability = (
        CapabilityEvidence(values={**curated.values, **capability.values}, sources={**curated.sources, **capability.sources}, supported_api_types=capability.supported_api_types or curated.supported_api_types)
        if capability is not None else curated
    )
    apis = capability.supported_api_types
    unified_responses = any(a.removeprefix("/") in {"mlflow/v1/responses", "responses"} for a in apis)
    unified_chat = any(a.removeprefix("/") in {"mlflow/v1/chat/completions", "chat/completions"} for a in apis)
    native_responses = any(a.removeprefix("/") == "openai/v1/responses" for a in apis)
    native_chat = any(a.removeprefix("/") == "openai/v1/chat/completions" for a in apis)
    if not explicit_surface and target.kind == TargetKind.SERVICE and apis and not (unified_chat or unified_responses) and (native_responses or native_chat):
        surface = "openai"
    if api == "auto":
        preferred_chat = capability.values.get("preferred_api") == "chat" and surface == "unified" and (unified_chat or not apis)
        api = "chat" if preferred_chat else "responses" if (surface == "openai" and (native_responses or not apis)) or (surface == "unified" and (unified_responses or not apis)) else "chat"
    if apis and surface == "unified" and not (unified_responses if api == "responses" else unified_chat):
        raise TargetResolutionError(f"{target.name} does not advertise unified {api}; supported APIs: {', '.join(apis)}. Choose a supported api/surface explicitly.")
    if apis and surface == "openai" and not (native_responses if api == "responses" else native_chat):
        raise TargetResolutionError(f"{target.name} does not advertise native OpenAI {api} support; supported APIs: {', '.join(apis)}.")
    explicit_profile = params.pop("profile", None)
    if explicit_profile is not None:
        if not isinstance(explicit_profile, dict):
            raise TargetResolutionError("Model profile must be a capability map; use databricks_profile for SDK authentication.")
        if "tool_choice" in explicit_profile and "forced_tool_choice" not in explicit_profile:
            explicit_profile = {**explicit_profile, "forced_tool_choice": explicit_profile["tool_choice"]}
        capability = CapabilityEvidence(values={**capability.values, **explicit_profile}, sources={**capability.sources, **{k: "explicit override" for k in explicit_profile}}, supported_api_types=apis)
    tags = validate_request_tags({**settings.get("request_tags", {}), **params.pop("request_tags", {})})
    # max_output_tokens is the public policy option, ChatOpenAI owns dialect
    # conversion and requires its max_tokens constructor spelling.
    if "max_output_tokens" in params:
        if "max_tokens" in params and params["max_tokens"] != params["max_output_tokens"]:
            raise TargetResolutionError("Conflicting max_tokens and max_output_tokens limits.")
        params["max_tokens"] = params.pop("max_output_tokens")
    return TargetPolicy(target, surface, api, params, capability, tags, profile)
