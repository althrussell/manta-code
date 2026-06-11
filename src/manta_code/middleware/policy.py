"""Tool-level policy enforcement: allow/deny lists, read-only, and path rules.

``deepagents`` can enforce *filesystem* read/write via ``FilesystemPermission``
natively — but only on backends that do **not** provide command execution. The
``deepagents-code`` runtime uses a sandbox backend with an ``execute`` tool, and
``FilesystemMiddleware`` refuses to accept permissions on such a backend
(``NotImplementedError`` at construction time). Compiling ``FilesystemPermission``
onto Manta subagents therefore crashed the agent server at start.

So Manta enforces *all* of an agent's declared boundary here, in a
``wrap_tool_call`` middleware that rejects a disallowed call *before it runs*,
returning an error ``ToolMessage`` the model can read and react to:

- allow / deny tool lists and ``read_only`` (covers ``execute`` and arbitrary
  MCP/custom tools that filesystem permissions never could), and
- per-path filesystem rules (``AgentDef.filesystem``) evaluated against the
  ``file_path`` argument of the filesystem tools, mirroring deepagents'
  first-match-wins glob semantics.

This is the whole of "enforced, not prompted" for Manta agents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

#: Tools that mutate state. A ``read_only`` agent denies all of these so it
#: cannot edit files or run shell commands even if the orchestrator handed those
#: tools down.
READ_ONLY_DENIED_TOOLS: frozenset[str] = frozenset(
    {"execute", "write_file", "edit_file"}
)

#: Filesystem tools whose ``file_path`` argument is checked against per-path
#: rules, mapped to the operation they perform.
_FS_TOOL_OPERATIONS: dict[str, Literal["read", "write"]] = {
    "read_file": "read",
    "ls": "read",
    "glob": "read",
    "write_file": "write",
    "edit_file": "write",
}


def _glob_matches(path: str, pattern: str) -> bool:
    """Match ``path`` against a deepagents-style glob ``pattern``.

    Uses ``wcmatch`` with the same flags deepagents' ``FilesystemMiddleware``
    uses (``BRACE | GLOBSTAR``) so Manta's path enforcement is consistent with
    the SDK's. Falls back to ``fnmatch`` if ``wcmatch`` is unavailable.
    """
    try:
        import wcmatch.glob as wcglob

        return wcglob.globmatch(
            path, pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR
        )
    except Exception:  # noqa: BLE001 - matcher fallback, never crash a tool call
        from fnmatch import fnmatch

        return fnmatch(path, pattern)


class ToolPolicyMiddleware(AgentMiddleware):
    """Reject tool calls that violate an agent's allow/deny/filesystem policy.

    Resolution order for a tool call:

    1. If ``read_only`` and the tool is in :data:`READ_ONLY_DENIED_TOOLS` -> deny.
    2. If it is in ``deny`` -> deny.
    3. If an ``allow`` list is set and the tool is not in it -> deny.
    4. If it is a filesystem tool and a ``filesystem`` rule denies its
       ``file_path`` -> deny.
    5. Otherwise -> allow (delegate to the handler).
    """

    def __init__(
        self,
        *,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        read_only: bool = False,
        filesystem: list[Any] | None = None,
        ask: list[str] | None = None,
        agent_name: str | None = None,
    ) -> None:
        super().__init__()
        self._allow = set(allow) if allow is not None else None
        self._deny = set(deny or ())
        self._ask = set(ask or ())
        self._read_only = read_only
        #: Each rule has ``.operations`` (``["read"|"write"]``), ``.paths``
        #: (list of globs) and ``.mode`` (``"allow"|"deny"``). Stored in order;
        #: first matching rule wins, like deepagents' ``_check_fs_permission``.
        self._filesystem = list(filesystem or ())
        self._agent_name = agent_name

    @property
    def name(self) -> str:
        suffix = f".{self._agent_name}" if self._agent_name else ""
        return f"Manta.ToolPolicy{suffix}"

    def _fs_denial(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Deny a filesystem tool call whose path matches a ``deny`` rule."""
        if not self._filesystem:
            return None
        operation = _FS_TOOL_OPERATIONS.get(tool_name)
        if operation is None:
            return None
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str) or not path:
            return None
        for rule in self._filesystem:
            if operation not in getattr(rule, "operations", ()):
                continue
            if any(_glob_matches(path, p) for p in getattr(rule, "paths", ())):
                if getattr(rule, "mode", "allow") == "deny":
                    return (
                        f"'{tool_name}' on {path!r} is blocked by this agent's "
                        f"filesystem policy ({operation} denied)."
                    )
                return None  # first match is an allow -> permitted
        return None

    def _denial_reason(self, tool_name: str, args: dict[str, Any]) -> str | None:
        if self._read_only and tool_name in READ_ONLY_DENIED_TOOLS:
            label = (
                f"the read-only '{self._agent_name}' agent"
                if self._agent_name
                else "this read-only agent"
            )
            return (
                f"'{tool_name}' is blocked: {label} may not modify files or run "
                "state-changing commands. To make changes, tell the user to switch "
                "to a writable agent (the base agent, or `swe`) via the /agents "
                "picker — switching restarts the session. Otherwise, describe the "
                "change instead of applying it."
            )
        if tool_name in self._deny:
            return f"'{tool_name}' is not permitted for this agent (deny-list)."
        if self._allow is not None and tool_name not in self._allow:
            allowed = ", ".join(sorted(self._allow)) or "(none)"
            return (
                f"'{tool_name}' is not in this agent's allowed tools. "
                f"Allowed: {allowed}."
            )
        fs = self._fs_denial(tool_name, args)
        if fs is not None:
            return fs
        # ASK tier (ADR 0011), evaluated last so a human is never prompted for
        # a call another rule denies anyway. Interactive sessions never reach
        # a denial here — upstream HITL (interrupt_on) owns the prompt; this
        # is the unattended fail-closed half.
        return self._ask_denial(tool_name)

    def _ask_denial(self, tool_name: str) -> str | None:
        """Deny ask-gated tools in unattended runs (fail closed).

        Upstream HITL auto-approves when no human is present; ask-gated tools
        must not run on that basis. ``MANTA_ALLOW_ASKS=1`` (set when a human
        submitted the task with ``--allow-asks``) grants blanket
        pre-approval, audited as ``auto_approved`` by the event layer.
        """
        if tool_name not in self._ask:
            return None
        try:
            from ..tasks.events import unattended_run

            if not unattended_run():
                return None  # interactive: interrupt_on owns the prompt
        except Exception:  # noqa: BLE001 - fail closed if detection breaks
            pass
        import os

        if os.environ.get("MANTA_ALLOW_ASKS", "").strip() == "1":
            return None  # human pre-approved at submission
        label = f" for agent '{self._agent_name}'" if self._agent_name else ""
        return (
            f"'{tool_name}' requires human approval{label} and this is an "
            "unattended run (ask-gated tools fail closed). A human can "
            "pre-approve by resubmitting the task with --allow-asks."
        )

    def _tool_call(self, request: Any) -> dict[str, Any]:
        call = getattr(request, "tool_call", None)
        return call if isinstance(call, dict) else {}

    def _tool_name(self, request: Any) -> str:
        return self._tool_call(request).get("name", "")

    def _tool_args(self, request: Any) -> dict[str, Any]:
        args = self._tool_call(request).get("args")
        return args if isinstance(args, dict) else {}

    def _tool_call_id(self, request: Any) -> str:
        return self._tool_call(request).get("id", "")

    def _blocked(self, request: Any) -> ToolMessage | None:
        reason = self._denial_reason(self._tool_name(request), self._tool_args(request))
        if reason is None:
            return None
        return ToolMessage(
            content=f"Blocked by Manta tool policy: {reason}",
            tool_call_id=self._tool_call_id(request),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else await handler(request)
