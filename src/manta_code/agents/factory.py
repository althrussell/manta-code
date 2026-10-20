"""Compile a Manta :class:`AgentDef` into a ``deepagents`` ``SubAgent`` dict.

This is where a Manta agent definition becomes something the runtime can
*enforce*. The upstream markdown loader produces a ``SubAgent`` with only
``name`` / ``description`` / ``system_prompt`` / ``model``; the factory fills in
the fields the SDK actually acts on:

- ``middleware`` — a :class:`~manta_code.middleware.policy.ToolPolicyMiddleware`
  that rejects disallowed / mutating / out-of-policy tool calls before they run.
  This is where ``read_only``, allow/deny lists, **and** per-path ``filesystem``
  rules are all enforced.
- ``interrupt_on`` — per-tool human-in-the-loop gating from the agent's
  ``approval`` list.
- ``model`` — the pinned ``provider:endpoint``.

Why no ``permissions``? ``deepagents``' native ``FilesystemPermission`` is only
honoured on backends without command execution; the ``deepagents-code`` runtime
uses a sandbox backend that *provides* ``execute``, and ``FilesystemMiddleware``
raises ``NotImplementedError`` at construction if it sees permissions there —
crashing the agent server. So Manta enforces filesystem boundaries through the
tool-policy middleware's ``wrap_tool_call`` instead (see ``middleware/policy``).

Heavy imports happen inside :func:`compile_subagent`, so the registry/CLI stay
importable without the ``agent`` extra; the factory is only called from the build
hook, which runs inside the agent server where the extra is present.
"""

from __future__ import annotations

from typing import Any

from .registry import AgentDef


#: Names of the background-task tools (must match ``tasks/tools.py``). Agents
#: that have not opted in via ``manta_tools=["tasks"]`` get these deny-listed:
#: deepagents subagents inherit the parent's extra tools unless their spec
#: declares its own, so without this a *read-only* agent could route around
#: its boundary by submitting a background ``swe`` task that writes files.
TASK_TOOL_NAMES: tuple[str, ...] = (
    "manta_task_submit",
    "manta_task_status",
    "manta_task_output",
    "manta_task_list",
    "manta_task_send",
    "manta_task_cancel",
    "manta_task_review",
    "manta_task_artifact",
)


def _effective_deny(defn: AgentDef) -> list[str]:
    """The agent's deny-list plus inherited task tools it has not opted into."""
    deny = list(defn.tools_deny)
    if "tasks" not in (defn.manta_tools or []):
        deny.extend(t for t in TASK_TOOL_NAMES if t not in deny)
    return deny


def _tool_policy_middleware(defn: AgentDef) -> list[Any]:
    """Return a one-element ``[ToolPolicyMiddleware]`` when any policy applies."""
    deny = _effective_deny(defn)
    needs_policy = (
        defn.read_only
        or defn.tools_allow is not None
        or bool(deny)
        or bool(defn.filesystem)
        or bool(defn.tools_ask)
    )
    if not needs_policy:
        return []
    from ..middleware.policy import ToolPolicyMiddleware

    return [
        ToolPolicyMiddleware(
            allow=defn.tools_allow,
            deny=deny,
            read_only=defn.read_only,
            filesystem=defn.filesystem,
            ask=defn.tools_ask,
            allow_delegation="tasks" in defn.manta_tools,
            agent_name=defn.name,
        )
    ]


def _interrupt_on(defn: AgentDef) -> dict[str, Any]:
    """Map ``approval`` + ``tools_ask`` to a deepagents ``interrupt_on`` dict.

    Interactive ASK rides upstream's proven HITL prompt (ADR 0011); a tool in
    both lists prompts once. The unattended fail-closed half of ASK lives in
    the tool-policy middleware.
    """
    return {tool: True for tool in {*defn.approval, *defn.tools_ask}}


def _runtime_interrupt_on(defn: AgentDef) -> dict[str, Any]:
    """Managed tasks checkpoint real decisions; legacy headless stays explicit."""
    import os
    from ..tasks.approvals import managed_task
    from ..tasks.events import unattended_run

    def required(name: str):
        def predicate(request: Any) -> bool:
            if managed_task():
                # ASK preapproval never waives an explicit approval policy.
                return name in defn.approval or os.environ.get("MANTA_ALLOW_ASKS", "").strip() != "1"
            return not unattended_run()
        return predicate

    return {name: {"allowed_decisions": ["approve", "reject"], "when": required(name)}
            for name in _interrupt_on(defn)}


def native_interrupt_on(tools: list[Any], *, primary: bool = False) -> dict[str, Any]:
    """Approval for native mutations added after dcode builds its tool map."""
    from ..runtime import current_launch_policy
    from ..tasks.approvals import managed_task
    launch = current_launch_policy() or {}
    managed = managed_task()
    if (launch.get("auto_approve") and not managed) or not any(getattr(tool, "name", "") == "run_job" for tool in tools):
        return {}
    from deepagents_code.agent import _interrupt_predicate
    return {"run_job": {
        "allowed_decisions": ["approve", "reject"],
        "description": "Trigger a Databricks job run.",
        "when": (lambda request: True) if managed else _interrupt_predicate(auto_mode_enabled=bool(primary and launch.get("auto_mode_enabled"))),
    }}


def install_approval_gates(middleware: list[Any], gates: dict[str, Any]) -> list[Any]:
    """Merge into the async live-mode gate, preserving a single HITL slot."""
    if not gates:
        return middleware
    from deepagents_code.agent import AsyncApprovalHITLMiddleware
    from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
    existing = next((mw for mw in middleware if isinstance(mw, HumanInTheLoopMiddleware)), None)
    merged = {**(existing.interrupt_on if existing is not None else {}), **gates}
    result = list(middleware)
    if existing is not None and type(existing) is not HumanInTheLoopMiddleware:
        import copy
        gate = copy.copy(existing)
        # Preserve specialized dcode Auto/classifier behavior and normalize
        # only the merged map; replacing it with stock HITL loses that policy.
        gate.interrupt_on = HumanInTheLoopMiddleware(merged).interrupt_on
    else:
        gate = AsyncApprovalHITLMiddleware(merged)
    if existing is not None:
        result[result.index(existing)] = gate
    else:
        result.append(gate)
    return result


def _manta_tools(defn: AgentDef) -> list[Any]:
    """Resolve the agent's ``manta_tools`` groups to LangChain tools.

    Currently one group: ``"tasks"`` (background-task submit/status/output/
    list/cancel — the chief-of-staff surface). Guarded so an unavailable
    group never blocks agent construction.
    """
    tools: list[Any] = []
    if "tasks" in (defn.manta_tools or []):
        from ..tasks.tools import build_task_tools

        tools.extend(build_task_tools())
    return tools


def compile_subagent(
    defn: AgentDef,
    *,
    extra_middleware: list[Any] | None = None,
) -> dict[str, Any]:
    """Compile ``defn`` into a ``deepagents`` ``SubAgent`` dict.

    ``extra_middleware`` (e.g. a per-agent budget governor) is appended after
    the tool-policy middleware. The returned dict only sets optional keys when
    they carry information, so an unconstrained agent inherits the parent's
    tools/permissions exactly as a plain markdown subagent would.
    """
    subagent: dict[str, Any] = {
        "name": defn.name,
        "description": defn.description,
        "system_prompt": defn.system_prompt,
        "mode": defn.context_mode,
    }
    if defn.model:
        subagent["model"] = defn.model

    extras = list(extra_middleware or ())
    # Audit wraps the policy so rejected calls are recorded too.
    events = [mw for mw in extras if type(mw).__name__ == "EventLogMiddleware"]
    middleware = [*events, *_tool_policy_middleware(defn), *[mw for mw in extras if mw not in events]]
    from ..middleware.session_policy import SessionPolicyMiddleware

    # Protocol restoration may rebuild the model before an invocation. Keep
    # accounting inside it so delegated receipts describe the model/API used.
    economy = [mw for mw in middleware if type(mw).__name__ == "TokenEconomyMiddleware"]
    middleware = [mw for mw in middleware if mw not in economy]
    middleware.extend([SessionPolicyMiddleware(), *economy])
    if middleware:
        subagent["middleware"] = middleware

    interrupt_on = _interrupt_on(defn)
    if interrupt_on:
        subagent["interrupt_on"] = interrupt_on

    if defn.skills:
        subagent["skills"] = list(defn.skills)

    manta_tools = _manta_tools(defn)
    if manta_tools:
        subagent["tools"] = manta_tools

    subagent["_manta_definition"] = defn
    subagent["_manta_databricks_tools"] = list(defn.databricks_tools)
    return subagent


DATABRICKS_TOOL_GROUPS: dict[str, set[str]] = {
    "uc_catalog": {"uc_list_catalogs", "uc_list_schemas", "uc_list_tables", "uc_describe_table"},
    "sql": {"sql_query"},
    "jobs": {"list_jobs", "get_run_status", "get_run_diagnostics", "run_job"},
    "system_tables": {"sql_query"},
    "lineage": {"uc_table_lineage"},
}
DATABRICKS_TOOL_NAMES = set().union(*DATABRICKS_TOOL_GROUPS.values())


def select_agent_tools(defn: AgentDef, inherited: list[Any]) -> list[Any]:
    """Explicit profile tool selection, preserving inherited custom/MCP tools."""
    from ..agents.memory import build_memory_tools
    unknown = set(defn.databricks_tools) - DATABRICKS_TOOL_GROUPS.keys()
    if unknown:
        raise ValueError(f"{defn.name}: unknown databricks_tools groups: {', '.join(sorted(unknown))}")
    allowed = set().union(*(DATABRICKS_TOOL_GROUPS[g] for g in defn.databricks_tools)) if defn.databricks_tools else set()
    selected = [t for t in inherited if getattr(t, "name", "") not in {*TASK_TOOL_NAMES, "manta_remember"}
                and (getattr(t, "name", "") not in DATABRICKS_TOOL_NAMES or t.name in allowed)]
    for group in defn.databricks_tools:
        if not any(getattr(t, "name", "") in DATABRICKS_TOOL_GROUPS[group] for t in selected):
            raise ValueError(f"{defn.name}: requested Databricks tool group {group!r} is unavailable; configure a Databricks workspace first")
    selected.extend(_manta_tools(defn))
    from ..runtime import launch_memory_enabled
    if launch_memory_enabled():
        selected.extend(build_memory_tools(defn))
    return list({getattr(t, "name", str(id(t))): t for t in selected}.values())


def compose_subagent(spec: dict[str, Any], *, template: dict[str, Any] | None,
                     inherited_tools: list[Any], backend: Any = None) -> dict[str, Any]:
    """Compose a specialist with dcode's prepared launch control stack."""
    from ..runtime import RuntimeDispatchMiddleware, middleware_for_agent, model_for_agent
    from ..middleware.policy import ToolPolicyMiddleware
    defn = spec.get("_manta_definition")
    if defn is None:
        return dict(spec)
    result = {k: v for k, v in spec.items() if k != "_manta_definition"}
    from ..runtime import current_launch_policy
    launch = current_launch_policy()
    if defn.model and launch and launch.get("enforce_model_policy", True):
        from deepagents_code.agent import _has_resolvable_model_provider, _resolve_retry_owned_model
        from deepagents_code.model_config import ModelConfig
        ModelConfig.load().require_model_allowed(defn.model)
        if _has_resolvable_model_provider(defn.model):
            resolved = _resolve_retry_owned_model(defn.model, launch.get("cli_max_retries"))
            if resolved is not None:
                result["model"] = resolved
    if "model" in result:
        result["model"] = model_for_agent(result["model"], defn.name, definition=defn)
    selected_tools = select_agent_tools(defn, inherited_tools)
    upstream = []
    gates = {**native_interrupt_on(selected_tools), **_runtime_interrupt_on(defn)}
    for mw in (template or {}).get("middleware", ()):
        kind = type(mw).__name__
        if kind == "ForkGoalToolsMiddleware" or (kind == "ConfigurableModelMiddleware" and defn.model):
            continue
        clone = middleware_for_agent([mw], defn.name)[0]
        if kind == "AsyncApprovalHITLMiddleware":
            from deepagents_code.agent import AsyncApprovalHITLMiddleware
            clone = AsyncApprovalHITLMiddleware({**mw.interrupt_on, **gates})
            gates = {}
        upstream.append(clone)
    custom = list(result.get("middleware", ()))
    economy = [m for m in custom if type(m).__name__ == "TokenEconomyMiddleware"]
    continuity = [m for m in custom if type(m).__name__ == "SessionPolicyMiddleware"]
    events = [m for m in custom if type(m).__name__ == "EventLogMiddleware"]
    other = [m for m in custom if m not in [*economy, *continuity, *events]]
    mws = [*events, *other, *upstream, *continuity, *economy]
    if gates:
        mws = install_approval_gates(mws, gates)
        gates = {}
    for mw in mws:
        if isinstance(mw, ToolPolicyMiddleware):
            mw.bind_scope(backend)
    dispatch = RuntimeDispatchMiddleware(agent=defn.name, interrupt_on=_runtime_interrupt_on(defn))
    dispatch.definition = defn
    mws.insert(len(events), dispatch)
    dispatch.configure(mws, gates)
    result["middleware"] = mws
    result["tools"] = selected_tools
    # dcode's async-compatible HITL owns the slot when present; prevent a
    # second synchronous, parent-inherited HITL from replacing it.
    result["interrupt_on"] = gates
    return result


def compile_subagents(
    defs: list[AgentDef],
) -> list[dict[str, Any]]:
    """Compile many definitions, preserving order."""
    return [compile_subagent(defn) for defn in defs]
