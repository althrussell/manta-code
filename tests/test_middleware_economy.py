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
    mw = E.TokenEconomyMiddleware(agent="swe", ledger_path=db)

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
