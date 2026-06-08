from __future__ import annotations

from typing import Protocol

from manta_cli.roles import RoleSpec
from manta_cli.schemas import ContextManifest, RoleResult


class AgentRuntime(Protocol):
    def run_role(self, role: RoleSpec, prompt: str, context: ContextManifest) -> RoleResult:
        ...
