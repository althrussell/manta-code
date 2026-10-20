"""The build hook: inject Manta's control plane into agent construction.

This is the central mechanism of ADR 0008. ``deepagents_code`` builds the agent
graph in its langgraph server subprocess via
``deepagents_code.agent.create_cli_agent``, which in turn calls the module-level
``deepagents_code.agent.create_deep_agent`` (imported at the top of that module).
We wrap that one symbol so Manta can, in a single place, enrich the agent with:

- **subagents** — Manta's compiled, *enforced* agents (built-ins + the user's
  registry), replacing any markdown subagent of the same name.
- **middleware** — orchestrator-level middleware (token economy / usage ledger)
  and per-agent recall middleware (durable memory is read by that middleware from
  Manta's own SQLite store, *not* injected as a graph ``store=``: the
  ``langgraph dev`` server rejects custom graph stores).
- **tools** — Databricks-native tools (UC / SQL / jobs / system tables).

Required controls are assembled atomically and fail closed with a diagnosis.
A registry, policy, accounting, memory or graph-validation failure stops launch;
optional advice remains best-effort.

Installation timing: the hook is installed from :mod:`manta_code.databricks_chat`
(imported by ``create_model`` via the provider ``class_path``), which runs in the
server subprocess *before* ``create_cli_agent``. It is also installed from
:func:`manta_code._boot.main` for the in-process CLI path. Idempotent either way.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("manta.hook")

#: Guards against re-wrapping ``create_deep_agent``.
_installed = False

#: Delegation policy appended to the *orchestrator's* system prompt (only when no
#: specific Manta profile is the primary agent). Plan requests are handled
#: deterministically by :class:`~manta_code.middleware.delegation.PlanDelegationMiddleware`
#: (the model can't reliably be nudged past its built-in ``write_todos``), so this
#: prompt nudge covers the things the model *does* decide: independent ``review``
#: delegation (no competing built-in) and optional ``swe`` delegation. Kept short
#: so it doesn't crowd out the base prompt.
ORCHESTRATOR_DELEGATION_POLICY = """\

## Delegating to specialist agents (Manta)

You have specialist subagents available through the `task` tool:

- **Code review:** after a non-trivial change, delegate to `subagent_type="review"` for an independent, read-only review.
- **Well-scoped implementation:** you may delegate to `subagent_type="swe"`.
- **Coordination / multi-agent work:** delegate to `subagent_type="chief"` to fan work out and collect results.

(Requests to *make a plan* are routed to the `planning` agent automatically, and messages starting with `@<agent>` are routed to that agent directly — you don't need to handle either yourself.)

For **long-running work**, use `manta_task_submit(agent, prompt)` to run it in the background: it returns a task id immediately and survives this session. Track with `manta_task_status` / `manta_task_list`; collect results with `manta_task_output`.

Each subagent is isolated: it does **not** see this conversation, so put everything it needs (files, goal, constraints) in the `task` `description`. Handle small or routine requests yourself rather than delegating.

When you learn something durable — a repo convention, a decision and its reason, a gotcha, a user preference — save it with `manta_remember` so future sessions start warm."""


def _warn(message: str) -> None:
    logger.warning(message)


def _subagent_name(spec: Any) -> str | None:
    """Return a subagent's name whether it is a dict spec or a compiled object."""
    if isinstance(spec, dict):
        return spec.get("name")
    return getattr(spec, "name", None)


def _required(value: Any, component: str) -> Any:
    if value is None:
        from .runtime import RuntimeBuildError
        raise RuntimeBuildError(f"Required Manta {component} could not be constructed. Run manta doctor.")
    return value


def _agent_extra_middleware(defn: Any) -> list[Any]:
    """Required memory, accounting and audit controls; optional advice."""
    from .agents.memory import agent_memory_middleware
    from .middleware.economy import agent_budget_middleware
    from .tasks.events import agent_event_middleware
    from .middleware.advice import agent_advice_middleware
    extras = [_required(agent_event_middleware(defn), "audit"),
              _required(agent_budget_middleware(defn), "accounting/budget")]
    from .runtime import launch_memory_enabled
    if defn.memory and launch_memory_enabled():
        extras.append(_required(agent_memory_middleware(defn), "memory"))
    try:
        advice = agent_advice_middleware(defn)
        if advice is not None:
            extras.append(advice)
    except Exception:
        logger.debug("Optional agent advice unavailable", exc_info=True)
    return extras


def _runtime_definitions() -> list[Any]:
    """Load every declared registry entry strictly for graph construction."""
    from .agents.defaults import merged_agents
    from .agents.registry import agents_root, load_agent
    directory = agents_root()
    entries = sorted(directory.iterdir()) if directory.is_dir() else []
    return merged_agents([load_agent(p.name) for p in entries if p.is_dir() and (p / "agent.toml").is_file()])


def build_manta_subagents() -> list[dict[str, Any]]:
    from .agents.factory import compile_subagent
    return [compile_subagent(d, extra_middleware=_agent_extra_middleware(d)) for d in _runtime_definitions()]


def active_agent_name() -> str | None:
    """Return the active top-level profile name, or ``None`` for the base agent.

    ``deepagents-code`` passes the selected profile to the server subprocess as
    ``DEEPAGENTS_CODE_SERVER_ASSISTANT_ID``. When the user picks a Manta agent in
    the ``/agents`` picker, this is how the build hook learns which one is the
    *primary* agent so it can enforce that agent's boundaries at the top level.
    The base ``agent`` profile is not a Manta agent, so it returns ``None``.
    """
    import os

    try:
        from deepagents_code._env_vars import SERVER_ENV_PREFIX

        key = f"{SERVER_ENV_PREFIX}ASSISTANT_ID"
    except Exception:  # noqa: BLE001 - fall back to the literal env var name
        key = "DEEPAGENTS_CODE_SERVER_ASSISTANT_ID"
    from .runtime import current_launch_policy
    launch = current_launch_policy() or {}
    name = launch.get("assistant_id") or os.environ.get(key)
    if not name or name == "agent":
        return None
    return name


def _active_profile_def() -> Any | None:
    name = active_agent_name()
    if not name:
        return None
    definition = next((a for a in _runtime_definitions() if a.name == name), None)
    if definition is None:
        from .runtime import RuntimeBuildError
        raise RuntimeBuildError(f"Selected Manta profile {name!r} has no valid agent definition. Run manta agents list or select the base agent.")
    return definition


def build_orchestrator_middleware() -> list[Any]:
    import os
    from .middleware.address import agent_address_middleware
    from .middleware.inbox import task_inbox_middleware
    from .middleware.routing import agent_model_pin_middleware, default_routing_middleware
    from .middleware.advice import orchestrator_advice_middleware
    explicit_model = os.environ.get("MANTA_EXPLICIT_MODEL") == "1"
    definition = _active_profile_def()
    middleware = []
    for item in (agent_address_middleware(), task_inbox_middleware()):
        if item is not None:
            middleware.append(item)
    if definition is not None:
        from .agents.factory import _tool_policy_middleware
        if definition.model and not explicit_model:
            middleware.append(_required(agent_model_pin_middleware(definition), "selected profile model pin"))
        middleware.extend(_tool_policy_middleware(definition))
        middleware.extend(_agent_extra_middleware(definition))
    else:
        from .middleware.delegation import plan_delegation_middleware
        from .middleware.economy import orchestrator_middleware
        from .agents.memory import orchestrator_memory_middleware
        from .tasks.events import orchestrator_event_middleware
        planning = plan_delegation_middleware()
        if planning is not None:
            middleware.append(planning)
        middleware.extend(orchestrator_middleware())
        from .runtime import launch_memory_enabled
        if launch_memory_enabled():
            middleware.append(_required(orchestrator_memory_middleware(), "orchestrator memory"))
        middleware.append(_required(orchestrator_event_middleware(), "orchestrator audit"))
        try:
            advice = orchestrator_advice_middleware()
            if advice is not None:
                middleware.append(advice)
        except Exception:
            logger.debug("Optional model advice unavailable", exc_info=True)
    if not explicit_model and definition is None:
        middleware.extend(default_routing_middleware())
    return middleware


def build_databricks_tools() -> list[Any]:
    """Detect-and-enable; configured integration failures abort construction."""
    from .auth import databricks_configured
    if not databricks_configured():
        return []
    from .databricks_tools import build_default_databricks_tools
    tools = build_default_databricks_tools()
    if not tools:
        raise RuntimeError("Databricks is configured but required native tools could not be built")
    return tools


def build_task_tools() -> list[Any]:
    from .tasks.tools import build_task_tools as build
    return build()


def build_memory_tools(defn: Any = None) -> list[Any]:
    from .agents.memory import build_memory_tools as build
    from .runtime import launch_memory_enabled
    return build(defn) if launch_memory_enabled() else []


def _maybe_add_delegation_policy(kwargs: dict[str, Any]) -> None:
    """Append the delegation nudge to the orchestrator's system prompt.

    Only applies to the **base orchestrator** (no specific Manta profile selected
    as primary): when the user has picked an agent like ``swe`` as the primary
    loop, that agent's own prompt governs and a "delegate to swe" nudge would be
    circular. Appends rather than replaces, and only when ``create_cli_agent``
    already supplied a non-empty ``system_prompt`` string — so we never clobber
    deepagents' rich default by turning a ``None`` (compute-the-default sentinel)
    into our short policy. Guarded; a failure leaves the prompt untouched.
    """
    try:
        if active_agent_name() is not None:
            return
        prompt = kwargs.get("system_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return
        if "Delegating to specialist agents (Manta)" in prompt:
            return
        kwargs["system_prompt"] = prompt + "\n" + ORCHESTRATOR_DELEGATION_POLICY
    except Exception:  # noqa: BLE001 - steering is best-effort, never block launch
        logger.debug("Manta delegation-policy injection failed", exc_info=True)


def _assemble_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Atomically assemble required controls, preserving dcode launch policy."""
    from .agents.factory import compose_subagent, select_agent_tools, _runtime_interrupt_on, native_interrupt_on, install_approval_gates
    from .middleware.policy import ToolPolicyMiddleware
    from .middleware.session_policy import SessionPolicyMiddleware
    from .runtime import RuntimeDispatchMiddleware, middleware_for_agent, model_for_agent
    if any(isinstance(m, RuntimeDispatchMiddleware) for m in kwargs.get("middleware", ())):
        return kwargs
    candidate = dict(kwargs)
    definition = _active_profile_def()
    role = definition.name if definition else "orchestrator"
    if "model" in candidate:
        candidate["model"] = model_for_agent(candidate["model"], role)
    inherited = list(kwargs.get("tools") or ())
    native = build_databricks_tools()
    extra_tools = [*native, *build_task_tools(), *build_memory_tools(definition)]
    tools = list({getattr(t, "name", str(id(t))): t for t in [*inherited, *extra_tools]}.values())
    specialist_tools = list(tools)
    if definition is not None:
        tools = select_agent_tools(definition, tools)
    candidate["tools"] = tools
    existing = [dict(s) if isinstance(s, dict) else s for s in (kwargs.get("subagents") or ())]
    template = next((s for s in existing if isinstance(s, dict) and s.get("name") == "general-purpose"), None)
    manta = build_manta_subagents()
    names = {s["name"] for s in manta}
    kept = [s for s in existing if _subagent_name(s) not in names]
    composed = [compose_subagent(s, template=template, inherited_tools=specialist_tools, backend=kwargs.get("backend")) for s in manta]
    # Existing markdown/GP children already carry the upstream stack. Guard
    # their JS dispatch too and retain their explicit filesystem restrictions.
    for spec in kept:
        if isinstance(spec, dict) and "runnable" not in spec:
            mws = middleware_for_agent(list(spec.get("middleware", ())), spec["name"])
            native_gates = native_interrupt_on(spec.get("tools", tools))
            mws = install_approval_gates(mws, native_gates)
            if native_gates:
                spec["interrupt_on"] = {}
            if not any(isinstance(m, RuntimeDispatchMiddleware) for m in mws):
                guard = RuntimeDispatchMiddleware(agent=spec["name"])
                guard.configure(mws, spec.get("interrupt_on"))
                spec["middleware"] = [guard, *mws]
    candidate["subagents"] = [*kept, *composed] or None
    for spec in candidate["subagents"] or ():
        if isinstance(spec, dict) and "runnable" not in spec:
            inherited_model = spec.get("model", candidate.get("model"))
            if inherited_model is not None:
                spec["model"] = model_for_agent(inherited_model, spec["name"])
    orchestrator = build_orchestrator_middleware()
    upstream = middleware_for_agent(list(kwargs.get("middleware") or ()), role)
    configurable = [m for m in upstream if type(m).__name__ == "ConfigurableModelMiddleware"]
    events = [m for m in orchestrator if type(m).__name__ == "EventLogMiddleware"]
    economy = [m for m in orchestrator if type(m).__name__ == "TokenEconomyMiddleware"]
    control = [m for m in orchestrator if m not in [*events, *economy]]
    other = [m for m in upstream if m not in configurable and not isinstance(m, RuntimeDispatchMiddleware)]
    gates = {**native_interrupt_on(tools, primary=True), **(_runtime_interrupt_on(definition) if definition is not None else {})}
    if gates:
        other = install_approval_gates(other, {**(kwargs.get("interrupt_on") or {}), **gates})
        candidate["interrupt_on"] = {}
    dispatch = RuntimeDispatchMiddleware(agent=definition.name if definition else "orchestrator", interrupt_on=gates)
    dispatch.databricks_selection = list(definition.databricks_tools) if definition else ["auto"]
    stack = [*configurable, *events, dispatch, *control, *other, SessionPolicyMiddleware(), *economy]
    for mw in stack:
        if isinstance(mw, ToolPolicyMiddleware):
            mw.bind_scope(kwargs.get("backend"))
    dispatch.configure(stack, candidate.get("interrupt_on"))
    candidate["middleware"] = stack
    for spec in candidate.get("subagents") or ():
        if isinstance(spec, dict) and spec.get("mode") == "fork":
            effective = list({m.name: m for m in [*stack, *spec.get("middleware", ())]}.values())
            for mw in spec.get("middleware", ()):
                if isinstance(mw, RuntimeDispatchMiddleware):
                    mw.configure(effective, spec.get("interrupt_on"))
    if definition is not None and definition.system_prompt:
        marker = f"## Manta role: {definition.name}"
        prompt = candidate.get("system_prompt") or ""
        if isinstance(prompt, str) and marker not in prompt:
            candidate["system_prompt"] = f"{prompt}\n\n{marker}\n{definition.system_prompt}".strip()
    _maybe_add_delegation_policy(candidate)
    kwargs.clear()
    kwargs.update(candidate)
    return kwargs


def enrich_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Commit a complete candidate or restore all touched adapter settings."""
    middleware = list(kwargs.get("middleware") or ())
    for spec in kwargs.get("subagents") or ():
        if isinstance(spec, dict):
            middleware.extend(spec.get("middleware") or ())
    snapshots = []
    for mw in middleware:
        for attribute in ("_ptc", "_approval", "_guarded_dispatch_names"):
            if hasattr(mw, attribute):
                value = getattr(mw, attribute)
                snapshots.append((mw, attribute, set(value) if isinstance(value, set) else value))
    try:
        return _assemble_kwargs(kwargs)
    except BaseException:
        for mw, attribute, value in snapshots:
            setattr(mw, attribute, value)
        raise


def install_build_hook() -> bool:
    """Install an atomic, fail-closed build hook and usage collector."""
    global _installed
    from .accounting import install_usage_collector
    from .server_graph import install_server_bootstrap
    install_usage_collector()
    if not install_server_bootstrap():
        return False
    if _installed:
        return True
    try:
        from deepagents_code import agent as dc_agent
    except ImportError:
        return False
    from .runtime import install_launch_policy_capture
    install_launch_policy_capture(dc_agent)
    original = getattr(dc_agent, "create_deep_agent", None)
    if not callable(original):
        return False

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from .runtime import RuntimeBuildError, effective_manifest, publish_manifest
        publish_manifest(None)
        try:
            candidate = enrich_kwargs(dict(kwargs))
            graph = original(*args, **candidate)
            manifest = effective_manifest(graph, candidate)
            publish_manifest(manifest)
            return graph
        except Exception as exc:
            raise RuntimeBuildError(f"Manta required runtime controls could not be validated: {exc}. Agent execution was not started. Run manta doctor.") from exc

    wrapped.__manta_hook__ = True
    dc_agent.create_deep_agent = wrapped
    _installed = True
    return True
