"""Tool-level policy enforcement: allow/deny lists and read-only agents.

The ``deepagents`` SDK enforces *filesystem* read/write via
``FilesystemPermission`` natively, but that does not cover the ``execute`` shell
tool (on a shell backend) or arbitrary MCP/custom tools. To make a Manta agent's
declared boundary real — "this reviewer cannot run anything that mutates state"
— we add a ``wrap_tool_call`` middleware that rejects disallowed tool calls
*before they run*, returning an error ``ToolMessage`` the model can read and
react to (rather than silently dropping the call or raising).

This is the second half of "enforced, not prompted" permissions; the first half
(filesystem allow/deny) is compiled into the subagent's ``permissions`` by
:mod:`manta_code.agents.factory`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

#: Tools that mutate state. A ``read_only`` agent denies all of these on top of
#: its filesystem write-deny rule, so it cannot edit files or run shell commands
#: even if the orchestrator handed those tools down.
READ_ONLY_DENIED_TOOLS: frozenset[str] = frozenset(
    {"execute", "write_file", "edit_file"}
)


class ToolPolicyMiddleware(AgentMiddleware):
    """Reject tool calls that violate an agent's allow/deny policy.

    Resolution order for a tool name:

    1. If ``read_only`` and the tool is in :data:`READ_ONLY_DENIED_TOOLS` -> deny.
    2. If it is in ``deny`` -> deny.
    3. If an ``allow`` list is set and the tool is not in it -> deny.
    4. Otherwise -> allow (delegate to the handler).
    """

    def __init__(
        self,
        *,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        read_only: bool = False,
        agent_name: str | None = None,
    ) -> None:
        super().__init__()
        self._allow = set(allow) if allow is not None else None
        self._deny = set(deny or ())
        self._read_only = read_only
        self._agent_name = agent_name

    @property
    def name(self) -> str:
        suffix = f".{self._agent_name}" if self._agent_name else ""
        return f"Manta.ToolPolicy{suffix}"

    def _denial_reason(self, tool_name: str) -> str | None:
        if self._read_only and tool_name in READ_ONLY_DENIED_TOOLS:
            return (
                f"'{tool_name}' is blocked: this agent is read-only and may not "
                "modify files or run state-changing commands. Describe the change "
                "instead of applying it."
            )
        if tool_name in self._deny:
            return f"'{tool_name}' is not permitted for this agent (deny-list)."
        if self._allow is not None and tool_name not in self._allow:
            allowed = ", ".join(sorted(self._allow)) or "(none)"
            return (
                f"'{tool_name}' is not in this agent's allowed tools. "
                f"Allowed: {allowed}."
            )
        return None

    def _tool_name(self, request: Any) -> str:
        call = getattr(request, "tool_call", None) or {}
        return call.get("name", "") if isinstance(call, dict) else ""

    def _tool_call_id(self, request: Any) -> str:
        call = getattr(request, "tool_call", None) or {}
        return call.get("id", "") if isinstance(call, dict) else ""

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        tool_name = self._tool_name(request)
        reason = self._denial_reason(tool_name)
        if reason is not None:
            return ToolMessage(
                content=f"Blocked by Manta tool policy: {reason}",
                tool_call_id=self._tool_call_id(request),
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        tool_name = self._tool_name(request)
        reason = self._denial_reason(tool_name)
        if reason is not None:
            return ToolMessage(
                content=f"Blocked by Manta tool policy: {reason}",
                tool_call_id=self._tool_call_id(request),
                status="error",
            )
        return await handler(request)
