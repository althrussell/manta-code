"""Compile a Manta :class:`AgentDef` into a ``deepagents`` ``SubAgent`` dict.

This is where a Manta agent definition becomes something the runtime can
*enforce*. The upstream markdown loader produces a ``SubAgent`` with only
``name`` / ``description`` / ``system_prompt`` / ``model``; the factory fills in
the fields the SDK actually acts on:

- ``permissions`` — ``FilesystemPermission`` rules so read-only is a real
  filesystem boundary (read-only agents get a deny-all-writes rule).
- ``middleware`` — a :class:`~manta_code.middleware.policy.ToolPolicyMiddleware`
  that rejects disallowed / mutating tool calls (covers ``execute`` and custom
  tools, which filesystem permissions cannot).
- ``interrupt_on`` — per-tool human-in-the-loop gating from the agent's
  ``approval`` list.
- ``model`` — the pinned ``provider:endpoint``.

Heavy ``deepagents`` imports happen inside :func:`compile_subagent`, so the
registry/CLI stay importable without the ``agent`` extra; the factory is only
called from the build hook, which runs inside the agent server where the extra
is present.
"""

from __future__ import annotations

from typing import Any

from .registry import AgentDef, FsRule


def _filesystem_permissions(defn: AgentDef) -> list[Any]:
    """Build the ordered ``FilesystemPermission`` rules for a definition.

    Order matters (first match wins in ``FilesystemMiddleware``):

    1. ``read_only`` -> deny all writes first, so nothing can re-enable them.
    2. The definition's explicit ``filesystem`` rules.

    Reads with no matching rule fall through to the SDK default (allow).
    """
    from deepagents.middleware.filesystem import FilesystemPermission

    rules: list[FsRule] = []
    if defn.read_only:
        rules.append(FsRule(operations=["write"], paths=["/**"], mode="deny"))
    rules.extend(defn.filesystem)
    return [
        FilesystemPermission(
            operations=list(rule.operations),
            paths=list(rule.paths),
            mode=rule.mode,
        )
        for rule in rules
    ]


def _tool_policy_middleware(defn: AgentDef) -> list[Any]:
    """Return a one-element ``[ToolPolicyMiddleware]`` when any policy applies."""
    needs_policy = (
        defn.read_only
        or defn.tools_allow is not None
        or bool(defn.tools_deny)
    )
    if not needs_policy:
        return []
    from ..middleware.policy import ToolPolicyMiddleware

    return [
        ToolPolicyMiddleware(
            allow=defn.tools_allow,
            deny=defn.tools_deny,
            read_only=defn.read_only,
            agent_name=defn.name,
        )
    ]


def _interrupt_on(defn: AgentDef) -> dict[str, Any]:
    """Map the ``approval`` tool list to a deepagents ``interrupt_on`` dict."""
    return {tool: True for tool in defn.approval}


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

    permissions = _filesystem_permissions(defn)
    if permissions:
        subagent["permissions"] = permissions

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

    return subagent


def compile_subagents(
    defs: list[AgentDef],
) -> list[dict[str, Any]]:
    """Compile many definitions, preserving order."""
    return [compile_subagent(defn) for defn in defs]
