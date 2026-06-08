"""Runtime selection.

Keeps Deep Agents construction out of the pipeline and CLI: callers ask for a
runtime by mode and never import the adapter directly. The default is the
offline :class:`MockRuntime` so the CLI stays useful without credentials.
"""

from __future__ import annotations

from pathlib import Path

from .base import AgentRuntime
from .mock_runtime import MockRuntime


def get_runtime(*, dry_run: bool = True, root: Path | None = None) -> AgentRuntime:
    """Return the appropriate runtime for the requested mode.

    ``dry_run=True`` returns the mock runtime. ``dry_run=False`` returns the
    Deep Agents adapter; importing it here keeps the optional ``[agent]`` extra
    out of the default import path.
    """
    if dry_run:
        return MockRuntime()
    from .deepagents_adapter import DeepAgentsRuntime

    return DeepAgentsRuntime(root=root)
