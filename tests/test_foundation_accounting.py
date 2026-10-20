from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from manta_code import accounting
from manta_code.agents import usage
from manta_code.middleware.economy import TokenEconomyMiddleware


def message(request: str = "") -> AIMessage:
    return AIMessage(content="done", usage_metadata={"input_tokens": 100, "output_tokens": 20,
                     "total_tokens": 120}, response_metadata={"request_id": request} if request else {})


def test_main_child_and_helpers_reconcile_without_duplicate_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    accounting.install_usage_collector()
    model = FakeMessagesListChatModel(responses=[message()])
    request = SimpleNamespace(model=model, runtime=SimpleNamespace(thread_id="session"),
                              messages=[HumanMessage(content="work")], system_message=None, tools=[])
    main = TokenEconomyMiddleware(agent="swe", ledger_path=tmp_path / ".state/usage.db")
    main.wrap_model_call(request, lambda r: model.invoke(r.messages))
    with accounting.accounting_scope(agent="review", thread_id="session", model=model):
        model.invoke("review")
    for kind in ("classifier", "summarizer", "grader"):
        model.invoke("helper", config={"metadata": {"thread_id": "session", "manta_call_kind": kind}})
    accounting.flush_usage()
    with usage.connect() as conn:
        rows = conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
    assert len(rows) == 5
    assert {r["call_kind"] for r in rows} == {"agent", "classifier", "summarizer", "grader"}
    assert len({r["invocation_id"] for r in rows}) == 5
    assert sum(r["input_tokens"] for r in rows) == 500
    assert all(r["thread_id"] == "session" for r in rows)
    assert all(r["cost_usd"] is None for r in rows)


def test_request_id_fallback_deduplicates_repeated_response(tmp_path):
    db = tmp_path / "ledger.db"
    for _ in range(2):
        usage.record_model_response("system.ai.test", message("same-request"), path=db)
    assert usage.totals(path=db).calls == 1


def test_price_override_provenance_is_inspectable():
    match = usage.pricing_for("system.ai.test", {"system.ai.test": usage.Price(
        1, 2, source="workspace contract", as_of="2026-10-20",
    )})
    assert "workspace contract" in match.source
    assert "2026-10-20" in match.version


def test_async_callback_and_middleware_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    accounting.install_usage_collector()
    model = FakeMessagesListChatModel(responses=[message()])
    request = SimpleNamespace(model=model, runtime=SimpleNamespace(thread_id="async"),
                              messages=[HumanMessage(content="work")], system_message=None, tools=[])
    mw = TokenEconomyMiddleware(agent="swe")
    asyncio.run(mw.awrap_model_call(request, lambda r: model.ainvoke(r.messages)))
    accounting.flush_usage()
    assert usage.totals().calls == 1


def test_budget_checkpoint_survives_graph_and_middleware_rebuild(tmp_path, monkeypatch):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    calls = []
    config = {"configurable": {"thread_id": "durable"}}
    request = SimpleNamespace(model="unpriced", runtime=SimpleNamespace(thread_id="durable"),
                              messages=[], system_message=None, tools=[])

    def build(checkpointer):
        mw = TokenEconomyMiddleware(agent="swe", max_tokens=100)

        def invoke(state):
            def handler(req):
                calls.append("sent")
                return message()
            mw.wrap_model_call(request, handler)
            return {"count": state.get("count", 0) + 1}

        graph = StateGraph(dict)
        graph.add_node("first", invoke)
        graph.add_node("second", invoke)
        graph.add_edge(START, "first")
        graph.add_edge("first", "second")
        graph.add_edge("second", END)
        return graph.compile(checkpointer=checkpointer)

    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        graph = build(saver)
        result = graph.invoke({"count": 0}, config)
        assert "__interrupt__" in result
        assert calls == ["sent"]
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        graph = build(saver)
        result = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert result["count"] == 2
        assert calls == ["sent", "sent"]
    assert usage.totals().total_tokens == 240


def test_advisory_is_explicit_and_unavailable_checkpoint_fails_closed(tmp_path):
    mw = TokenEconomyMiddleware(max_tokens=1, ledger_path=tmp_path / "one.db")
    mw._totals[""] = {"tokens": 2, "usd": 0, "unknown_calls": 0}
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        mw._maybe_pause("")
    advisory = TokenEconomyMiddleware(max_tokens=1, budget_mode="advisory", ledger_path=tmp_path / "two.db")
    advisory._totals[""] = {"tokens": 2, "usd": 0, "unknown_calls": 0}
    assert advisory._maybe_pause("") is None


def test_inconsistent_provider_totals_remain_unpriced():
    breakdown = usage.extract_breakdown({"input_tokens": 5, "output_tokens": 1, "total_tokens": 149})
    assert not breakdown.usage_complete
    assert breakdown.cost_usd(usage.Price(1, 2)) is None


def test_request_snapshots_task_price_and_adapter_identity(tmp_path, monkeypatch):
    from langchain_core.outputs import ChatGeneration, LLMResult
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_TASK_ID", "original")
    (tmp_path / "config.toml").write_text('[pricing."system.ai.test"]\ninput=1\noutput=2\nsource="contract"\n')
    recorder = accounting.RequestCollector()
    model = SimpleNamespace(model_name="databricks:system.ai.test",
                            target_policy=SimpleNamespace(surface="unified", api="responses"))
    with accounting.accounting_scope(agent="swe", model=model):
        recorder.on_chat_model_start({}, [], run_id="snapshot", metadata={"ls_model_name": "system.ai.test"})
    monkeypatch.setenv("MANTA_TASK_ID", "changed")
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "other"))
    recorder.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message())]]), run_id="snapshot")
    recorder.flush()
    with usage.connect(tmp_path / ".state/usage.db") as conn:
        row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["task"] == "original"
    assert row["requested_service"] == "system.ai.test"
    assert "config:exact:system.ai.test" in row["pricing_source"]
    assert row["cost_usd"] == pytest.approx(0.00014)


def test_prices_invalidate_when_project_or_config_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "home"))
    for project, value in (("a", 1), ("b", 3)):
        path = tmp_path / project / ".manta"
        path.mkdir(parents=True)
        (path / "config.toml").write_text(f'[pricing."system.ai.test"]\ninput={value}\noutput=2\n')
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path / "a"))
    assert usage.price_for("system.ai.test").input == 1
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path / "b"))
    assert usage.price_for("system.ai.test").input == 3
    (tmp_path / "b/.manta/config.toml").write_text('[pricing."system.ai.test"]\ninput=4\noutput=20\n')
    assert usage.price_for("system.ai.test").input == 4


def test_checkpoint_budget_ledger_failure_sends_no_request(tmp_path, monkeypatch):
    mw = TokenEconomyMiddleware(agent="swe", max_tokens=100, daily_max_usd=1,
                               ledger_path=tmp_path / "broken.db")
    request = SimpleNamespace(model="fake", runtime=SimpleNamespace(thread_id="strict"))
    monkeypatch.setattr(usage, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")))
    calls = []
    with pytest.raises(OSError, match="unavailable"):
        mw.wrap_model_call(request, lambda r: calls.append("sent"))
    assert calls == []
    advisory = TokenEconomyMiddleware(agent="swe", daily_max_usd=1, budget_mode="advisory")
    assert advisory._maybe_pause("strict") is None


def test_cost_drilldown_retains_reported_reasoning_cache_and_latency(tmp_path):
    db = tmp_path / "usage.db"
    usage.record_usage(usage.UsageRecord(thread_id="cost", call_kind="grader", reasoning_tokens=3,
                                       cache_available=True, cache_read=7, duration_ms=12, request_id="req"), path=db)
    row, = usage.recent_calls("cost", path=db)
    assert (row.call_kind, row.reasoning_tokens, row.cache_read, row.duration_ms, row.request_id) == ("grader", 3, 7, 12, "req")


@pytest.mark.parametrize("error", [TimeoutError("do not retain request contents"), asyncio.CancelledError()])
def test_failed_and_cancelled_attempts_are_visible_as_unknown(error, tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    recorder = accounting.RequestCollector()
    recorder.on_chat_model_start({}, [], run_id="failed", metadata={"lc_source": "auto_mode_classifier"})
    recorder.on_llm_error(error, run_id="failed")
    recorder.on_llm_error(error, run_id="failed")  # Duplicate callback observation.
    recorder.flush()
    totals = usage.totals()
    assert totals.calls == 1 and totals.missing_usage_calls == 1 and not totals.cost_known
    with usage.connect() as conn:
        row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["call_kind"] == "classifier"
    assert row["outcome"] in {"error", "cancelled"}


@pytest.mark.parametrize("metadata,tags,expected", [
    ({"lc_source": "auto_mode_classifier"}, ["dcode:auto"], "classifier"),
    ({"lc_source": "summarization"}, [], "summarizer"),
    ({"lc_agent_name": "goal_criteria_agent"}, [], "grader"),
])
def test_upstream_helper_metadata_is_classified(metadata, tags, expected):
    assert accounting._purpose(metadata, tags) == expected


@pytest.mark.parametrize("asynchronous", [False, True])
def test_compiled_grader_graph_name_reaches_nested_model_purpose(tmp_path, monkeypatch, asynchronous):
    from langgraph.graph import END, START, StateGraph
    from deepagents_code.reliable_rubric import ReliableRubricMiddleware

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    accounting.install_usage_collector()
    class GraderModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    graded = message().model_copy(update={"content": "", "tool_calls": [{
        "name": "GraderResponse", "args": {"result": "satisfied", "explanation": "Fixture completed.",
        "criteria": [{"name": "Response says done", "passed": True}]}, "id": "grader-fixture",
        "type": "tool_call",
    }]})
    model = GraderModel(responses=[graded])
    rubric = ReliableRubricMiddleware(model=model)
    builder = StateGraph(dict)
    if asynchronous:
        async def call(state):
            result = await rubric._ainvoke_grader(state, 0)
            assert result.result == "satisfied"
            return state
    else:
        def call(state):
            result = rubric._invoke_grader(state, 0)
            assert result.result == "satisfied"
            return state
    builder.add_node("verification", call)
    builder.add_edge(START, "verification")
    builder.add_edge("verification", END)
    graph = builder.compile(name="fixture_parent")
    config = {"metadata": {"thread_id": "real-helper-graph"}}
    state = {"messages": [HumanMessage(content="Say done"), AIMessage(content="done")],
             "rubric": "1. Response says done"}
    if asynchronous:
        asyncio.run(graph.ainvoke(state, config))
    else:
        graph.invoke(state, config)
    accounting.flush_usage()
    with usage.connect() as conn:
        rows = conn.execute("SELECT call_kind, thread_id FROM usage").fetchall()
    assert [tuple(row) for row in rows] == [("grader", "real-helper-graph")]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_agent_and_rubric_keep_distinct_request_purposes(tmp_path, monkeypatch, asynchronous):
    from deepagents_code.reliable_rubric import ReliableRubricMiddleware
    from langchain.agents import create_agent

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path))
    accounting.install_usage_collector()

    class GraderModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    graded = message().model_copy(update={"content": "", "tool_calls": [{
        "name": "GraderResponse", "args": {"result": "satisfied", "explanation": "Fixture completed.",
        "criteria": [{"name": "Response says done", "passed": True}]}, "id": "grader-fixture",
        "type": "tool_call",
    }]})
    evaluations = []
    graph = create_agent(
        model=FakeMessagesListChatModel(responses=[message()]),
        middleware=[TokenEconomyMiddleware(), ReliableRubricMiddleware(
            model=GraderModel(responses=[graded]), on_evaluation=evaluations.append)],
        name="fixture_coding_agent",
    )
    config = {"configurable": {"thread_id": "agent-and-grader"}}
    state = {"messages": [HumanMessage(content="Say done")], "rubric": "1. Response says done"}
    if asynchronous:
        result = asyncio.run(graph.ainvoke(state, config))
    else:
        result = graph.invoke(state, config)
    assert result["messages"][-1].content == "done"
    assert [evaluation["result"] for evaluation in evaluations] == ["satisfied"]
    accounting.flush_usage()
    with usage.connect() as conn:
        rows = conn.execute("SELECT agent, call_kind, thread_id FROM usage ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("orchestrator", "agent", "agent-and-grader"),
        ("orchestrator", "grader", "agent-and-grader"),
    ]
    assert usage.totals().total_tokens == 240


@pytest.mark.parametrize("asynchronous", [False, True])
def test_direct_helper_pauses_before_dispatch_and_resumes_same_graph(tmp_path, monkeypatch, asynchronous):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Command
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "config.toml").write_text('[budget]\ndaily_max_usd=1\nmode="checkpoint"\n')
    usage.record_usage(usage.UsageRecord(cost_usd=2, thread_id="prior"))
    accounting.install_usage_collector()
    model = FakeMessagesListChatModel(responses=[message()])
    config = {"configurable": {"thread_id": "helper-paused"}}
    async def async_helper(state):
        await model.ainvoke("summarize", config={"metadata": {"lc_source": "summarization"}})
        return {"done": True}
    def sync_helper(state):
        model.invoke("summarize", config={"metadata": {"lc_source": "summarization"}})
        return {"done": True}
    builder = StateGraph(dict)
    builder.add_node("helper", async_helper if asynchronous else sync_helper)
    builder.add_edge(START, "helper")
    builder.add_edge("helper", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    invoke = (lambda value: asyncio.run(graph.ainvoke(value, config))) if asynchronous else (lambda value: graph.invoke(value, config))
    paused = invoke({"done": False})
    assert "__interrupt__" in paused
    accounting.flush_usage()
    assert usage.totals().calls == 1  # Only the seeded earlier request.
    resumed = invoke(Command(resume={"decisions": [{"type": "approve"}]}))
    assert resumed["done"] is True
    accounting.flush_usage()
    assert usage.totals().calls == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_direct_adapter_helper_retains_target_and_protocol(tmp_path, monkeypatch, asynchronous):
    import httpx
    from manta_code.databricks_chat import MantaChatDatabricks
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_PROJECT_ROOT", str(tmp_path))
    response = {"id": "chatcmpl-helper", "object": "chat.completion", "created": 0,
                "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "summary"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=response, headers={"x-request-id": "helper-id"}))
    workspace = SimpleNamespace(config=SimpleNamespace(host="https://fixture.databricks.com",
                                authenticate=lambda: {"Authorization": "Bearer fixture"}))
    model = MantaChatDatabricks(model="custom.team.helper", api="chat", workspace_client=workspace,
                               http_client=httpx.Client(transport=transport),
                               http_async_client=httpx.AsyncClient(transport=transport),
                               metadata={"manta_agent": "review"})
    accounting.install_usage_collector()
    config = {"metadata": {"lc_source": "summarization", "thread_id": "adapter-helper"}}
    if asynchronous:
        asyncio.run(model.ainvoke("summarize", config=config))
    else:
        model.invoke("summarize", config=config)
    accounting.flush_usage()
    with usage.connect() as conn:
        row = conn.execute("SELECT * FROM usage").fetchone()
    assert (row["agent"], row["call_kind"], row["requested_service"], row["surface"], row["protocol"]) == (
        "review", "summarizer", "custom.team.helper", "unified", "chat")
