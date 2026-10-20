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


@pytest.mark.parametrize("first_scope", ["agent", "daily"])
def test_approval_of_one_budget_does_not_waive_a_later_other_budget(tmp_path, monkeypatch, first_scope):
    from types import SimpleNamespace
    import langgraph.types as lt

    running = {"tokens": 100.0 if first_scope == "agent" else 0.0,
               "usd": 0.0, "unknown_calls": 0.0}
    daily_spend = [2.0 if first_scope == "daily" else 0.0]
    prompts = []
    monkeypatch.setattr(E.TokenEconomyMiddleware, "_running", lambda self, thread: running)
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: daily_spend[0])
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    monkeypatch.setattr(lt, "interrupt", lambda payload: (
        prompts.append(payload) or {"decisions": [{"type": "approve"}]}
    ))
    options = dict(agent="swe", max_tokens=100, daily_max_usd=1, ledger_path=tmp_path / "u.db")
    middleware = E.TokenEconomyMiddleware(**options)
    assert middleware._maybe_pause("same-thread") is None
    assert prompts[0]["action_requests"][0]["args"]["budget_scopes"] == [first_scope]

    running["tokens"] = 100.0
    daily_spend[0] = 2.0
    # A rebuilt graph/helper retains only the consent actually granted.
    middleware = E.TokenEconomyMiddleware(**options)
    assert middleware._maybe_pause("same-thread") is None
    second_scope = "daily" if first_scope == "agent" else "agent"
    assert [p["action_requests"][0]["args"]["budget_scopes"] for p in prompts] == [
        [first_scope], [second_scope]
    ]
    assert E.TokenEconomyMiddleware(**options)._maybe_pause("same-thread") is None
    assert len(prompts) == 2


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_daily_budget_decision_and_coverage_reset_on_utc_rollover(tmp_path, monkeypatch, decision):
    from types import SimpleNamespace
    import langgraph.types as lt

    day = ["2026-10-20"]
    prompts, coverage_checks, warnings = [], [], []
    monkeypatch.setattr(U, "_utc_day_start", lambda *args: (day[0], 0.0))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: (
        coverage_checks.append(day[0]) or SimpleNamespace(cost_known=False)
    ))
    monkeypatch.setattr(lt, "interrupt", lambda payload: (
        prompts.append(payload) or {"decisions": [{"type": decision}]}
    ))
    middleware = E.TokenEconomyMiddleware(daily_max_usd=1, ledger_path=tmp_path / "u.db")
    monkeypatch.setattr(middleware, "_record_budget_event", warnings.append)
    expected = "stop" if decision == "reject" else None
    assert middleware._maybe_pause("long-lived") == expected
    assert middleware._maybe_pause("long-lived") == expected
    day[0] = "2026-10-21"
    assert middleware._maybe_pause("long-lived") == expected
    assert middleware._maybe_pause("long-lived") == expected
    assert [p["action_requests"][0]["args"]["utc_day"] for p in prompts] == [
        "2026-10-20", "2026-10-21"
    ]
    assert coverage_checks == ["2026-10-20", "2026-10-21"]
    assert len(warnings) == 2


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_daily_consent_is_shared_across_agents_but_not_other_threads(tmp_path, monkeypatch, decision):
    from types import SimpleNamespace
    import langgraph.types as lt

    prompts = []
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    monkeypatch.setattr(lt, "interrupt", lambda payload: (
        prompts.append(payload) or {"decisions": [{"type": decision}]}
    ))
    options = dict(daily_max_usd=1, ledger_path=tmp_path / "u.db")
    expected = "stop" if decision == "reject" else None
    assert E.TokenEconomyMiddleware(agent="swe", **options)._maybe_pause("shared") == expected
    assert E.TokenEconomyMiddleware(agent="review", **options)._maybe_pause("shared") == expected
    assert len(prompts) == 1
    assert E.TokenEconomyMiddleware(agent="review", **options)._maybe_pause("different") == expected
    assert len(prompts) == 2


def test_simultaneous_agent_and_daily_budget_pause_resumes_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    middleware = E.TokenEconomyMiddleware(max_tokens=100, daily_max_usd=1,
                                        ledger_path=tmp_path / "u.db")
    middleware._totals["both"] = {"tokens": 100.0, "usd": 0.0, "unknown_calls": 0.0}
    dispatched = []

    def send(state):
        assert middleware._maybe_pause("both") is None
        dispatched.append("sent")
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("send", send)
    builder.add_edge(START, "send")
    builder.add_edge("send", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "both"}}
    paused = graph.invoke({"done": False}, config)
    assert dispatched == []
    action = paused["__interrupt__"][0].value["action_requests"][0]
    assert action["args"]["budget_scopes"] == ["agent", "daily"]
    resumed = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert resumed["done"] is True
    assert dispatched == ["sent"]
    assert middleware._maybe_pause("both") is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("change", ["utc_rollover", "new_daily_cap"])
def test_delayed_budget_approval_retains_reviewed_scope_and_rechecks(
    tmp_path, monkeypatch, asynchronous, change
):
    from types import SimpleNamespace
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    day = ["2026-10-20"]
    daily_spend = [2.0 if change == "utc_rollover" else 0.0]
    running = {"tokens": 0.0 if change == "utc_rollover" else 100.0,
               "usd": 0.0, "unknown_calls": 0.0}
    monkeypatch.setattr(U, "_utc_day_start", lambda *args: (day[0], 0.0))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: daily_spend[0])
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    middleware = E.TokenEconomyMiddleware(max_tokens=100, daily_max_usd=1,
                                        ledger_path=tmp_path / "u.db")
    monkeypatch.setattr(middleware, "_running", lambda thread: running)
    request = _Request(runtime=_Runtime(thread_id="delayed"))
    dispatched = []

    def handler(request):
        dispatched.append("sent")
        return _Response(result=[_ai({"input_tokens": 1, "output_tokens": 1})])

    async def async_handler(request):
        return handler(request)

    def send(state):
        middleware.wrap_model_call(request, handler)
        return {"done": True}

    async def async_send(state):
        await middleware.awrap_model_call(request, async_handler)
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("send", async_send if asynchronous else send)
    builder.add_edge(START, "send")
    builder.add_edge("send", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "delayed"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    first = invoke({"done": False})["__interrupt__"][0].value["action_requests"][0]["args"]
    if change == "utc_rollover":
        assert first["utc_day"] == "2026-10-20"
        day[0] = "2026-10-21"
    else:
        assert first["budget_scopes"] == ["agent"]
        daily_spend[0] = 2.0

    second = invoke(Command(resume={"decisions": [{"type": "approve"}]}))
    assert dispatched == []
    action = second["__interrupt__"][0].value["action_requests"][0]["args"]
    assert action["budget_scopes"] == ["daily"]
    assert action["utc_day"] == day[0]
    assert ("delayed", day[0]) not in middleware._daily_approved
    if change == "utc_rollover":
        assert ("delayed", "2026-10-20") in middleware._daily_approved
    else:
        assert ("delayed", "") in middleware._agent_approved

    completed = invoke(Command(resume={"decisions": [{"type": "approve"}]}))
    assert completed["done"] is True
    assert dispatched == ["sent"]
    assert ("delayed", day[0]) in middleware._daily_approved


@pytest.mark.parametrize("asynchronous", [False, True])
def test_direct_helper_callback_retains_budget_scope_during_replay(tmp_path, monkeypatch, asynchronous):
    from types import SimpleNamespace
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command
    from manta_code import accounting

    day = ["2026-10-20"]
    ledger = tmp_path / "usage.db"
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setattr(U, "_utc_day_start", lambda *args: (day[0], 0.0))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    accounting.install_usage_collector()
    middleware = E.TokenEconomyMiddleware(agent="helper", daily_max_usd=1, ledger_path=ledger)
    model = FakeMessagesListChatModel(responses=[_ai({"input_tokens": 1, "output_tokens": 1})])
    call_config = {"metadata": {"lc_source": "summarization"}}

    def scope():
        return accounting.accounting_scope(agent="helper", thread_id="helper-delay", model=model,
                                           ledger_path=ledger, budget_guard=middleware)

    def send(state):
        with scope():
            model.invoke("summarize", config=call_config)
        return {"done": True}

    async def async_send(state):
        with scope():
            await model.ainvoke("summarize", config=call_config)
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("helper", async_send if asynchronous else send)
    builder.add_edge(START, "helper")
    builder.add_edge("helper", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "helper-delay"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    assert "__interrupt__" in invoke({"done": False})
    day[0] = "2026-10-21"
    second = invoke(Command(resume={"decisions": [{"type": "approve"}]}))
    assert second["__interrupt__"][0].value["action_requests"][0]["args"]["utc_day"] == day[0]
    accounting.flush_usage()
    assert U.totals(path=ledger).calls == 0
    assert invoke(Command(resume={"decisions": [{"type": "approve"}]}))["done"] is True
    accounting.flush_usage()
    assert U.totals(path=ledger).calls == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("helper", [False, True])
@pytest.mark.parametrize("cleared_by", [
    "utc_rollover", "ledger_correction", "raised_cap", "removed_cap", "advisory_mode"
])
def test_pending_rejection_is_honored_after_live_threshold_clears(
    tmp_path, monkeypatch, asynchronous, helper, cleared_by
):
    from types import SimpleNamespace
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command
    from manta_code import accounting

    day, spend = ["2026-10-20"], [2.0]
    ledger = tmp_path / "usage.db"
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setattr(U, "_utc_day_start", lambda *args: (day[0], 0.0))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: spend[0])
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    middlewares = [E.TokenEconomyMiddleware(daily_max_usd=1, ledger_path=ledger)]
    model = FakeMessagesListChatModel(responses=[_ai({"input_tokens": 1, "output_tokens": 1})])
    request = _Request(runtime=_Runtime(thread_id="cleared"))
    dispatched = []
    accounting.install_usage_collector()

    def scope():
        return accounting.accounting_scope(thread_id="cleared", model=model,
                                           ledger_path=ledger, budget_guard=middlewares[0])

    def handler(request):
        dispatched.append("sent")
        return _Response(result=[_ai({"input_tokens": 1, "output_tokens": 1})])

    async def async_handler(request):
        return handler(request)

    def send(state):
        if helper:
            with scope():
                model.invoke("summarize", config={"metadata": {"lc_source": "summarization"}})
        else:
            middlewares[0].wrap_model_call(request, handler)
        return {"done": True}

    async def async_send(state):
        if helper:
            with scope():
                await model.ainvoke("summarize", config={"metadata": {"lc_source": "summarization"}})
        else:
            await middlewares[0].awrap_model_call(request, async_handler)
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("send", async_send if asynchronous else send)
    builder.add_edge(START, "send")
    builder.add_edge("send", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "cleared"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    assert "__interrupt__" in invoke({"done": False})
    if cleared_by == "utc_rollover":
        day[0], spend[0] = "2026-10-21", 0.0
    elif cleared_by == "ledger_correction":
        spend[0] = 0.0
    elif cleared_by == "raised_cap":
        middlewares[0] = E.TokenEconomyMiddleware(daily_max_usd=3, ledger_path=ledger)
    elif cleared_by == "removed_cap":
        middlewares[0] = E.TokenEconomyMiddleware(ledger_path=ledger)
    else:
        middlewares[0] = E.TokenEconomyMiddleware(daily_max_usd=1, budget_mode="advisory", ledger_path=ledger)
    rejection = Command(resume={"decisions": [{"type": "reject"}]})
    if helper:
        with pytest.raises(RuntimeError, match="Stopped at the budget cap"):
            invoke(rejection)
    else:
        assert invoke(rejection)["done"] is True
    accounting.flush_usage()
    assert dispatched == []
    assert U.totals(path=ledger).calls == 0


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["agent", "daily"])
def test_rebuilt_budget_limit_requires_its_own_approval(tmp_path, monkeypatch, asynchronous, kind):
    from types import SimpleNamespace
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    ledger = tmp_path / "usage.db"
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    options = dict(ledger_path=ledger)
    options["max_tokens" if kind == "agent" else "daily_max_usd"] = 100 if kind == "agent" else 1
    middlewares = [E.TokenEconomyMiddleware(**options)]
    monkeypatch.setattr(E.TokenEconomyMiddleware, "_running", lambda self, thread: {
        "tokens": 100.0, "usd": 0.0, "unknown_calls": 0.0
    })
    dispatched = []

    def send(state):
        assert middlewares[0]._maybe_pause("changed-cap") is None
        dispatched.append("sent")
        return {"done": True}

    async def async_send(state):
        return await asyncio.to_thread(send, state)

    builder = StateGraph(dict)
    builder.add_node("send", async_send if asynchronous else send)
    builder.add_edge(START, "send")
    builder.add_edge("send", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "changed-cap"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    assert "__interrupt__" in invoke({"done": False})
    options["max_tokens" if kind == "agent" else "daily_max_usd"] = 50 if kind == "agent" else 0.5
    middlewares[0] = E.TokenEconomyMiddleware(**options)
    second = invoke(Command(resume={"decisions": [{"type": "approve"}]}))
    args = second["__interrupt__"][0].value["action_requests"][0]["args"]
    assert args["max_tokens" if kind == "agent" else "daily_max_usd"] == (50 if kind == "agent" else 0.5)
    assert dispatched == []
    assert invoke(Command(resume={"decisions": [{"type": "approve"}]}))["done"] is True
    assert dispatched == ["sent"]


def test_reviewed_budget_scope_survives_sqlite_graph_and_process_state_rebuild(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    day = ["2026-10-20"]
    ledger = tmp_path / "usage.db"
    monkeypatch.setattr(U, "_utc_day_start", lambda *args: (day[0], 0.0))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    dispatched = []

    def build(saver):
        middleware = E.TokenEconomyMiddleware(daily_max_usd=1, ledger_path=ledger)

        def send(state):
            assert middleware._maybe_pause("sqlite-replay") is None
            dispatched.append("sent")
            return {"done": True}

        builder = StateGraph(dict)
        builder.add_node("send", send)
        builder.add_edge(START, "send")
        builder.add_edge("send", END)
        return builder.compile(checkpointer=saver), middleware

    checkpoint = str(tmp_path / "checkpoints.db")
    config = {"configurable": {"thread_id": "sqlite-replay"}}
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        graph, _ = build(saver)
        assert "__interrupt__" in graph.invoke({"done": False}, config)
    day[0] = "2026-10-21"
    # Separate processes have no in-memory decision cache. The saved task
    # scope and resume positions must still preserve both approval boundaries.
    monkeypatch.setattr(E, "_DECISIONS", {})
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        graph, middleware = build(saver)
        second = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert second["__interrupt__"][0].value["action_requests"][0]["args"]["utc_day"] == day[0]
        assert ("sqlite-replay", "2026-10-20") in middleware._daily_approved
        assert dispatched == []
    monkeypatch.setattr(E, "_DECISIONS", {})
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        graph, middleware = build(saver)
        completed = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert completed["done"] is True
        assert middleware._daily_approved == {("sqlite-replay", "2026-10-20"),
                                              ("sqlite-replay", "2026-10-21")}
        assert dispatched == ["sent"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("changed_to", ["removed_cap", "advisory"])
def test_unscoped_helper_honors_pending_rejection_after_budget_config_changes(
    tmp_path, monkeypatch, asynchronous, changed_to
):
    from types import SimpleNamespace
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command
    from manta_code import accounting

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: 2.0)
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    settings = tmp_path / "config.toml"
    settings.write_text('[budget]\ndaily_max_usd=1\nmode="checkpoint"\n')
    accounting.install_usage_collector()
    model = FakeMessagesListChatModel(responses=[_ai({"input_tokens": 1, "output_tokens": 1})])
    call_config = {"metadata": {"lc_source": "summarization"}}

    def send(state):
        model.invoke("summarize", config=call_config)
        return {"done": True}

    async def async_send(state):
        await model.ainvoke("summarize", config=call_config)
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("helper", async_send if asynchronous else send)
    builder.add_edge(START, "helper")
    builder.add_edge("helper", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "unscoped-helper"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    assert "__interrupt__" in invoke({"done": False})
    settings.write_text('' if changed_to == "removed_cap" else '[budget]\ndaily_max_usd=1\nmode="advisory"\n')
    with pytest.raises(RuntimeError, match="Stopped at the budget cap"):
        invoke(Command(resume={"decisions": [{"type": "reject"}]}))
    accounting.flush_usage()
    assert U.totals().calls == 0


@pytest.mark.parametrize("asynchronous", [False, True])
def test_completed_budget_check_preserves_later_effect_and_other_approval(tmp_path, monkeypatch, asynchronous):
    from types import SimpleNamespace
    from langgraph.func import task
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from manta_code.budget_runtime import _pending_budget_interrupt_ids

    spend = [0.0]
    monkeypatch.setattr(U, "today_total_usd", lambda **kwargs: spend[0])
    monkeypatch.setattr(U, "today_totals", lambda **kwargs: SimpleNamespace(cost_known=True))
    middleware = E.TokenEconomyMiddleware(daily_max_usd=1, ledger_path=tmp_path / "usage.db")
    dispatched, effects = [], []
    external_request = {"action_requests": [{"name": "fixture_write"}]}

    @task(name="completed_effect")
    def effect():
        effects.append("effect")
        return "done"

    def first(state):
        assert middleware._maybe_pause("other-interrupt") is None
        assert effect().result() == "done"
        decision = interrupt(external_request)
        return {"decision": decision}

    async def async_first(state):
        assert await asyncio.to_thread(middleware._maybe_pause, "other-interrupt") is None
        assert await effect() == "done"
        decision = interrupt(external_request)
        return {"decision": decision}

    def send(state):
        assert middleware._maybe_pause("other-interrupt") is None
        dispatched.append("sent")
        return state

    async def async_send(state):
        assert await asyncio.to_thread(middleware._maybe_pause, "other-interrupt") is None
        dispatched.append("sent")
        return state

    builder = StateGraph(dict)
    builder.add_node("first", async_first if asynchronous else first)
    builder.add_node("send", async_send if asynchronous else send)
    builder.add_edge(START, "first")
    builder.add_edge("first", "send")
    builder.add_edge("send", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "other-interrupt"}}
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (
        lambda value: graph.invoke(value, config)
    )
    original = invoke({})["__interrupt__"][0]
    assert original.value == external_request
    assert effects == ["effect"]
    spend[0] = 2.0
    decision = {"decisions": [{"type": "approve"}]}
    budget = invoke(Command(resume={original.id: decision}))["__interrupt__"][0]
    assert budget.id != original.id
    assert budget.value["action_requests"][0]["name"] == "manta_budget_continue"
    assert middleware._daily_approved == set()
    assert dispatched == []
    assert effects == ["effect"]
    assert _pending_budget_interrupt_ids(graph.get_state(config)) == {budget.id}
    completed = invoke(Command(resume={budget.id: decision}))
    assert completed["decision"] == decision
    assert dispatched == ["sent"]
    assert effects == ["effect"]


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
    # Re-observing the same provider request is one accounting event.
    assert total.missing_usage_calls == 1
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
