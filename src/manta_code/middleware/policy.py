"""Fail-closed tool effects and canonical filesystem policy enforcement.

This is a tool boundary, not an operating-system sandbox. Constrained agents
cannot use shell or unclassified tools to step around their filesystem rules.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

READ_ONLY_DENIED_TOOLS = frozenset({"execute", "write_file", "edit_file", "delete", "run_job"})
_FS_TOOL_OPERATIONS: dict[str, Literal["read", "write"]] = {
    "read_file": "read", "ls": "read", "glob": "read", "grep": "read",
    "write_file": "write", "edit_file": "write", "delete": "write",
}
# These names are owned by the harness. External tools must declare trusted
# effects via metadata; names such as get_* are deliberately not guessed safe.
_READ_TOOLS = frozenset({
    "read_file", "ls", "glob", "grep", "write_todos", "ask_user", "ask_user_question",
    "compact_conversation", "update_goal", "manta_remember", "manta_task_status", "manta_task_output", "manta_task_list",
    "uc_list_catalogs", "uc_list_schemas", "uc_list_tables", "uc_describe_table",
    "uc_table_lineage", "sql_query", "list_jobs", "get_run_status", "get_run_diagnostics",
})
_DELEGATION_TOOLS = frozenset({"task", "manta_task_submit", "manta_task_send", "manta_task_cancel"})


def _glob_matches(path: str, pattern: str) -> bool:
    import wcmatch.glob as wcglob
    return wcglob.globmatch(path, pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR)


def tool_effect(tool_name: str, tool: Any = None) -> str:
    """Classify only built-ins or an explicit, trusted tool metadata contract.

    MCP annotations are hints from remote servers, not authorization. A tool
    integrator may attest ``metadata['manta_effect']='read'`` after review.
    """
    metadata = getattr(tool, "metadata", None) or {}
    if metadata.get("_deepagents_code_mcp_server"):
        # dcode flattens remote MCP annotations into metadata. They cannot
        # attest their own authority, even when using a built-in-looking name.
        return "unknown"
    if metadata.get("manta_effect") in {"read", "write", "execute", "delegate"}:
        return metadata["manta_effect"]
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _DELEGATION_TOOLS:
        return "delegate"
    if tool_name == "execute":
        return "execute"
    if tool_name in READ_ONLY_DENIED_TOOLS:
        return "write"
    if tool_name in {"js_eval", "code_interpreter"}:
        return "dispatch"
    return "unknown"


class ToolPolicyMiddleware(AgentMiddleware):
    def __init__(self, *, allow: list[str] | None = None, deny: list[str] | None = None,
                 read_only: bool = False, filesystem: list[Any] | None = None,
                 ask: list[str] | None = None, agent_name: str | None = None,
                 allow_delegation: bool = False, root: str | Path | None = None) -> None:
        super().__init__()
        self._allow = set(allow) if allow is not None else None
        self._deny = set(deny or ())
        self._ask = set(ask or ())
        self._read_only = read_only
        self._filesystem = list(filesystem or ())
        self._agent_name = agent_name
        self._allow_delegation = allow_delegation
        self._root = Path(root or Path.cwd()).resolve()
        self._backend: Any = None
        self._guarded_dispatch_names: set[str] = set()

    @property
    def name(self) -> str:
        return "Manta.ToolPolicy" + (f".{self._agent_name}" if self._agent_name else "")

    def bind_scope(self, backend: Any) -> None:
        self._backend = backend

    def _canonical_path(self, raw: str) -> str:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("missing or invalid filesystem path")
        backend = self._backend
        route = ""
        if backend is not None and hasattr(backend, "routes"):
            matches = [prefix for prefix in backend.routes if raw.startswith(prefix)]
            if matches:
                route = max(matches, key=len)
                backend = backend.routes[route]
                raw = "/" + raw[len(route):].lstrip("/")
            else:
                backend = backend.default
        if backend is not None:
            # FilesystemBackend (including LocalShellBackend) exposes cwd and
            # virtual_mode. Other backends require an explicit resolver contract.
            resolver = getattr(backend, "manta_canonical_path", None)
            if resolver is not None:
                return str(resolver(raw))
            if not hasattr(backend, "cwd"):
                raise ValueError("backend does not provide canonical filesystem scope")
            root = Path(backend.cwd).resolve()
            path = root / raw.lstrip("/") if getattr(backend, "virtual_mode", False) else Path(raw)
        else:
            root = self._root
            path = Path(raw)
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve(strict=False)
        if backend is not None and getattr(backend, "virtual_mode", False):
            # The host canonical scope is used for policy comparisons, including
            # symlink targets. A virtual path is never mistaken for a host root.
            return str(resolved)
        return str(resolved)

    def _fs_denial(self, tool_name: str, args: dict[str, Any]) -> str | None:
        if not self._filesystem or tool_name not in _FS_TOOL_OPERATIONS:
            return None
        operation = _FS_TOOL_OPERATIONS[tool_name]
        search = tool_name in {"glob", "grep", "ls"}
        raw = args.get("file_path") or args.get("path") or ("." if search else None)
        try:
            path = self._canonical_path(raw)
        except (ValueError, OSError, RuntimeError) as exc:
            return f"'{tool_name}' blocked by filesystem policy: {exc}."
        for rule in self._filesystem:
            if operation not in rule.operations:
                continue
            patterns = []
            for pattern in rule.paths:
                prefix = pattern
                for token in ("*", "?", "[", "{"):
                    prefix = prefix.split(token, 1)[0]
                # Canonicalize literal rule prefixes too, so a deny referring
                # to a symlink is enforced through every spelling of its target.
                if prefix:
                    resolved = str(Path(prefix).resolve(strict=False))
                    if prefix.endswith("/") and resolved != "/":
                        resolved += "/"
                    pattern = resolved + pattern[len(prefix):]
                patterns.append(pattern)
            if any(_glob_matches(path, p) for p in patterns):
                if rule.mode == "allow":
                    return None
                return f"'{tool_name}' on {path!r} is blocked by filesystem policy ({operation} denied)."
            if search and rule.mode == "deny":
                # A recursive search can touch denied descendants even when its
                # base path doesn't itself match the rule. Refuse that scope.
                for pattern in patterns:
                    prefix = pattern
                    for token in ("*", "?", "[", "{"):
                        prefix = prefix.split(token, 1)[0]
                    if not prefix or prefix.startswith(path.rstrip("/") + "/"):
                        return f"'{tool_name}' search scope intersects a denied filesystem policy path."
        # Allow rules define a positive scope for the operation. Deny-only rules
        # retain their usual default-allow behavior outside denied paths.
        if any(operation in r.operations and r.mode == "allow" for r in self._filesystem):
            return f"'{tool_name}' on {path!r} is outside this agent's allowed filesystem policy scope."
        return None

    def _denial_reason(self, tool_name: str, args: dict[str, Any], tool: Any = None) -> str | None:
        effect = tool_effect(tool_name, tool)
        if effect == "dispatch" and tool_name not in self._guarded_dispatch_names:
            effect = "unknown"
        delegated = effect == "delegate" and self._allow_delegation
        if self._read_only and effect not in {"read", "dispatch"} and not delegated:
            label = f"the read-only '{self._agent_name}' agent" if self._agent_name else "this read-only agent"
            return (f"'{tool_name}' is blocked: {label} cannot use tools with {effect} effects. "
                    "Use /agents to switch to a writable agent such as swe; otherwise describe the change.")
        if tool_name in self._deny:
            return f"'{tool_name}' is not permitted for this agent (deny-list)."
        if self._allow is not None and tool_name not in self._allow:
            return f"'{tool_name}' is not in this agent's allowed tools. Allowed: {', '.join(sorted(self._allow)) or '(none)'}."
        if self._filesystem and effect in {"execute", "unknown", "delegate"}:
            return f"'{tool_name}' cannot enforce this agent's canonical filesystem policy scope."
        denial = self._fs_denial(tool_name, args)
        return denial if denial is not None else self._ask_denial(tool_name)

    def _ask_denial(self, tool_name: str) -> str | None:
        if tool_name not in self._ask:
            return None
        from ..tasks.events import unattended_run
        if not unattended_run() or os.environ.get("MANTA_ALLOW_ASKS", "").strip() == "1":
            return None
        return (f"'{tool_name}' requires human approval and this is an unattended run "
                "(ask-gated tools fail closed). Resubmit with --allow-asks to pre-approve.")

    def _blocked(self, request: Any) -> ToolMessage | None:
        call = getattr(request, "tool_call", {})
        args = call.get("args") or {}
        reason = self._denial_reason(call.get("name", ""), args, getattr(request, "tool", None))
        if reason is None:
            return None
        return ToolMessage(content=f"Blocked by Manta tool policy: {reason}", tool_call_id=call.get("id", ""), status="error")

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else await handler(request)
