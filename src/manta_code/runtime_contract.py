"""Read-only health of required runtime adapter seams and compiled controls."""
from __future__ import annotations

import importlib
from typing import Any


_REQUIRED_SYMBOLS = (
    ("deepagents_code.client.launch.server", "generate_langgraph_json", "owned server graph bootstrap"),
    ("deepagents_code.agent", "create_deep_agent", "build hook adapter"),
    ("deepagents_code.agent", "AsyncApprovalHITLMiddleware", "async approval adapter"),
    ("deepagents_code.client.remote_client", "RemoteAgent.aensure_thread", "durable task thread registration"),
    ("deepagents_code.client.remote_client", "RemoteAgent.aget_state", "durable task checkpoint validation"),
    ("langgraph.prebuilt.tool_node", "ToolNode._execute_tool_async", "programmatic async dispatch"),
    ("langgraph.prebuilt.tool_node", "ToolNode._execute_tool_sync", "programmatic sync dispatch"),
    ("langchain.agents.middleware.human_in_the_loop", "HumanInTheLoopMiddleware._resolve_per_call", "programmatic approval adapter"),
)


def runtime_health() -> list[dict[str, Any]]:
    """Separate adapter availability from validation of a running graph.

    A doctor process may not have built a graph; that is explicitly reported
    as pending, never represented as a validated effective capability list.
    """
    rows: list[dict[str, Any]] = []
    for module, attribute, label in _REQUIRED_SYMBOLS:
        try:
            value = importlib.import_module(module)
            for part in attribute.split("."):
                value = getattr(value, part)
            ok = callable(value)
            detail = f"available: {module}.{attribute}"
        except (ImportError, AttributeError) as exc:
            ok = False
            detail = f"unavailable: {module}.{attribute} ({type(exc).__name__})"
        rows.append({"name": label, "required": True, "ok": ok, "detail": detail})
    from .runtime import last_effective_manifest
    manifest = last_effective_manifest()
    detail = "pending: this process has no validated current graph"
    if manifest:
        detail = "actual primary and inspectable inline tool controls validated"
        omitted = manifest.get("unvalidated_subagents") or []
        if omitted:
            detail += f"; {len(omitted)} opaque/precompiled children unvalidated"
    rows.append({"name": "compiled control manifest", "required": manifest is not None,
                 "ok": bool(manifest and manifest.get("validated")),
                 "detail": detail})
    return rows
