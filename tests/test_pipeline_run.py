from pathlib import Path

from manta_cli.pipeline import MantaPipeline
from manta_cli.roles import RoleSpec
from manta_cli.schemas import ContextManifest, RoleResult, TokenUsage


class _FakeRuntime:
    def __init__(self, usage: dict, status: str = "completed", block_role: str | None = None):
        self.usage = usage
        self.status = status
        self.block_role = block_role

    def run_role(self, role: RoleSpec, prompt: str, context: ContextManifest) -> RoleResult:
        status = "blocked" if role.name == self.block_role else self.status
        return RoleResult(role=role.name, status=status, usage=TokenUsage(**self.usage))


def test_run_records_usage_into_ledger(tmp_path: Path):
    runtime = _FakeRuntime({"input_tokens": 1000, "output_tokens": 1000})
    pipeline = MantaPipeline(root=tmp_path, runtime=runtime)

    result = pipeline.run("add a small fix")

    assert result["cost"]["used_usd"] > 0
    ledger = tmp_path / ".manta" / "ledger.jsonl"
    assert ledger.exists()
    assert ledger.read_text(encoding="utf-8").strip() != ""


def test_run_stops_when_budget_exceeded(tmp_path: Path):
    runtime = _FakeRuntime({"input_tokens": 10_000_000, "output_tokens": 10_000_000})
    pipeline = MantaPipeline(root=tmp_path, runtime=runtime)

    # Security route -> planner first; the huge usage blows the $5 cap immediately.
    result = pipeline.run("add JWT auth and update secrets handling")

    statuses = [r["status"] for r in result["roles"]]
    assert result["stopped_reason"] is not None
    assert "failed" in statuses
    assert "skipped" in statuses


def test_run_stops_when_reviewer_blocks(tmp_path: Path):
    runtime = _FakeRuntime({"input_tokens": 10, "output_tokens": 10}, block_role="code_reviewer")
    pipeline = MantaPipeline(root=tmp_path, runtime=runtime)

    result = pipeline.run("implement a new feature with tests")

    statuses = {r["role"]: r["status"] for r in result["roles"]}
    assert statuses["code_reviewer"] == "blocked"
    assert result["stopped_reason"]


def test_dry_run_does_not_record_cost(tmp_path: Path):
    pipeline = MantaPipeline(root=tmp_path)

    result = pipeline.dry_run("what does this function do?")

    assert result["cost"]["used_usd"] == 0
    assert not (tmp_path / ".manta" / "ledger.jsonl").exists()
