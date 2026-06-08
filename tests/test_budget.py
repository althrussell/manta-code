from pathlib import Path

from manta_cli.budget import BudgetExceeded, BudgetLedger, PriceTable
from manta_cli.schemas import ModelPrice


def test_budget_records_cost(tmp_path: Path):
    table = PriceTable({"test:model": ModelPrice(input_per_million=1, output_per_million=2, context_window=1000)})
    ledger = BudgetLedger("s1", 1.0, tmp_path / "ledger.jsonl", table)
    record = ledger.record("router", "test:model", 1000, 1000, "simple_answer")
    assert record.estimated_cost_usd == 0.003
    assert ledger.used_usd == 0.003
    assert (tmp_path / "ledger.jsonl").exists()


def test_budget_blocks_when_exceeded(tmp_path: Path):
    table = PriceTable({"test:model": ModelPrice(input_per_million=1_000_000, output_per_million=1_000_000, context_window=1000)})
    ledger = BudgetLedger("s1", 0.01, tmp_path / "ledger.jsonl", table)
    try:
        ledger.record("planner", "test:model", 1, 1, "complex_architecture")
    except BudgetExceeded:
        assert True
    else:
        assert False, "Expected BudgetExceeded"
