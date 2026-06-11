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

Reliability is non-negotiable (ADR 0008): the wrapper degrades gracefully. If
loading the registry or compiling agents raises, it logs a warning and calls the
original ``create_deep_agent`` unchanged, so ``manta`` still launches — it just
loses the Manta enrichment rather than failing to start.

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

Each subagent is isolated: it does **not** see this conversation, so put everything it needs (files, goal, constraints) in the `task` `description`. Handle small or routine requests yourself rather than delegating."""


def _warn(message: str) -> None:
    logger.warning(message)


def _subagent_name(spec: Any) -> str | None:
    """Return a subagent's name whether it is a dict spec or a compiled object."""
    if isinstance(spec, dict):
        return spec.get("name")
    return getattr(spec, "name", None)


def _agent_extra_middleware(defn: Any) -> list[Any]:
    """Per-agent middleware to attach when compiling (memory, budget).

    Filled in by later phases (memory in Phase 3, budget in Phase 4). Each
    builder is imported lazily and guarded so a missing/!installed piece never
    breaks agent construction.
    """
    extras: list[Any] = []

    # Phase 3: durable per-agent memory middleware.
    try:
        from .agents.memory import agent_memory_middleware

        mw = agent_memory_middleware(defn)
        if mw is not None:
            extras.append(mw)
    except Exception:  # noqa: BLE001 - optional; never block construction
        pass

    # Phase 4: per-agent token budget governor.
    try:
        from .middleware.economy import agent_budget_middleware

        mw = agent_budget_middleware(defn)
        if mw is not None:
            extras.append(mw)
    except Exception:  # noqa: BLE001
        pass

    # ADR 0010 Phase B: tool-call event log (the `manta status` feed, including
    # the approval/denial audit records).
    try:
        from .tasks.events import agent_event_middleware

        mw = agent_event_middleware(defn)
        if mw is not None:
            extras.append(mw)
    except Exception:  # noqa: BLE001
        pass

    # ADR 0010 Phase C: proactive model advice (session signals + tiered tips).
    try:
        from .middleware.advice import agent_advice_middleware

        mw = agent_advice_middleware(defn)
        if mw is not None:
            extras.append(mw)
    except Exception:  # noqa: BLE001
        pass

    return extras


def build_manta_subagents() -> list[dict[str, Any]]:
    """Compile Manta's built-in + user agents into enforced ``SubAgent`` dicts."""
    from .agents.defaults import merged_agents
    from .agents.factory import compile_subagent
    from .agents.registry import list_agents

    defs = merged_agents(list_agents())
    return [compile_subagent(d, extra_middleware=_agent_extra_middleware(d)) for d in defs]


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
    name = os.environ.get(key)
    if not name or name == "agent":
        return None
    return name


def _active_profile_def() -> Any | None:
    """Resolve the active profile name to a merged :class:`AgentDef`, or ``None``."""
    name = active_agent_name()
    if not name:
        return None
    try:
        from .agents.defaults import merged_agents
        from .agents.registry import list_agents

        return next((a for a in merged_agents(list_agents()) if a.name == name), None)
    except Exception:  # noqa: BLE001
        return None


def build_orchestrator_middleware() -> list[Any]:
    """Top-level (primary agent) middleware: enforcement + economy + routing.

    When the active profile is a Manta agent (the user selected it in the
    ``/agents`` picker), this enforces *that agent's* boundaries on the primary
    loop — its model pin, tool policy (read-only / allow / deny / filesystem),
    memory recall for its namespace, and economy attributed to it (with its
    budget caps). Otherwise it applies the generic orchestrator accounting.
    Routing (cost-aware escalation) is applied in both cases.

    Economy is attributed to the active profile *instead of* a separate
    orchestrator instance so a call is never double-counted in the ledger. Each
    piece is guarded so a missing module is simply skipped.
    """
    middleware: list[Any] = []
    defn = _active_profile_def()

    # @agent addressing (ADR 0010 Phase B), outermost in both branches: an
    # explicit "@swe …" or "@swe … &" turn outranks every other routing
    # decision (it only fires on an explicit @-mention of a real agent).
    try:
        from .middleware.address import agent_address_middleware

        mw = agent_address_middleware()
        if mw is not None:
            middleware.append(mw)
    except Exception:  # noqa: BLE001
        pass

    if defn is not None:
        # Model pin first (outermost) so accounting/routing below see the pinned
        # model, and the primary loop actually runs on the profile's model — not
        # just delegated subagents.
        try:
            from .middleware.routing import agent_model_pin_middleware

            mw = agent_model_pin_middleware(defn)
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .agents.factory import _tool_policy_middleware

            middleware.extend(_tool_policy_middleware(defn))
        except Exception:  # noqa: BLE001
            pass
        try:
            from .agents.memory import agent_memory_middleware

            mw = agent_memory_middleware(defn)
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .middleware.economy import agent_budget_middleware

            mw = agent_budget_middleware(defn)
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .tasks.events import agent_event_middleware

            mw = agent_event_middleware(defn)
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .middleware.advice import agent_advice_middleware

            mw = agent_advice_middleware(defn)
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
    else:
        # Deterministic plan-request delegation, outermost so it can short-circuit
        # the model call entirely (no accounting noise for a call that never runs).
        try:
            from .middleware.delegation import plan_delegation_middleware

            mw = plan_delegation_middleware()
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .middleware.economy import orchestrator_middleware

            middleware.extend(orchestrator_middleware())
        except Exception:  # noqa: BLE001
            pass
        try:
            from .tasks.events import orchestrator_event_middleware

            mw = orchestrator_event_middleware()
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .middleware.advice import orchestrator_advice_middleware

            mw = orchestrator_advice_middleware()
            if mw is not None:
                middleware.append(mw)
        except Exception:  # noqa: BLE001
            pass

    try:
        from .middleware.routing import default_routing_middleware

        middleware.extend(default_routing_middleware())
    except Exception:  # noqa: BLE001
        pass
    return middleware


def build_databricks_tools() -> list[Any]:
    """Databricks-native tools to expose to the main agent (Phase 2).

    Databricks is detect-and-enable (ADR 0010): with no workspace configured,
    no tools are injected — off Databricks they would only error at call time
    and crowd the tool list.
    """
    try:
        from .auth import databricks_configured

        if not databricks_configured():
            return []
        from .databricks_tools import build_default_databricks_tools

        return build_default_databricks_tools()
    except Exception:  # noqa: BLE001
        return []


def build_task_tools() -> list[Any]:
    """Background-task tools for the main agent (ADR 0010 Phase B).

    Gives the orchestrator the same task surface as the ``chief`` agent so
    "run this in the background" works from any session.
    """
    try:
        from .tasks.tools import build_task_tools as _build

        return _build()
    except Exception:  # noqa: BLE001
        return []


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


def enrich_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject Manta's subagents, middleware, and tools into ``kwargs``.

    Mutates and returns ``kwargs`` (the dict passed to ``create_deep_agent``).
    Pure enough to unit-test directly: no global state beyond the registry it
    reads. Manta subagents replace any inherited subagent of the same name. For
    the base orchestrator it also appends a short delegation policy to the system
    prompt so planning/review work is routed to the specialist agents.

    Note: Manta does **not** inject a graph ``store=``. Durable memory is read by
    the per-agent recall middleware from Manta's own SQLite store. The
    ``langgraph dev`` server that runs the agent rejects graphs that carry a
    custom ``BaseStore``, so injecting one here would crash the server at startup.
    """
    manta_subagents = build_manta_subagents()
    if manta_subagents:
        manta_names = {s["name"] for s in manta_subagents}
        existing = list(kwargs.get("subagents") or [])
        kept = [s for s in existing if _subagent_name(s) not in manta_names]
        merged = [*kept, *manta_subagents]
        kwargs["subagents"] = merged or None

    orchestrator_mw = build_orchestrator_middleware()
    if orchestrator_mw:
        # Manta middleware runs first (outermost) so accounting/policy wrap
        # everything below.
        kwargs["middleware"] = [*orchestrator_mw, *list(kwargs.get("middleware") or ())]

    _maybe_add_delegation_policy(kwargs)

    extra_tools = [*build_databricks_tools(), *build_task_tools()]
    if extra_tools:
        kwargs["tools"] = [*list(kwargs.get("tools") or ()), *extra_tools]

    return kwargs


def install_build_hook() -> bool:
    """Wrap ``deepagents_code.agent.create_deep_agent`` with Manta enrichment.

    Idempotent. Returns ``True`` when the hook is installed (or already was),
    ``False`` when the patch target is unavailable (caller continues to launch
    vanilla). The wrapper itself never raises out of enrichment — on any failure
    it logs and calls the original unchanged.
    """
    global _installed
    if _installed:
        return True
    try:
        from deepagents_code import agent as dc_agent
    except Exception:
        return False

    original = getattr(dc_agent, "create_deep_agent", None)
    if original is None or not callable(original):
        _warn(
            "deepagents_code.agent.create_deep_agent not found; Manta control "
            "plane disabled, launching vanilla. Run `manta doctor`."
        )
        return False

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            enrich_kwargs(kwargs)
        except Exception as exc:  # noqa: BLE001 - reliability: never block launch
            _warn(f"Manta control-plane injection failed ({exc}); launching vanilla.")
        return original(*args, **kwargs)

    wrapped.__manta_hook__ = True  # type: ignore[attr-defined]  # marker for tests/idempotency
    dc_agent.create_deep_agent = wrapped
    _installed = True
    return True
