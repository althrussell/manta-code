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
    "manta_task_cancel",
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
            agent_name=defn.name,
        )
    ]


def _interrupt_on(defn: AgentDef) -> dict[str, Any]:
    """Map the ``approval`` tool list to a deepagents ``interrupt_on`` dict."""
    return {tool: True for tool in defn.approval}


def _manta_tools(defn: AgentDef) -> list[Any]:
    """Resolve the agent's ``manta_tools`` groups to LangChain tools.

    Currently one group: ``"tasks"`` (background-task submit/status/output/
    list/cancel — the chief-of-staff surface). Guarded so an unavailable
    group never blocks agent construction.
    """
    tools: list[Any] = []
    if "tasks" in (defn.manta_tools or []):
        try:
            from ..tasks.tools import build_task_tools

            tools.extend(build_task_tools())
        except Exception:  # noqa: BLE001 - optional; never block construction
            pass
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
    }
    if defn.model:
        subagent["model"] = defn.model

    middleware = _tool_policy_middleware(defn)
    if extra_middleware:
        middleware.extend(extra_middleware)
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

    return subagent


def compile_subagents(
    defs: list[AgentDef],
) -> list[dict[str, Any]]:
    """Compile many definitions, preserving order."""
    return [compile_subagent(defn) for defn in defs]
