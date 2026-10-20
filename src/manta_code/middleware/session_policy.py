"""Checkpoint the effective Databricks dialect and protect opaque history."""

from __future__ import annotations

import hashlib
import os
from typing import Annotated, Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, PrivateStateAttr
from langchain_core.messages import AIMessage
from langgraph.types import Command


_DIALECT_KEYS = frozenset({
    "api", "surface", "use_responses_api", "use_ai_gateway", "use_ai_gateway_native_api",
})
_CONSTRUCTOR_KEYS = _DIALECT_KEYS | {
    "target_kind", "databricks_profile", "profile", "max_retries", "http_client", "http_async_client",
}
_PROVENANCE = "manta_databricks_policy"


class SessionPolicyState(AgentState):
    _manta_databricks_protocols: Annotated[NotRequired[dict[str, dict[str, str]]], PrivateStateAttr]
    _manta_databricks_last_target: Annotated[NotRequired[str], PrivateStateAttr]


def _context(model: Any) -> dict[str, str]:
    """Read routing identity without constructing a client or authenticating.

    Profiles can be repointed between sessions. Checkpoint the workspace origin
    as a hash as well, then refresh it after inference has resolved lazy auth.
    """
    policy = model.target_policy
    profile = policy.profile or "DEFAULT"
    host = getattr(getattr(getattr(model, "workspace_client", None), "config", None), "host", None)
    if not host:
        host = os.environ.get("DATABRICKS_HOST")
    if not host:
        from ..auth import list_profiles
        host = next((p.host for p in list_profiles() if p.name == profile), None)
    result = {"profile": profile, "kind": str(policy.target.kind)}
    if host:
        result["workspace"] = hashlib.sha256(host.rstrip("/").lower().encode()).hexdigest()
    return result


def _different_context(saved: dict[str, str], current: dict[str, str]) -> bool:
    # Old checkpoints have no routing context. Preserve their normal resume;
    # all newly saved checkpoints record known profile/workspace boundaries.
    return any(key in saved and key in current and saved[key] != current[key] for key in ("kind", "profile", "workspace"))


def has_opaque_history(messages: list[Any]) -> bool:
    """Opaque signatures/reasoning/item references cannot cross model dialects."""
    def opaque(value: Any) -> bool:
        if isinstance(value, dict):
            if any(k in value for k in (
                "encrypted_content", "signature", "thought_signature", "thoughtSignature",
                "provider_tool_call_id", "manta_chat_tool_state", "manta_chat_content_state",
                "manta_responses_tool_state",
            )):
                return True
            if value.get("type") in {"reasoning", "function_call", "item_reference"} and value.get("id"):
                return True
            return any(opaque(v) for v in value.values())
        if isinstance(value, list):
            return any(opaque(v) for v in value)
        return False

    return any(
        opaque(getattr(msg, "content", None)) or opaque(getattr(msg, "additional_kwargs", None))
        or any(opaque({k: v for k, v in call.items() if k != "args"}) for call in (getattr(msg, "tool_calls", None) or []))
        for msg in messages
    )


class SessionPolicyMiddleware(AgentMiddleware):
    """Run after runtime model resolution, for primary and delegated calls."""

    state_schema = SessionPolicyState

    def _prepare(self, request: Any) -> tuple[Any, dict | None]:
        model = request.model
        policy = getattr(model, "target_policy", None)
        provenance = [
            (msg, msg.additional_kwargs[_PROVENANCE]) for msg in request.messages
            if isinstance(msg, AIMessage) and isinstance(msg.additional_kwargs.get(_PROVENANCE), dict)
        ]
        last_policy = provenance[-1][1] if provenance else {}
        if policy is None:
            if (request.state.get("_manta_databricks_last_target") or last_policy) and has_opaque_history(request.messages):
                raise ValueError(
                    "This conversation contains opaque Databricks model state. "
                    "Continue with its original model or start a new thread before switching providers."
                )
            return request, None
        target = policy.target.public_ref
        original_context = _context(model)
        protocols = dict(request.state.get("_manta_databricks_protocols") or {})
        # Forked histories keep messages, while upstream's public input schema
        # can omit private channels. Message provenance preserves the policy
        # boundary and lets a same-model fork restore its effective dialect.
        saved = protocols.get(target) or (last_policy if last_policy.get("target") == target else None)
        context = getattr(getattr(request, "runtime", None), "context", None)
        overrides = getattr(context, "model_params", None)
        if overrides is None and isinstance(context, dict):
            overrides = context.get("model_params")
        settings = dict(getattr(request, "model_settings", None) or {})
        runtime_options = {**settings, **(overrides or {})}
        explicit = bool(_DIALECT_KEYS & runtime_options.keys())
        constructor = {k: v for k, v in runtime_options.items() if k in _CONSTRUCTOR_KEYS}
        if constructor:
            # Upstream forwards /model params as invocation settings. Routing
            # knobs must instead select a constructor before serialization.
            options = {
                **policy.params, "model": model.model_name,
                "target_kind": policy.target.kind,
                "profile": policy.capabilities.profile,
                "request_tags": policy.request_tags,
                "databricks_profile": policy.profile,
                "workspace_client": model.workspace_client,
            }
            if not ({"api", "use_responses_api"} & constructor.keys()):
                options["api"] = policy.api
            if not ({"surface", "use_ai_gateway", "use_ai_gateway_native_api"} & constructor.keys()):
                options["surface"] = policy.surface
            if (
                "databricks_profile" in constructor
                and constructor["databricks_profile"] != policy.profile
            ):
                # A client already authenticated with the old profile must not
                # override the new profile's lazy SDK client selection.
                options["workspace_client"] = None
            model = type(model)(**{**options, **constructor})
            policy = model.target_policy
            settings = {k: v for k, v in settings.items() if k not in _CONSTRUCTOR_KEYS}
            request = request.override(model=model, model_settings=settings)
        current_context = _context(model)
        context_changed = _different_context(original_context, current_context) or bool(saved and _different_context(saved, current_context))
        if saved and not context_changed and not explicit and (policy.api, policy.surface) != (saved["api"], saved["surface"]):
            model = model.clone_with_policy(api=saved["api"], surface=saved["surface"])
            policy = model.target_policy
            request = request.override(model=model)
        previous = request.state.get("_manta_databricks_last_target") or last_policy.get("target")
        incompatible_history = any(
            has_opaque_history([msg]) and (
                prior.get("target") != target
                or (prior.get("api"), prior.get("surface")) != (policy.api, policy.surface)
                or _different_context(prior, current_context)
            )
            for msg, prior in provenance
        )
        changed = (
            previous and previous != target
            or saved and (saved["api"], saved["surface"]) != (policy.api, policy.surface)
            or context_changed
            or incompatible_history
        )
        if changed and has_opaque_history(request.messages):
            raise ValueError(
                "This model switch would send opaque reasoning or tool state to an incompatible "
                "model/API or Databricks profile/workspace. Continue with the original model/API "
                "and profile/workspace or use /clear to start a new thread."
            )
        protocols[target] = {"api": policy.api, "surface": policy.surface, **current_context}
        return request, {
            "_manta_databricks_protocols": protocols,
            "_manta_databricks_last_target": target,
        }

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        request, update = self._prepare(request)
        response = handler(request)
        self._resolved_context(request, update)
        self._record_provenance(response, update)
        return ExtendedModelResponse(model_response=response, command=Command(update=update)) if update else response

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        request, update = self._prepare(request)
        response = await handler(request)
        self._resolved_context(request, update)
        self._record_provenance(response, update)
        return ExtendedModelResponse(model_response=response, command=Command(update=update)) if update else response

    @staticmethod
    def _resolved_context(request: Any, update: dict | None) -> None:
        if update:
            target = update["_manta_databricks_last_target"]
            update["_manta_databricks_protocols"][target].update(_context(request.model))

    @staticmethod
    def _record_provenance(response: Any, update: dict | None) -> None:
        if update:
            target = update["_manta_databricks_last_target"]
            policy = {"target": target, **update["_manta_databricks_protocols"][target]}
            for message in ([response] if isinstance(response, AIMessage) else getattr(response, "result", [])):
                if isinstance(message, AIMessage):
                    message.additional_kwargs = {**message.additional_kwargs, _PROVENANCE: policy}
