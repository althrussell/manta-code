from __future__ import annotations

from pathlib import Path

from .agents.base import AgentRuntime
from .agents.factory import get_runtime
from .budget import BudgetExceeded, BudgetLedger
from .config import load_config, project_manta_dir
from .context_broker import ContextBroker
from .roles import default_roles
from .routing import HeuristicRouter
from .schemas import RoleResult
from .session import MantaSession


class MantaPipeline:
    def __init__(self, root: Path | None = None, runtime: AgentRuntime | None = None):
        self.root = root or Path.cwd()
        self.config = load_config(self.root)
        self.router = HeuristicRouter()
        self.context_broker = ContextBroker(self.root)
        self.roles = default_roles(self.config)
        self._injected_runtime = runtime

    def dry_run(self, prompt: str, max_usd: float | None = None) -> dict:
        runtime = self._injected_runtime or get_runtime(dry_run=True, root=self.root)
        return self._execute(prompt, max_usd, runtime, dry_run=True)

    def run(self, prompt: str, max_usd: float | None = None) -> dict:
        """Execute the pipeline with a real model-backed runtime.

        Records token usage into the budget ledger after every role and stops
        when a hard cap would be exceeded.
        """
        runtime = self._injected_runtime or get_runtime(dry_run=False, root=self.root)
        return self._execute(prompt, max_usd, runtime, dry_run=False)

    def _execute(self, prompt: str, max_usd: float | None, runtime: AgentRuntime, *, dry_run: bool) -> dict:
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

        role_results: list[dict] = []
        stopped_reason: str | None = None
        for role_name in route.pipeline:
            role = self.roles[role_name]
            if stopped_reason is not None:
                role_results.append(
                    RoleResult(role=role_name, status="skipped", output={"reason": stopped_reason}).model_dump(
                        mode="json"
                    )
                )
                continue

            session.event("role_started", {"role": role_name, "model": role.model})
            result = runtime.run_role(role, prompt, context)

            if result.usage is not None and not dry_run:
                try:
                    record = ledger.record(
                        role=role_name,
                        model=role.model,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        route=route.route.value,
                        reason=f"{role_name} call",
                    )
                    result.cost = record.estimated_cost_usd
                    session.event("cost_recorded", record.model_dump(mode="json"))
                except BudgetExceeded as exc:
                    stopped_reason = str(exc)
                    result.status = "failed"
                    session.event("budget_exceeded", {"role": role_name, "detail": stopped_reason})

            role_results.append(result.model_dump(mode="json"))
            session.event("role_completed", result.model_dump(mode="json"))

            if result.status == "blocked":
                stopped_reason = f"{role_name} blocked the pipeline"

        session.event(
            "session_completed",
            {"cost": ledger.summary(), "dry_run": dry_run, "stopped_reason": stopped_reason},
        )
        return {
            "session_id": session.session_id,
            "route": route.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "roles": role_results,
            "cost": ledger.summary(),
            "stopped_reason": stopped_reason,
        }
