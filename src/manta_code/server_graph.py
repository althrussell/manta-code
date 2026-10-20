"""Owned server entry point: required Manta controls before every provider."""
from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any

from deepagents_code._cli_context import CLIContextSchema
from langgraph_sdk.runtime import ServerRuntime as LangGraphServerRuntime

_GRAPH_REF = "manta_code.server_graph:make_graph"
_UPSTREAM_GRAPH_REF = "deepagents_code.server_graph:make_graph"


def install_server_bootstrap() -> bool:
    """Preserve upstream server services while selecting Manta's graph factory."""
    from deepagents_code.client.launch import server
    original = server.generate_langgraph_json
    if getattr(original, "__manta_server_bootstrap__", False):
        return True

    @functools.wraps(original)
    def generate(*args: Any, **kwargs: Any) -> Path:
        output = original(*args, **kwargs)
        if kwargs.get("graph_ref", _UPSTREAM_GRAPH_REF) == _UPSTREAM_GRAPH_REF:
            content = json.loads(output.read_text(encoding="utf-8"))
            content["graphs"]["agent"] = _GRAPH_REF
            output.write_text(json.dumps(content, indent=2), encoding="utf-8")
        return output

    generate.__manta_server_bootstrap__ = True
    server.generate_langgraph_json = generate
    return True


def install_server_controls() -> None:
    from .accounting import install_usage_collector
    from .budget_runtime import install_budget_runtime
    from .hook import install_build_hook
    from .model_capabilities import install_model_capability_bridge
    for name, installer in (("accounting", install_usage_collector),
                            ("budget checkpoints", install_budget_runtime),
                            ("model capabilities", install_model_capability_bridge),
                            ("agent control plane", install_build_hook)):
        if not installer():
            raise RuntimeError(f"Required Manta server control unavailable: {name}. Run manta doctor.")


async def make_graph(config: dict[str, Any] | None = None,
                     runtime: LangGraphServerRuntime[CLIContextSchema] | None = None) -> Any:
    await asyncio.to_thread(install_server_controls)
    from deepagents_code.server_graph import make_graph as upstream
    return await upstream(config=config, runtime=runtime)
