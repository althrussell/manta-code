"""One definition service for the agent workspace, CLI, SDK and runtime.

The registry stores overrides, including disabled built-in tombstones. This
service resolves built-ins and overrides before every action, and supplies a
stable revision so an open editor cannot silently overwrite a concurrent edit.
Validation compiles the real specialist controls without invoking a model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .defaults import DEFAULT_AGENT_NAMES, merged_agents
from .registry import AgentDef, definition_revision, list_agents, save_agent


class AgentWorkspace:
    def __init__(self, *, root: Path | None = None) -> None:
        self.root = root

    def list(self, *, include_disabled: bool = False) -> list[AgentDef]:
        return merged_agents(list_agents(root=self.root), include_disabled=include_disabled)

    def resolve(self, name: str, *, include_disabled: bool = False) -> AgentDef:
        for definition in self.list(include_disabled=True):
            if definition.name == name:
                if not definition.enabled and not include_disabled:
                    raise ValueError(f"agent {name!r} is disabled; enable it before dispatch")
                return definition.model_copy(deep=True)
        raise FileNotFoundError(f"no Manta agent named {name!r}")

    def revision(self) -> str:
        values = [definition.model_dump(mode="json") for definition in self.list(include_disabled=True)]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def create(self, definition: AgentDef) -> AgentDef:
        if definition.name == "agent":
            raise ValueError("'agent' is reserved for the base session; choose a specialist name")
        if any(item.name == definition.name for item in self.list(include_disabled=True)):
            raise FileExistsError(f"agent {definition.name!r} already exists; clone it under a new name")
        definition = AgentDef.model_validate(definition.model_dump())
        self.validate(definition)
        save_agent(definition, root=self.root, create_only=True)
        return self.resolve(definition.name, include_disabled=True)

    def clone(self, name: str, new_name: str) -> AgentDef:
        source = self.resolve(name, include_disabled=True)
        # A clone gets its own namespace unless the user explicitly opts back
        # into shared memory after reviewing the effective definition.
        return self.create(AgentDef.model_validate({
            **source.model_dump(), "name": new_name, "enabled": True,
            "memory_namespace": None,
        }))

    def update(self, name: str, changes: dict[str, Any], *, expected_revision: str | None = None) -> AgentDef:
        current = self.resolve(name, include_disabled=True)
        if "name" in changes and changes["name"] != name:
            raise ValueError("renaming changes agent identity; clone the agent instead")
        if expected_revision is not None and definition_revision(current) != expected_revision:
            raise ValueError(f"agent {name!r} changed; reload before saving")
        definition = AgentDef.model_validate({**current.model_dump(), **changes})
        self.validate(definition)
        # A built-in has no persisted revision yet; save only the override.
        from .registry import agent_exists

        persisted = agent_exists(name, root=self.root)
        save_agent(definition, root=self.root, create_only=not persisted,
                   expected_revision=expected_revision if persisted else None)
        return self.resolve(name, include_disabled=True)

    def set_enabled(self, name: str, enabled: bool) -> AgentDef:
        return self.update(name, {"enabled": enabled})

    def source(self, name: str) -> str:
        user_names = {item.name for item in list_agents(root=self.root)}
        if name in user_names:
            return "user override" if name in DEFAULT_AGENT_NAMES else "user"
        return "built-in"

    def validate(self, definition: AgentDef) -> list[str]:
        """Validate stored controls without credentials, network or inference."""
        from .factory import DATABRICKS_TOOL_GROUPS

        definition = AgentDef.model_validate(definition.model_dump())
        unknown = set(definition.databricks_tools) - DATABRICKS_TOOL_GROUPS.keys()
        if unknown:
            raise ValueError(f"unknown Databricks tool groups: {', '.join(sorted(unknown))}")
        unknown_manta = set(definition.manta_tools) - {"tasks"}
        if unknown_manta:
            raise ValueError(f"unknown Manta tool groups: {', '.join(sorted(unknown_manta))}")
        warnings = []
        if definition.model:
            from ..providers import parse_model_ref

            ref = parse_model_ref(definition.model)
            if ref is None:
                raise ValueError("model must be an exact provider:model reference")
            if ref.provider == "databricks":
                from ..providers.targets import resolve_target_policy

                policy = resolve_target_policy(ref.model)
                values = policy.capabilities.values
                effort_levels = values.get("reasoning_effort_levels")
                if definition.reasoning_effort is not None:
                    if effort_levels is not None and definition.reasoning_effort not in effort_levels:
                        raise ValueError(f"{definition.model} supports effort {effort_levels}; requested {definition.reasoning_effort!r}")
                    if effort_levels is None:
                        warnings.append("Reasoning effort has no qualified capability evidence on this API.")
                maximum = values.get("max_output_tokens")
                if maximum and definition.max_output_tokens and definition.max_output_tokens > maximum:
                    raise ValueError(f"output limit {definition.max_output_tokens} exceeds model limit {maximum}")
                warnings.append("Gateway inference access is checked when the model is called.")
        elif definition.reasoning_effort is not None or definition.max_output_tokens is not None:
            warnings.append("Model controls will be validated against the inherited model at dispatch.")
        if definition.context_mode == "fork":
            warnings.append("Forked inline delegation receives the parent's conversation. Detached tasks default to isolation; a forked task requires an explicit visible conversation snapshot.")
        return warnings

    def test(self, name: str) -> dict[str, Any]:
        """Compile the actual specialist policy offline; no tool effects occur."""
        from .factory import compile_subagent

        definition = self.resolve(name, include_disabled=True)
        warnings = self.validate(definition)
        spec = compile_subagent(definition)
        return {"ok": True, "kind": "offline compilation", "agent": name,
                "revision": definition_revision(definition), "context_mode": spec["mode"],
                "middleware": [type(value).__name__ for value in spec.get("middleware", [])],
                "warnings": warnings, "inference_access": "unverified"}

    def effective(self, name: str, *, inherited_model: str | None = None,
                  inherited_tools: list[str] | None = None) -> dict[str, Any]:
        """Inspect resolved controls, with inherited/unknown values explicit."""
        from .factory import DATABRICKS_TOOL_GROUPS, TASK_TOOL_NAMES, _effective_deny

        definition = self.resolve(name, include_disabled=True)
        model = definition.model or inherited_model
        selected = None
        if inherited_tools is not None:
            from .factory import DATABRICKS_TOOL_NAMES

            native = set().union(*(DATABRICKS_TOOL_GROUPS[group] for group in definition.databricks_tools))
            selected = [tool for tool in inherited_tools if tool not in {*TASK_TOOL_NAMES, "manta_remember"}
                        and (tool not in DATABRICKS_TOOL_NAMES or tool in native)]
            if "tasks" in definition.manta_tools:
                selected.extend(TASK_TOOL_NAMES)
            if definition.memory:
                selected.append("manta_remember")
            selected = sorted(set(selected))
        model_details = None
        if model and model.startswith("databricks:"):
            from ..model_capabilities import capability_summary

            model_details = capability_summary(model)
        return {
            "schema_version": 1, "name": name, "enabled": definition.enabled,
            "source": self.source(name), "revision": definition_revision(definition),
            "definition": definition.model_dump(mode="json"),
            "model": model, "model_source": "pinned" if definition.model else "inherited",
            "reasoning_effort": definition.reasoning_effort,
            "max_output_tokens": definition.max_output_tokens,
            "detached_shell": {"enabled": bool(definition.task_shell_allow_list),
                               "allow_list": definition.task_shell_allow_list},
            "model_capabilities": model_details,
            "context_mode": definition.context_mode,
            "detached_context_mode": "isolated",
            "tools": selected, "tool_source": "resolved launch tools" if selected is not None else "resolved at runtime from inherited launch tools",
            "tool_groups": {"databricks": definition.databricks_tools, "manta": definition.manta_tools},
            "policy": {"read_only": definition.read_only, "allow": definition.tools_allow,
                       "deny": _effective_deny(definition), "ask": definition.tools_ask,
                       "approval": definition.approval,
                       "filesystem": [rule.model_dump() for rule in definition.filesystem]},
            "memory": {"enabled": definition.memory, "namespace": definition.effective_namespace(),
                       "scope": "project + agent namespace"},
            "skills": definition.skills or ("inherited from parent" if definition.context_mode == "fork" else []),
            "budget": {"max_tokens": definition.budget_max_tokens, "max_usd": definition.budget_max_usd},
            "warnings": self.validate(definition),
        }
