"""Tests for the local usage ledger + cost model (Phase 4)."""

from __future__ import annotations

import time

import pytest

from manta_code.agents import usage as U


def test_price_for_prefers_longest_match():
    assert U.price_for("databricks-claude-opus-4-8").input == 15.0
    # Generic claude fallback when no specific variant matches.
    assert U.price_for("databricks-claude-3-x").input == 3.0
    assert U.price_for("unknown-endpoint") is None
    assert U.price_for(None) is None


def test_breakdown_splits_cache_and_uncached():
    bd = U.extract_breakdown(
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "input_token_details": {"cache_read": 600, "cache_creation": 100},
        }
    )
    assert bd.cache_available is True
    assert bd.cache_read == 600
    assert bd.cache_creation == 100
    assert bd.uncached_input == 300
    assert bd.total_tokens == 1200


def test_breakdown_without_cache_detail():
    bd = U.extract_breakdown({"input_tokens": 500, "output_tokens": 50})
    assert bd.cache_available is False
    assert bd.cache_read == 0
    assert bd.uncached_input == 500


def test_cost_prices_buckets_separately():
    price = U.Price(input=15.0, output=75.0, cache_read=1.5, cache_creation=18.75)
    bd = U.TokenBreakdown(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read=400_000,
        cache_creation=100_000,
        cache_available=True,
    )
    # uncached = 500k. cost = 0.5*15 + 0.4*1.5 + 0.1*18.75 + 1*75
    cost = bd.cost_usd(price)
    assert cost is not None
    assert abs(cost - (7.5 + 0.6 + 1.875 + 75.0)) < 1e-6


def test_cost_unknown_price_returns_none():
    assert U.TokenBreakdown(input_tokens=10).cost_usd(None) is None


def test_record_and_aggregate_by_agent(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(
        U.UsageRecord(agent="swe", model="databricks-gpt-oss-120b", input_tokens=1000,
                      output_tokens=200, cost_usd=0.01, scaffold_tokens=300, net_new_tokens=700),
        path=db,
    )
    U.record_usage(
        U.UsageRecord(agent="planning", model="databricks-claude-opus-4-8",
                      input_tokens=2000, output_tokens=500, cache_read=800,
                      cost_usd=0.50, scaffold_tokens=500, net_new_tokens=1500),
        path=db,
    )
    rows = U.aggregate(by="agent", path=db)
    assert [r.key for r in rows] == ["planning", "swe"]  # sorted by cost desc
    planning = rows[0]
    assert planning.calls == 1
    assert planning.cost_usd == 0.50
    assert abs(planning.cache_hit_rate - 800 / 2000) < 1e-9
    assert rows[1].cache_hit_rate is None  # swe had no cache data


def test_aggregate_by_model_and_unknown_cost(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(agent="a", model="mystery", input_tokens=10, cost_usd=None), path=db)
    rows = U.aggregate(by="model", path=db)
    assert rows[0].key == "mystery"
    assert rows[0].cost_known is False


def test_aggregate_invalid_dimension(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(agent="a", model="m"), path=db)
    try:
        U.aggregate(by="nonsense", path=db)
    except ValueError as exc:
        assert "group by" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_since_filters_old_rows(tmp_path):
    db = tmp_path / "usage.db"
    old = U.UsageRecord(agent="old", model="m", input_tokens=1, ts=time.time() - 10 * 86400)
    new = U.UsageRecord(agent="new", model="m", input_tokens=1, ts=time.time())
    U.record_usage(old, path=db)
    U.record_usage(new, path=db)
    rows = U.aggregate(by="agent", since=time.time() - 86400, path=db)
    assert {r.key for r in rows} == {"new"}


def test_scaffold_breakdown_and_ratio(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(agent="a", model="m", scaffold_tokens=400, net_new_tokens=200), path=db)
    U.record_usage(U.UsageRecord(agent="a", model="m", scaffold_tokens=200, net_new_tokens=200), path=db)
    sb = U.scaffold_breakdown(path=db)
    assert sb.scaffold_tokens == 600
    assert sb.net_new_tokens == 400
    assert abs(sb.overhead_ratio - 1.5) < 1e-9


def test_totals_sums_across_agents(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(agent="a", model="m", input_tokens=10, output_tokens=5, cost_usd=0.1), path=db)
    U.record_usage(U.UsageRecord(agent="b", model="m", input_tokens=20, output_tokens=5, cost_usd=0.2), path=db)
    total = U.totals(path=db)
    assert total.total_tokens == 40
    assert abs(total.cost_usd - 0.3) < 1e-9
    assert total.cost_known is True


def test_aggregate_empty_ledger(tmp_path):
    assert U.aggregate(by="agent", path=tmp_path / "empty.db") == []


# --- advice ledger + offline advisor (ADR 0010 Phase C) --------------------------


def test_advice_record_roundtrip(tmp_path):
    db = tmp_path / "U.db"
    U.record_advice(
        U.AdviceRecord(
            agent="swe", kind="escalate", severity="note", message="try opus",
            model="databricks-gpt-oss-120b", thread_id="t1", delivered="note",
        ),
        path=db,
    )
    (loaded,) = U.recent_advice(path=db)
    assert loaded.kind == "escalate"
    assert loaded.message == "try opus"
    assert loaded.accepted is None


def test_advise_flags_scaffold_overhead(tmp_path):
    db = tmp_path / "U.db"
    U.record_usage(
        U.UsageRecord(
            agent="orchestrator", model="databricks-gpt-oss-120b",
            input_tokens=10_000, output_tokens=500,
            scaffold_tokens=8_000, net_new_tokens=2_000,
        ),
        path=db,
    )
    recs = U.advise(path=db)
    assert any("Scaffolding" in r for r in recs)


def test_advise_flags_low_cache_hit(tmp_path):
    db = tmp_path / "U.db"
    U.record_usage(
        U.UsageRecord(
            agent="orchestrator", model="databricks-claude-opus-4-8",
            input_tokens=10_000, output_tokens=500,
            cache_read=100, cache_creation=5_000,
        ),
        path=db,
    )
    recs = U.advise(path=db)
    assert any("Cache-hit rate" in r for r in recs)


def test_advise_flags_premium_heavy_mix(tmp_path):
    db = tmp_path / "U.db"
    for _ in range(12):
        U.record_usage(
            U.UsageRecord(
                agent="orchestrator", model="databricks-claude-opus-4-8",
                input_tokens=1_000, output_tokens=100,
            ),
            path=db,
        )
    recs = U.advise(path=db)
    assert any("premium models" in r for r in recs)


def test_advise_quiet_on_healthy_ledger(tmp_path):
    db = tmp_path / "U.db"
    U.record_usage(
        U.UsageRecord(
            agent="orchestrator", model="databricks-gpt-oss-120b",
            input_tokens=1_000, output_tokens=400,
            scaffold_tokens=200, net_new_tokens=800,
        ),
        path=db,
    )
    assert U.advise(path=db) == []


def test_effective_pricing_merges_config_overrides(tmp_path, monkeypatch):
    # ADR 0010 Phase D: pricing is pluggable via [pricing] in .manta/config.toml.
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[pricing."my-finetune"]\ninput = 2.0\noutput = 8.0\n'
        '[pricing."claude-opus"]\ninput = 10.0\noutput = 50.0\n',
        encoding="utf-8",
    )
    U.clear_pricing_cache()
    try:
        assert U.price_for("databricks-my-finetune-v2").input == 2.0
        # Config overrides the built-in claude-opus rate.
        assert U.price_for("databricks-claude-opus-4-8").input == 10.0
        # Untouched built-ins survive the merge.
        assert U.price_for("databricks-gpt-5-4").input == 1.25
    finally:
        U.clear_pricing_cache()


def test_effective_pricing_skips_malformed_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[pricing."broken"]\nnonsense = true\n', encoding="utf-8"
    )
    U.clear_pricing_cache()
    try:
        assert U.price_for("broken-model") is None  # skipped, not fatal
        assert U.price_for("databricks-claude-opus-4-8") is not None
    finally:
        U.clear_pricing_cache()


def test_services_require_exact_prices_and_provenance():
    service = "system.ai.claude-opus-4-8"
    assert U.price_for(service) is None
    assert U.price_for("databricks:" + service) is None
    assert U.price_for("team.models.gpt-5-fallback") is None
    explicit = {
        "claude-opus": U.Price(99, 999),
        service: U.Price(2, 7),
        "backend-model-v1": U.Price(4, 12),
    }
    match = U.pricing_for("databricks:" + service, explicit, observed_backend="backend-model-v1")
    assert match.price == U.Price(2, 7)
    assert match.source == "override:exact:system.ai.claude-opus-4-8"
    assert match.version
    assert U.pricing_for("team.models.router", explicit, observed_backend="backend-model-v1").price == U.Price(4, 12)
    assert U.pricing_for("team.models.router", explicit, observed_backend="claude-opus-new").price is None
    # A deliberately empty override table must not silently restore defaults.
    assert U.price_for("databricks-gpt-5-4", {}) is None


def test_config_service_rates_are_exact_and_fingerprinted(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[pricing."system.ai.private-gpt-5"]\ninput = 2.0\noutput = 8.0\n', encoding="utf-8"
    )
    U.clear_pricing_cache()
    try:
        match = U.pricing_for("system.ai.private-gpt-5")
        assert match.price == U.Price(2, 8)
        assert match.source == "config:exact:system.ai.private-gpt-5"
        assert len(match.version) == 16
        assert U.price_for("system.ai.private-gpt-5-fallback") is None
    finally:
        U.clear_pricing_cache()


def test_missing_usage_and_unknown_cache_rates_do_not_become_zero_cost():
    assert U.extract_breakdown(None).usage_complete is False
    assert U.extract_breakdown({}).cost_usd(U.Price(1, 2)) is None
    assert U.extract_breakdown({"input_tokens": 12}).cost_usd(U.Price(1, 2)) is None
    assert U.extract_breakdown({"input_tokens": -1, "output_tokens": 3}).usage_complete is False
    assert U.extract_breakdown({"input_tokens": True, "output_tokens": 3}).usage_complete is False
    cached = U.extract_breakdown({
        "input_tokens": 100, "output_tokens": 20,
        "input_token_details": {"cache_read": 50},
    })
    # Cache rates cannot be derived from an unrelated vendor's economics.
    assert cached.cost_usd(U.Price(1, 2)) is None
    assert cached.cost_usd(U.Price(1, 2, cache_read=0.2)) == (50 + 10 + 40) / 1_000_000
    assert U.extract_breakdown({"input_tokens": 0, "output_tokens": 0}).cost_usd(U.Price(1, 2)) == 0


def test_reasoning_tokens_are_a_subset_of_output():
    detail = U.extract_breakdown({
        "input_tokens": 100, "output_tokens": 80,
        "output_token_details": {"reasoning": 60},
    })
    assert detail.reasoning_tokens == 60
    assert detail.total_tokens == 180
    assert detail.cost_usd(U.Price(1, 2)) == 260 / 1_000_000
    assert U.extract_breakdown({
        "input_tokens": 100, "output_tokens": 80,
        "output_token_details": {"reasoning": 90},
    }).usage_complete is False


def test_ledger_migration_preserves_historical_data_and_unknown_completeness(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(U._SCHEMA)
    conn.execute(
        "INSERT INTO usage (ts, agent, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), "old", "legacy-endpoint", 100, 20, 0.005),
    )
    conn.commit()
    conn.close()
    # Opening twice proves the additive migration is idempotent.
    U.connect(db).close()
    conn = U.connect(db)
    row = conn.execute("SELECT * FROM usage").fetchone()
    conn.close()
    assert (row["model"], row["input_tokens"], row["cost_usd"]) == ("legacy-endpoint", 100, 0.005)
    assert row["usage_complete"] is None
    assert row["observed_backend"] is None
    total = U.totals(path=db)
    assert total.cost_usd == 0.005
    assert total.priced_calls == 1
    assert total.cost_known is False
    assert total.missing_usage_calls == 1


def test_ledger_provenance_roundtrip_and_partial_totals(tmp_path):
    db = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(
        model="system.ai.private", requested_target="system.ai.private",
        requested_service="system.ai.private", observed_backend="backend-v1",
        surface="unified", protocol="responses", request_id="req-1",
        usage_complete=True, cache_available=False, reasoning_tokens=20,
        pricing_source="config:exact:system.ai.private", pricing_version="v1",
        input_tokens=10, output_tokens=30, cost_usd=0.05, thread_id="t-1",
    ), path=db)
    U.record_usage(U.UsageRecord(
        model="system.ai.other", usage_complete=False, cost_usd=None, thread_id="t-1",
    ), path=db)
    conn = U.connect(db)
    row = conn.execute("SELECT * FROM usage ORDER BY id LIMIT 1").fetchone()
    conn.close()
    assert row["observed_backend"] == "backend-v1"
    assert row["request_id"] == "req-1"
    assert row["reasoning_tokens"] == 20
    assert row["pricing_source"] == "config:exact:system.ai.private"
    total = U.totals(path=db)
    assert total.cost_usd == 0.05
    assert total.cost_known is False
    assert total.priced_calls == 1 and total.unknown_price_calls == 1
    assert total.missing_usage_calls == 1
    assert "1/2 calls priced" in total.completeness_label
    assert U.thread_totals("t-1", path=db)[0].missing_usage_calls == 1
    assert U.today_totals(path=db).cost_known is False
    assert U.receipts(path=db).estimated_savings_usd is None


def test_daily_budget_cache_respects_ledger_identity_and_record_day(tmp_path):
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    U.record_usage(U.UsageRecord(cost_usd=0.1), path=first)
    U.record_usage(U.UsageRecord(cost_usd=0.8), path=second)
    assert U.today_total_usd(path=first) == 0.1
    assert U.today_total_usd(path=second) == 0.8
    U.record_usage(U.UsageRecord(cost_usd=100, ts=time.time() - 86400 * 2), path=second)
    assert U.today_total_usd(path=second) == 0.8
    U.record_usage(U.UsageRecord(cost_usd=0.2), path=first)
    assert U.today_total_usd(path=second) == 0.8
    assert U.today_total_usd(path=first) == pytest.approx(0.3)
