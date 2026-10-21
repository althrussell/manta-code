"""Exact Gateway capability evidence shared by inference and runtime controls.

The registry contains reviewed facts, not an allow-list: new services remain
discoverable and unknown capabilities remain absent. Provider documentation is
used for context/output limits only where Databricks explicitly guarantees the
same limits. Wire-specific options require Gateway documentation or qualification.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

CHECKED_AT = "2026-10-20"
SUPPORTED_MODELS = "https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models"
REASONING_MODELS = "https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models"
OPENAI_MODELS = "https://developers.openai.com/api/docs/models"
CLAUDE_CONTEXT = "https://platform.claude.com/docs/en/build-with-claude/context-windows"
GEMINI_ARGON = "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-4-argon"
DOCUMENTED_TARGETS = frozenset({
    "system.ai.gpt-6-1-sol", "system.ai.gpt-6-sol", "system.ai.gpt-6-luna", "system.ai.gpt-6-astra",
    "system.ai.claude-opus-5-5", "system.ai.claude-sonnet-5-5", "system.ai.gemini-4-argon",
    "system.ai.databricks-gemini-4-argon", "system.ai.gemini-3-7-flash", "system.ai.gemini-3-8-flash",
    "system.ai.glm-5-3", "system.ai.glm-5-3-flash", "system.ai.glm-5-2", "system.ai.kimi-k3",
    "system.ai.deepseek-v4-1-flash", "system.ai.deepseek-v4-pro-0813", "system.ai.deepseek-v4-flash-0731",
})


def documented_capabilities(target: Any) -> Any:
    """Return reviewed facts for an exact platform service, with field provenance."""
    from .providers.targets import CapabilityEvidence

    if target.kind != "service":
        return CapabilityEvidence()
    name = target.name
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    scoped: dict[str, dict[str, Any]] = {}
    scoped_sources: dict[str, dict[str, str]] = {}

    def facts(entries: dict[str, Any], source: str) -> None:
        values.update(entries)
        sources.update({key: source for key in entries})

    def efforts(levels: list[str], default: str | None = None) -> None:
        entries: dict[str, Any] = {"reasoning_output": True, "reasoning_effort_levels": levels}
        if default is not None:
            entries["reasoning_effort_default"] = default
        # These controls use the documented OpenAI-format Gateway dialects.
        for scope in ("unified:chat", "unified:responses", "openai:chat", "openai:responses"):
            scoped[scope] = dict(entries)
            scoped_sources[scope] = {key: REASONING_MODELS for key in entries}

    if name in {"system.ai.gpt-6-1-sol", "system.ai.gpt-6-sol", "system.ai.gpt-6-luna", "system.ai.gpt-6-astra"}:
        facts({"name": name.removeprefix("system.ai."), "max_input_tokens": 1_050_000,
               "context_window": 1_050_000, "max_output_tokens": 128_000,
               "text_inputs": True, "image_inputs": True, "audio_inputs": False,
               "video_inputs": False, "tool_calling": True}, SUPPORTED_MODELS)
        for key in ("max_input_tokens", "context_window", "max_output_tokens"):
            sources[key] = f"{SUPPORTED_MODELS} (provider limits) ; {OPENAI_MODELS}"
        levels = ["low", "medium", "high", "xhigh", "max"]
        if name in {"system.ai.gpt-6-sol", "system.ai.gpt-6-luna"}:
            levels.insert(0, "none")
        # GPT-6.1 documentation conflicts on `none`; expose the common subset.
        efforts(levels, "medium" if name != "system.ai.gpt-6-astra" else None)
    elif name in {"system.ai.claude-opus-5-5", "system.ai.claude-sonnet-5-5"}:
        facts({"name": name.removeprefix("system.ai."), "max_input_tokens": 1_000_000,
               "context_window": 1_000_000, "max_output_tokens": 128_000,
               "text_inputs": True, "image_inputs": name == "system.ai.claude-opus-5-5",
               "audio_inputs": False, "video_inputs": False, "tool_calling": True,
               "reasoning_output": True}, SUPPORTED_MODELS)
        for key in ("max_input_tokens", "context_window", "max_output_tokens"):
            sources[key] = f"{SUPPORTED_MODELS} (provider limits) ; {CLAUDE_CONTEXT}"
        # Native Anthropic effort documentation alone does not qualify the
        # unified dialect. Exact live evidence is layered separately below.
    elif name in {"system.ai.gemini-4-argon", "system.ai.databricks-gemini-4-argon", "system.ai.gemini-3-7-flash", "system.ai.gemini-3-8-flash"}:
        facts({"name": name.removeprefix("system.ai."), "text_inputs": True,
               "image_inputs": True, "audio_inputs": True, "video_inputs": True,
               "tool_calling": True}, SUPPORTED_MODELS)
        efforts(["low", "medium", "high"], "medium")
        if name in {"system.ai.gemini-4-argon", "system.ai.databricks-gemini-4-argon"}:
            facts({"max_input_tokens": 1_048_576, "context_window": 1_048_576,
                   "max_output_tokens": 65_536, "max_output_tokens_unary": 32_768}, f"{SUPPORTED_MODELS} (provider limits) ; {GEMINI_ARGON}")
    elif name in {"system.ai.glm-5-3", "system.ai.glm-5-3-flash", "system.ai.glm-5-2", "system.ai.kimi-k3",
                  "system.ai.deepseek-v4-1-flash", "system.ai.deepseek-v4-pro-0813", "system.ai.deepseek-v4-flash-0731"}:
        facts({"name": name.removeprefix("system.ai."), "text_inputs": True,
               "image_inputs": name in {"system.ai.glm-5-3-flash", "system.ai.kimi-k3", "system.ai.deepseek-v4-1-flash"},
               "audio_inputs": False, "video_inputs": False, "tool_calling": True}, SUPPORTED_MODELS)
        if name == "system.ai.glm-5-2":
            efforts(["high", "max"], "max")
        elif name == "system.ai.deepseek-v4-1-flash":
            efforts(["none", "low", "high", "xhigh", "max"], "max")
        else:
            efforts(["low", "high", "max"], "max")
        if name in {"system.ai.glm-5-3", "system.ai.glm-5-3-flash"}:
            facts({"max_input_tokens": 1_048_576, "context_window": 1_048_576}, SUPPORTED_MODELS)
        if name == "system.ai.glm-5-3":
            facts({"max_output_tokens": 65_536, "structured_output": True}, SUPPORTED_MODELS)
    return CapabilityEvidence(
        values=values, sources=sources, checked_at=CHECKED_AT if values else None,
        states={key: "documented" for key in values},
        scoped_values=scoped, scoped_sources=scoped_sources,
    )


def profile_for_target(name: str, *, upstream_profile: Mapping[str, Any] | None = None,
                       cli_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the same model facts as construction, then the runtime overrides."""
    from .providers.targets import resolve_target_policy

    policy = resolve_target_policy(name)
    return {**policy.capabilities.profile, **(upstream_profile or {}), **(cli_override or {})}


def capability_summary(name: str, *, effective_profile: Mapping[str, Any] | None = None,
                       overridden_keys: list[str] | None = None) -> dict[str, Any]:
    """Nonsecret model-card data; metadata visibility never verifies EXECUTE."""
    from .providers.targets import resolve_target_policy

    policy = resolve_target_policy(name)
    evidence = policy.capabilities
    scope = f"{policy.surface}:{policy.api}"
    values = dict(evidence.values)
    overrides = {key for key, value in (effective_profile or {}).items() if values.get(key) != value}
    overrides.update(overridden_keys or [])
    values.update(effective_profile or {})
    return {
        "target": policy.target.public_ref, "profile": policy.profile,
        "protocol": scope, "checked_at": evidence.checked_at,
        "values": values, "sources": {**evidence.sources, **{key: "runtime profile override" for key in overrides}},
        "states": {**evidence.states, **{key: "override" for key in overrides}},
        "qualified_features": [key for key in evidence.qualified_features.get(scope, []) if key not in overrides],
        "overrides": sorted(overrides),
        "qualification_context": dict(evidence.qualification_context.get(scope, {})),
        "inference_access": "unverified",
    }


def format_capability_card(name: str, *, effective_profile: Mapping[str, Any] | None = None,
                           overridden_keys: list[str] | None = None) -> str:
    """Compact, truthful detail for a Gateway picker footer."""
    card = capability_summary(name, effective_profile=effective_profile, overridden_keys=overridden_keys)
    values = card["values"]
    contexts = [values.get(key) for key in ("context_window", "max_input_tokens")]
    context = min((v for v in contexts if isinstance(v, int) and not isinstance(v, bool) and v > 0), default=None)
    output = values.get("max_output_tokens")
    levels = values.get("reasoning_effort_levels")
    limits = f"Context: {context:,}" if isinstance(context, int) else "Context: unknown"
    limits += f" · Output: {output:,}" if isinstance(output, int) else " · Output: unknown"
    effort = "Effort: " + (" / ".join(levels) if levels else "unavailable on this API" if levels == [] else "not qualified for this API")
    labels = {"tool_calling": "tool roundtrip", "stream_usage": "stream usage constraint",
              "tool_schema_dialect": "coding-tool schemas",
              "tool_choice": "forced choice unavailable", "forced_tool_choice": "forced choice unavailable",
              "reasoning_effort_levels": "effort controls", "reasoning_effort_default": "effort default constraint",
              "reasoning_effort_low": "low effort", "reasoning_effort_high": "high effort",
              "service_status": values.get("service_status", "service availability")}
    qualified = list(dict.fromkeys(labels.get(key, key) for key in card["qualified_features"]))
    context_profile = card["qualification_context"].get("profile")
    evidence = "Live evidence" + (f" ({context_profile})" if context_profile else "") + ": " + (", ".join(qualified) if qualified else "none")
    inputs = [label for label in ("text", "image", "audio", "video") if values.get(f"{label}_inputs") is True]
    tools = values.get("tool_calling")
    features = "Inputs: " + (", ".join(inputs) if inputs else "unknown")
    features += " · Tools: " + ("yes" if tools is True else "no" if tools is False else "unknown")
    features += " · JSON schema: " + ("yes" if values.get("structured_output") is True else "unknown")
    overrides = "\nOverrides: " + ", ".join(card["overrides"]) if card["overrides"] else ""
    return f"{limits}\n{effort}\n{features}\n{card['protocol']} · {evidence}{overrides}\nWorkspace authentication; EXECUTE access is not verified by discovery"


def install_model_capability_bridge() -> bool:
    """Project shared evidence into the locked runtime's profile/effort readers.

    Values are resolved on every read. Discovery/profile changes do not retain a
    second capability cache, and only Databricks entries are replaced. Import
    aliases in already-loaded consumers are updated as well.
    """
    import importlib
    import sys

    try:
        from deepagents_code import model_config
    except ImportError:
        return False
    original = model_config.get_model_profiles
    if getattr(original, "__manta_capabilities__", False):
        return True

    def get_model_profiles(*, cli_override: dict[str, Any] | None = None) -> Mapping[str, Any]:
        from .providers.targets import POLICY_ENV, _policy_inputs
        import os

        entries = original(cli_override=cli_override)
        result = dict(entries)
        names = {spec.removeprefix("databricks:") for spec in entries if spec.startswith("databricks:")}
        # Metadata for exact documented services stays readable after a remote
        # picker selection, even before local provider config is refreshed.
        # This does not add those services to the available-model inventory.
        names.update(DOCUMENTED_TARGETS)
        settings, _provider, inventory = _policy_inputs()
        names.update(settings.get("targets", {}))
        if os.environ.get(POLICY_ENV):
            names.update(key.split(":", 1)[1] for key in inventory if key.startswith(("service:", "endpoint:")))
        for name in names:
            spec = f"databricks:{name}"
            previous = entries.get(spec, {})
            # A malformed target/policy is surfaced by construction/doctor;
            # do not silently advertise controls from a guessed replacement.
            try:
                profile = profile_for_target(name, upstream_profile=previous.get("profile"), cli_override=cli_override)
            except ValueError:
                profile = dict(previous.get("profile", {}))
            overrides = frozenset(previous.get("overridden_keys", ()))
            result[spec] = model_config.ModelProfileEntry(profile=profile, overridden_keys=overrides)
        return MappingProxyType(result)

    get_model_profiles.__manta_capabilities__ = True
    model_config.get_model_profiles = get_model_profiles
    # These are the locked runtime's consumers. Future imports bind the patched
    # function; existing aliases must be updated explicitly.
    for module_name in ("deepagents_code.reasoning_effort", "deepagents_code.model_catalog",
                        "deepagents_code.tui.widgets.model_selector"):
        module = sys.modules.get(module_name)
        if module is not None and getattr(module, "get_model_profiles", None) is original:
            module.get_model_profiles = get_model_profiles
    effort_module = importlib.import_module("deepagents_code.reasoning_effort")
    effort_module.get_model_profiles = get_model_profiles
    _install_effort_parameter_bridge(effort_module)
    return True


def _install_effort_parameter_bridge(module: Any) -> None:
    """Use OpenAI-compatible effort precedence for Gateway session controls.

    The pinned runtime otherwise reads only the flat Databricks spelling, so
    changing `/effort` can retain a conflicting nested reasoning.effort value.
    Delegating its existing readers/remover preserves all unrelated options.
    """
    from functools import wraps

    for name in ("current_effort_from_model_params", "has_explicit_effort_model_params",
                 "without_effort_model_params"):
        original = getattr(module, name)
        if getattr(original, "__manta_effort__", False):
            continue

        def wrap(original: Any) -> Any:
            @wraps(original)
            def compatible(model_spec: str | None, *args: Any, **kwargs: Any) -> Any:
                if model_spec and model_spec.startswith("databricks:"):
                    model_spec = "openai:" + model_spec.split(":", 1)[1]
                return original(model_spec, *args, **kwargs)

            compatible.__manta_effort__ = True
            return compatible

        setattr(module, name, wrap(original))
