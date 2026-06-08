from __future__ import annotations

from pathlib import Path

from .agents.mock_runtime import MockRuntime
from .budget import BudgetLedger
from .config import load_config, project_manta_dir
from .context_broker import ContextBroker
from .roles import default_roles
from .routing import HeuristicRouter
from .session import MantaSession


class MantaPipeline:
    def __init__(self, root: Path | None = None):
        self.root = root or Path.cwd()
        self.config = load_config(self.root)
        self.router = HeuristicRouter()
        self.context_broker = ContextBroker(self.root)
        self.roles = default_roles(self.config)
        self.runtime = MockRuntime()

    def dry_run(self, prompt: str, max_usd: float | None = None) -> dict:
        session = MantaSession(self.root)
        route = self.router.route(prompt)
        if max_usd is not None:
            route.max_budget_usd = min(max_usd, route.max_budget_usd)
        session.event("route_decision", route.model_dump(mode="json"))
        context = self.context_broker.build_manifest(session.session_id, route, prompt)
        session.event("context_manifest", context.model_dump(mode="json"))
        ledger = BudgetLedger(
            session_id=session.session_id,
            max_usd=route.max_budget_usd,
            path=project_manta_dir(self.root) / "ledger.jsonl",
        )
        role_results = []
        for role_name in route.pipeline:
            role = self.roles[role_name]
            session.event("role_started", {"role": role_name, "model": role.model})
            result = self.runtime.run_role(role, prompt, context)
            role_results.append(result.model_dump(mode="json"))
            session.event("role_completed", result.model_dump(mode="json"))
        session.event("session_completed", {"cost": ledger.summary(), "dry_run": True})
        return {
            "session_id": session.session_id,
            "route": route.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "roles": role_results,
            "cost": ledger.summary(),
        }
