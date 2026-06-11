"""Tests for the local usage ledger + cost model (Phase 4)."""

from __future__ import annotations

import time

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
    price = U.Price(input=15.0, output=75.0)  # cache_read=1.5, cache_write=18.75
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
