from __future__ import annotations

from manta_cli.roles import RoleSpec
from manta_cli.schemas import ContextManifest, RoleResult


class MockRuntime:
    """Dry-run runtime used before real model integration."""

    def run_role(self, role: RoleSpec, prompt: str, context: ContextManifest) -> RoleResult:
        return RoleResult(
            role=role.name,
            status="completed",
            output={
                "message": f"Dry-run completed for role {role.name}",
                "model": role.model,
                "selected_files": context.selected_files,
            },
            cost=0.0,
        )
