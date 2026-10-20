"""Tests for the trust-first token economy middleware (Phase 4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("langchain.agents.middleware.types")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from manta_code.agents import usage as U  # noqa: E402
from manta_code.middleware import economy as E  # noqa: E402


@dataclass
class _Runtime:
    thread_id: str = "t-1"
    store: Any = None
    config: dict = field(default_factory=dict)


@dataclass
class _Request:
    """A minimal stand-in for langchain's ModelRequest."""

    model: Any = "databricks-gpt-oss-120b"
    messages: list = field(default_factory=list)
    system_message: Any = None
    tools: list = field(default_factory=list)
    runtime: _Runtime = field(default_factory=_Runtime)

    def override(self, **kwargs):
        new = _Request(**{**self.__dict__, **kwargs})
        return new


@dataclass
class _Response:
    result: list


def _ai(usage):
    usage = dict(usage)
    usage.setdefault(
        "total_tokens", int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    )
    return AIMessage(content="ok", usage_metadata=usage)


def test_model_name_extraction():
    assert E._model_name("foo") == "foo"

    class M:
        model = "databricks-claude-opus-4-8"

    assert E._model_name(M()) == "databricks-claude-opus-4-8"
    assert E._model_name(None) == ""


def test_estimate_scaffolding_counts_system_and_messages():
    req = _Request(
        system_message=SystemMessage(content="You are a very capable agent. " * 20),
        messages=[HumanMessage(content="hello there")],
    )
    scaffold, net_new = E.estimate_scaffolding(req)
    assert scaffold > 0
    assert net_new > 0
    assert scaffold > net_new  # big system prompt dwarfs a short message


def test_wrap_model_call_records_to_ledger(tmp_path):
    db = tmp_path / "usage.db"
    mw = E.TokenEconomyMiddleware(
        agent="swe", ledger_path=db,
        pricing={"databricks-gpt-oss-120b": U.Price(0.15, 0.60, cache_read=0.01)},
    )

    usage_meta = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "input_token_details": {"cache_read": 400, "cache_creation": 0},
    }
    req = _Request(messages=[HumanMessage(content="do it")])

    def handler(r):
        return _Response(result=[_ai(usage_meta)])

    mw.wrap_model_call(req, handler)

    rows = U.aggregate(by="agent", path=db)
    assert len(rows) == 1
    assert rows[0].key == "swe"
    assert rows[0].input_tokens == 1000
    assert rows[0].output_tokens == 200
    assert rows[0].cache_read == 400
    assert rows[0].cost_usd > 0  # gpt-oss-120b is priced


def test_budget_pause_invoked_when_over_cap(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    # Tiny token cap so the first call trips it.
    mw = E.TokenEconomyMiddleware(agent="swe", max_tokens=100, ledger_path=db)

    interrupts: list = []

    def fake_interrupt(payload):
        interrupts.append(payload)

    # Patch the symbol the middleware imports lazily.
    import langgraph.types as lt

    monkeypatch.setattr(lt, "interrupt", fake_interrupt, raising=False)

    usage_meta = {"input_tokens": 500, "output_tokens": 50}
    req = _Request(messages=[HumanMessage(content="x")])

    def handler(r):
        return _Response(result=[_ai(usage_meta)])

    # First call: not yet over (running starts at 0), records 550 tokens.
    mw.wrap_model_call(req, handler)
    assert interrupts == []
    # Second call: running (550) >= cap (100) -> pause requested once.
    mw.wrap_model_call(req, handler)
    assert len(interrupts) == 1
    # HITLRequest shape (ADR 0011): the only payload upstream renders.
    (action,) = interrupts[0]["action_requests"]
    assert action["name"] == "manta_budget_continue"
    (review,) = interrupts[0]["review_configs"]
    assert review["allowed_decisions"] == ["approve", "reject"]
    # Third call: already approved -> not asked again.
    mw.wrap_model_call(req, handler)
    assert len(interrupts) == 1


def test_no_budget_never_pauses(tmp_path):
    mw = E.TokenEconomyMiddleware(agent="swe", ledger_path=tmp_path / "u.db")
    assert mw.has_budget is False
    mw._maybe_pause("t-1")  # must be a no-op, no exception


def test_async_wrap_records(tmp_path):
    db = tmp_path / "usage.db"
    mw = E.TokenEconomyMiddleware(agent="planning", ledger_path=db)
    req = _Request(model="databricks-claude-opus-4-8", messages=[HumanMessage(content="plan")])

    async def handler(r):
        return _Response(result=[_ai({"input_tokens": 100, "output_tokens": 10})])

    asyncio.run(mw.awrap_model_call(req, handler))
    rows = U.aggregate(by="agent", path=db)
    assert rows[0].key == "planning"


def test_agent_budget_middleware_reads_caps():
    @dataclass
    class _Defn:
        name: str = "guarded"
        budget_max_tokens: int = 5000
        budget_max_usd: float | None = None

    mw = E.agent_budget_middleware(_Defn())
    assert mw is not None
    assert mw.has_budget is True
    assert mw._agent == "guarded"


def test_orchestrator_middleware_factory():
    mws = E.orchestrator_middleware()
    assert len(mws) == 1
    assert mws[0]._agent == "orchestrator"
    assert mws[0].has_budget is False


def test_account_attributes_background_task_id(tmp_path, monkeypatch):
    # ADR 0010 Phase C: spend inside a background task is drillable per task.
    monkeypatch.setenv("MANTA_TASK_ID", "task7777")
    ledger = tmp_path / "usage.db"
    mw = E.TokenEconomyMiddleware(agent="swe", ledger_path=ledger)
    request = _Request()
    response = _Response(result=[_ai({"input_tokens": 100, "output_tokens": 50})])
    mw.wrap_model_call(request, lambda r: response)

    import sqlite3

    conn = sqlite3.connect(ledger)
    (task,) = conn.execute("SELECT task FROM usage").fetchone()
    conn.close()
    assert task == "task7777"


def test_budget_pause_graph_interrupt_propagates(tmp_path, monkeypatch):
    # The budget governor pauses via interrupt(), which raises GraphInterrupt;
    # it must propagate to the runtime, not be swallowed (review finding).
    from langgraph.errors import GraphInterrupt

    import langgraph.types as lg_types

    def _raising_interrupt(payload):
        raise GraphInterrupt()

    monkeypatch.setattr(lg_types, "interrupt", _raising_interrupt)
    mw = E.TokenEconomyMiddleware(
        agent="swe", max_tokens=10, ledger_path=tmp_path / "usage.db"
    )
    req = _Request()
    mw.wrap_model_call(req, lambda r: _Response(result=[_ai({"input_tokens": 100, "output_tokens": 50})]))
    with pytest.raises(GraphInterrupt):
        mw.wrap_model_call(req, lambda r: _Response(result=[_ai({"input_tokens": 1, "output_tokens": 1})]))


def test_thread_id_falls_back_to_langgraph_config(monkeypatch):
    # The real runtime exposes no thread attribute; the live RunnableConfig's
    # contextvar is the only source. Without this every ledger row carried an
    # empty thread_id (found while building in-session /cost).
    import langgraph.config as lg_config

    monkeypatch.setattr(
        lg_config, "get_config", lambda: {"configurable": {"thread_id": "thr-real"}}
    )
    assert E._thread_id(_Request(runtime=_Runtime(thread_id=""))) == "thr-real"


def test_unity_call_records_requested_identity_and_observed_backend(tmp_path):
    from types import SimpleNamespace

    model = SimpleNamespace(
        model_name="system.ai.router",
        target_policy=SimpleNamespace(
            target=SimpleNamespace(name="system.ai.router", kind="service"),
            surface="unified", api="responses",
        ),
    )
    mw = E.TokenEconomyMiddleware(
        ledger_path=tmp_path / "usage.db",
        pricing={"system.ai.router": U.Price(1, 2), "actual-backend": U.Price(9, 10)},
    )
    message = _ai({"input_tokens": 100, "output_tokens": 20,
                   "output_token_details": {"reasoning": 10}})
    message.response_metadata = {"model_name": "actual-backend", "request_id": "req-123"}
    mw.wrap_model_call(_Request(model=model), lambda r: _Response([message]))
    conn = U.connect(tmp_path / "usage.db")
    row = conn.execute("SELECT * FROM usage").fetchone()
    conn.close()
    assert row["requested_service"] == "system.ai.router"
    assert row["observed_backend"] == "actual-backend"
    assert row["surface"] == "unified" and row["protocol"] == "responses"
    assert row["request_id"] == "req-123"
    assert row["reasoning_tokens"] == 10
    assert row["cost_usd"] == pytest.approx(140 / 1_000_000)
    assert row["pricing_source"].endswith("system.ai.router")


def test_service_echo_and_response_id_do_not_invent_backend_or_request_id():
    message = AIMessage(content="ok", id="resp-123", response_metadata={"model_name": "system.ai.router"})
    identity = E._call_identity("databricks:system.ai.router", message)
    assert identity["requested_service"] == "system.ai.router"
    assert identity["observed_backend"] is None
    assert identity["request_id"] is None


def test_missing_usage_is_unknown_and_budget_continues_with_visible_coverage(tmp_path, caplog, monkeypatch):
    mw = E.TokenEconomyMiddleware(
        max_usd=1, ledger_path=tmp_path / "usage.db", pricing={"known": U.Price(1, 2)},
    )
    events = []
    monkeypatch.setattr(mw, "_record_budget_event", events.append)
    message = AIMessage(content="no usage", response_metadata={"request_id": "req-no-usage"})
    request = _Request(model="known")
    response = _Response([message])
    # Trust-first: absent usage doesn't silently block inference or count as $0.
    assert mw.wrap_model_call(request, lambda r: response) is response
    mw.wrap_model_call(request, lambda r: response)
    total = U.totals(path=tmp_path / "usage.db")
    assert total.cost_known is False
    assert total.missing_usage_calls == 2
    assert mw._running("t-1")["usd"] == 0
    assert mw._running("t-1")["unknown_calls"] == 2
    assert len(events) == 1
    assert "cannot count toward the client USD cap" in caplog.text
    conn = U.connect(tmp_path / "usage.db")
    row = conn.execute("SELECT * FROM usage LIMIT 1").fetchone()
    conn.close()
    assert row["cost_usd"] is None
    assert row["usage_complete"] == 0
    assert row["request_id"] == "req-no-usage"


def test_already_merged_stream_usage_is_accounted_once(tmp_path):
    from langchain_core.messages import AIMessageChunk

    # Both delta and final-only adapter strategies converge to one final message.
    stream = (
        AIMessageChunk(content="a", usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11})
        + AIMessageChunk(content="b", usage_metadata={"input_tokens": 0, "output_tokens": 2, "total_tokens": 2})
    )
    mw = E.TokenEconomyMiddleware(ledger_path=tmp_path / "u.db", pricing={"known": U.Price(1, 2)})
    mw.wrap_model_call(_Request(model="known"), lambda r: _Response([stream]))
    total = U.totals(path=tmp_path / "u.db")
    assert total.calls == 1
    assert total.input_tokens == 10 and total.output_tokens == 3
    assert total.cost_usd == pytest.approx(16 / 1_000_000)


def test_other_reported_service_is_not_a_resolved_backend():
    message = AIMessage(content="ok", response_metadata={"model_name": "system.ai.other-service"})
    assert E._call_identity("system.ai.router", message)["observed_backend"] is None
    message.response_metadata = {"backend_model": "system.ai.explicit-backend"}
    assert E._call_identity("system.ai.router", message)["observed_backend"] == "system.ai.explicit-backend"


def test_request_id_from_gateway_headers():
    message = AIMessage(content="ok", response_metadata={"headers": {"X-Databricks-Request-Id": "gateway-id"}})
    assert E._call_identity("system.ai.router", message)["request_id"] == "gateway-id"


def test_daily_budget_warns_for_preexisting_unpriced_calls(tmp_path, caplog, monkeypatch):
    ledger = tmp_path / "usage.db"
    U.record_usage(U.UsageRecord(model="private", input_tokens=20, output_tokens=4), path=ledger)
    mw = E.TokenEconomyMiddleware(daily_max_usd=10, ledger_path=ledger)
    events = []
    monkeypatch.setattr(mw, "_record_budget_event", events.append)
    assert mw._maybe_pause("new-thread") is None
    assert len(events) == 1
    assert "USD budget coverage is incomplete" in caplog.text
    assert mw._maybe_pause("new-thread") is None
    assert len(events) == 1
