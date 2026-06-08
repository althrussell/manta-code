from __future__ import annotations

from manta_cli.roles import RoleSpec
from manta_cli.schemas import ContextManifest, RoleResult


class DeepAgentsRuntime:
    """Adapter boundary for the real Deep Agents implementation.

    This file is intentionally thin in the bootstrap repo. Sprint 3 should wire
    the current Deep Agents SDK here, mapping Manta RoleSpec -> Deep Agents
    subagent config and wrapping all side-effecting tools in Manta policy.
    """

    def __init__(self) -> None:
        try:
            import deepagents  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Deep Agents is not installed. Install with: pip install -e '.[agent]'"
            ) from exc

    def run_role(self, role: RoleSpec, prompt: str, context: ContextManifest) -> RoleResult:
        # TODO Sprint 3:
        # - create Deep Agent with model=role.model
        # - map role.tools to policy-wrapped tools
        # - map role.skills to skills paths
        # - map role permissions
        # - request structured output for reviewer/security roles
        # - capture token usage into BudgetLedger
        raise NotImplementedError("DeepAgentsRuntime wiring is planned for Sprint 3.")
