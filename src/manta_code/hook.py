"""The build hook: inject Manta's control plane into agent construction.

This is the central mechanism of ADR 0008. ``deepagents_code`` builds the agent
graph in its langgraph server subprocess via
``deepagents_code.agent.create_cli_agent``, which in turn calls the module-level
``deepagents_code.agent.create_deep_agent`` (imported at the top of that module).
We wrap that one symbol so Manta can, in a single place, enrich the agent with:

- **subagents** — Manta's compiled, *enforced* agents (built-ins + the user's
  registry), replacing any markdown subagent of the same name.
- **middleware** — orchestrator-level middleware (token economy / usage ledger).
- **store** — a persistent ``BaseStore`` for durable per-agent memory.
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

    return extras


def build_manta_subagents() -> list[dict[str, Any]]:
    """Compile Manta's built-in + user agents into enforced ``SubAgent`` dicts."""
    from .agents.defaults import merged_agents
    from .agents.factory import compile_subagent
    from .agents.registry import list_agents

    defs = merged_agents(list_agents())
    return [compile_subagent(d, extra_middleware=_agent_extra_middleware(d)) for d in defs]


def build_orchestrator_middleware() -> list[Any]:
    """Orchestrator-level middleware: token economy/ledger, then cost-aware routing.

    Order matters: economy (accounting/budget) is outermost so it sees the final
    model choice; routing runs closer to the call so its escalation is what gets
    priced. Each piece is guarded so a missing module is simply skipped.
    """
    middleware: list[Any] = []
    try:
        from .middleware.economy import orchestrator_middleware

        middleware.extend(orchestrator_middleware())
    except Exception:  # noqa: BLE001
        pass
    try:
        from .middleware.routing import default_routing_middleware

        middleware.extend(default_routing_middleware())
    except Exception:  # noqa: BLE001
        pass
    return middleware


def build_store() -> Any | None:
    """The persistent ``BaseStore`` for durable memory (Phase 3)."""
    try:
        from .agents.memory import build_memory_store

        return build_memory_store()
    except Exception:  # noqa: BLE001
        return None


def build_databricks_tools() -> list[Any]:
    """Databricks-native tools to expose to the main agent (Phase 2)."""
    try:
        from .databricks_tools import build_default_databricks_tools

        return build_default_databricks_tools()
    except Exception:  # noqa: BLE001
        return []


def enrich_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject Manta's subagents, middleware, store, and tools into ``kwargs``.

    Mutates and returns ``kwargs`` (the dict passed to ``create_deep_agent``).
    Pure enough to unit-test directly: no global state beyond the registry it
    reads. Manta subagents replace any inherited subagent of the same name.
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

    store = build_store()
    if store is not None and kwargs.get("store") is None:
        kwargs["store"] = store

    extra_tools = build_databricks_tools()
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
