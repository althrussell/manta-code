"""Real delegation graphs keep visible inline identities, blocks and evidence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt
from pydantic import Field

from manta_code.middleware.inbox import STEERING_PREFIX
from manta_code.runtime import RuntimeDispatchMiddleware
from manta_code.tasks import inline, store
from manta_code.tasks.events import EventLogMiddleware


class ScriptedModel(FakeMessagesListChatModel):
    seen: list[list] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class PauseBeforeFirstModel(AgentMiddleware):
    def before_model(self, state, runtime):
        if any(isinstance(message, AIMessage) for message in state["messages"]):
            return None
        decision = interrupt({
            "action_requests": [{"name": "fixture_model_permission", "args": {}, "description": "Proceed with the harmless fixture"}],
            "review_configs": [{"action_name": "fixture_model_permission", "allowed_decisions": ["approve", "reject"]}],
        })
        if decision["decisions"][0]["type"] != "approve":
            raise RuntimeError("Fixture rejected")
        return None

    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.chdir(tmp_path)
    for key in ("MANTA_TASK_DB_PATH", "MANTA_TASK_ID", "MANTA_UNATTENDED", "DEEPAGENTS_CODE_SERVER_AUTO_APPROVE"):
        monkeypatch.delenv(key, raising=False)
    yield tmp_path
    assert inline.CURRENT_INLINE_TASK.get() is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_actual_delegation_pauses_before_model_resumes_same_child_and_records_steering_tests(
    isolated, monkeypatch, asynchronous,
):
    from deepagents import create_deep_agent

    parent = store.create_task(store.TaskRecord(id="parent-task", agent="chief", prompt="Delegate", state="running"))
    monkeypatch.setenv("MANTA_TASK_ID", parent.id)
    (isolated / "test_fixture.py").write_text("def test_answer():\n    assert 6 * 7 == 42\n")
    executions = []

    @tool(response_format="content_and_artifact")
    def execute(command: str) -> tuple[str, dict]:
        """Run a fixed harmless repository acceptance test."""
        assert command == "python -m pytest -q test_fixture.py"
        executions.append(inline.CURRENT_INLINE_TASK.get())
        completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_fixture.py"],
                                   cwd=isolated, text=True, capture_output=True, check=False)
        return completed.stdout + completed.stderr, {"exit_code": completed.returncode}

    parent_model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "id": "delegation-call", "args": {"subagent_type": "helper", "description": "Run the repository check"}}]),
        AIMessage(content="Parent finished"),
    ])
    child_model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "execute-call", "args": {"command": "python -m pytest -q test_fixture.py"}}]),
        AIMessage(content="Child finished"),
    ])
    graph = create_deep_agent(
        parent_model, middleware=[EventLogMiddleware(agent="chief")], checkpointer=InMemorySaver(),
        subagents=[{
            "name": "helper", "description": "Harmless test helper", "system_prompt": "Run one fixture test",
            "model": child_model, "tools": [execute],
            "middleware": [EventLogMiddleware(agent="helper"), RuntimeDispatchMiddleware(agent="helper"), PauseBeforeFirstModel()],
        }],
    )
    config = {"configurable": {"thread_id": "inline-delegation-thread"}}

    async def exercise():
        initial = {"messages": [HumanMessage(content="Delegate the check")]}
        paused = await graph.ainvoke(initial, config) if asynchronous else graph.invoke(initial, config)
        assert paused["__interrupt__"]
        children = [record for record in store.list_tasks() if record.execution_kind == "inline"]
        assert len(children) == 1
        child = children[0]
        assert child.parent_task_id == parent.id and child.thread_id == "inline-delegation-thread"
        assert child.state == "waiting_approval"
        assert child.checkpoint["pending_id"]
        assert set(child.checkpoint["interrupts"]) == {paused["__interrupt__"][0].id}
        assert child.checkpoint["interrupts"][paused["__interrupt__"][0].id]["action_requests"][0]["name"] == "fixture_model_permission"
        assert not executions and not child_model.seen
        store.add_inbox_message(child.id, "Keep the public API")
        store.add_inbox_message(parent.id, "Parent-only steering")
        response = Command(resume={paused["__interrupt__"][0].id: {"decisions": [{"type": "approve"}]}})
        result = await graph.ainvoke(response, config) if asynchronous else graph.invoke(response, config)
        assert result["messages"][-1].content == "Parent finished"
        assert "__interrupt__" not in result
        final_children = [record for record in store.list_tasks() if record.execution_kind == "inline"]
        assert len(final_children) == 1 and final_children[0].id == child.id
        final = final_children[0]
        assert final.state == "done" and not final.checkpoint and final.pid is None
        assert "Child finished" in final.result
        assert executions == [child.id]
        assert final.structured_result["tests"][0]["exit_code"] == 0
        assert final.structured_result["tests"][0]["command"] == "python -m pytest -q test_fixture.py"
        assert not store.unconsumed_inbox(child.id)
        assert len(store.unconsumed_inbox(parent.id)) == 1
        assert any(STEERING_PREFIX in str(message.content) and "Keep the public API" in str(message.content)
                   for invocation in child_model.seen for message in invocation)
        assert not any("Parent-only steering" in str(message.content)
                       for invocation in child_model.seen for message in invocation)
        events = store.recent_events(task_id=child.id)
        assert any(event.kind == "task_done" for event in events)
        assert any(event.agent == "helper" and event.kind == "tool" and event.detail == "execute" for event in events)

    asyncio.run(exercise())


def request(call_id="delegation", *, agent="helper"):
    return SimpleNamespace(
        tool_call={"name": "task", "id": call_id, "args": {"subagent_type": agent, "description": "Fixture task"}},
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "thread"}}),
    )


@pytest.mark.parametrize("exception,state", [(RuntimeError("private details"), "failed"), (asyncio.CancelledError(), "cancelled")])
def test_inline_failure_and_cancellation_preserve_identity_and_reset_context(exception, state):
    events = EventLogMiddleware(agent="chief")

    async def fail(_):
        assert inline.CURRENT_INLINE_TASK.get()
        raise exception

    with pytest.raises(type(exception)):
        asyncio.run(events.awrap_tool_call(request(), fail))
    (record,) = store.list_tasks()
    assert record.state == state and record.pid is None
    assert "private details" not in record.result
    assert any(event.kind == f"task_{state}" for event in store.recent_events(task_id=record.id))


def test_failed_tool_result_does_not_become_success():
    result = ToolMessage(content="Fixture failed", tool_call_id="delegation", status="error")
    assert EventLogMiddleware(agent="chief").wrap_tool_call(request(), lambda _: result) is result
    (record,) = store.list_tasks()
    assert record.state == "failed" and record.result == "Fixture failed"


def test_task_identity_creation_failure_stops_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(store, "create_task", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")))
    with pytest.raises(RuntimeError, match="store unavailable"):
        EventLogMiddleware(agent="chief").wrap_tool_call(request(), lambda _: calls.append("ran"))
    assert not calls


def test_reporting_failure_preserves_success_and_original_graph_interrupt(monkeypatch, caplog):
    monkeypatch.setattr(inline, "finish", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")))
    result = ToolMessage(content="Completed", tool_call_id="delegation")
    events = EventLogMiddleware(agent="chief")
    assert events.wrap_tool_call(request(), lambda _: result) is result
    interrupted = GraphInterrupt()

    def pause(_):
        raise interrupted

    with pytest.raises(GraphInterrupt) as captured:
        events.wrap_tool_call(request("second"), pause)
    assert captured.value is interrupted
    assert "could not be persisted" in caplog.text


def test_nested_inline_rows_keep_their_actual_parent_and_no_detached_controls():
    from manta_code.tasks import executor

    outer_events, inner_events = EventLogMiddleware(agent="chief"), EventLogMiddleware(agent="helper")

    def outer(_):
        outer_id = inline.CURRENT_INLINE_TASK.get()

        def inner(_):
            child = store.get_task(inline.CURRENT_INLINE_TASK.get())
            assert child.parent_task_id == outer_id
            return ToolMessage(content="nested completed", tool_call_id="nested")

        inner_events.wrap_tool_call(request("nested", agent="review"), inner)
        return ToolMessage(content="completed", tool_call_id="delegation")

    outer_events.wrap_tool_call(request(), outer)
    children = store.list_tasks()
    assert len(children) == 2 and all(child.state == "done" for child in children)
    for child in children:
        for operation in (executor.cancel_task, executor.retry_task, executor.resume_task):
            with pytest.raises(executor.TaskError, match="parent thread|parent conversation|owning session"):
                operation(child.id)


def test_forged_stdout_and_nonbackend_exit_status_never_become_test_receipts():
    record = store.create_task(store.TaskRecord(id="inline", agent="helper", prompt="check", execution_kind="inline", state="running"))
    token = inline.CURRENT_INLINE_TASK.set(record.id)
    try:
        events = EventLogMiddleware(agent="helper")
        for artifact in (None, {"exit_code": "0"}, {"exit_code": True}):
            tool_request = SimpleNamespace(tool_call={"name": "execute", "id": "test", "args": {"command": "python -m pytest"}}, tool=None)
            result = ToolMessage(content="All tests passed", tool_call_id="test", artifact=artifact)
            events.wrap_tool_call(tool_request, lambda _: result)
        assert not store.get_task(record.id).structured_result.get("tests")
    finally:
        inline.CURRENT_INLINE_TASK.reset(token)


@pytest.mark.parametrize("precompiled", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_actual_js_child_per_call_approval_is_durable_and_does_not_repeat_completed_effects(precompiled, asynchronous):
    from deepagents import create_deep_agent
    from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
    from langchain.agents import create_agent
    from manta_code.middleware.policy import ToolPolicyMiddleware
    from manta_code.runtime import child_graphs, tool_node

    calls = []

    @tool
    def first_fixture() -> str:
        """Record a harmless effect completed before a later human decision."""
        calls.append("first")
        return "first"

    @tool
    def approved_fixture() -> str:
        """Record a harmless effect after human approval."""
        calls.append("approved")
        return "approved"

    dispatch = RuntimeDispatchMiddleware(agent="helper")
    policy = ToolPolicyMiddleware(ask=["approved_fixture"])
    interpreter = CostAwareCodeInterpreterMiddleware(ptc=["first_fixture", "approved_fixture"], subagents=False, tool_name="js_eval")
    stack = [EventLogMiddleware(agent="helper"), dispatch, policy, interpreter]
    dispatch.configure(stack, {"approved_fixture": True})
    child_model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "js_eval", "args": {"code": "await tools.firstFixture({}); await tools.approvedFixture({})"}, "id": "js-call"}]),
        AIMessage(content="Child completed"),
    ])
    spec = {"name": "helper", "description": "Fixture specialist"}
    if precompiled:
        spec["runnable"] = create_agent(child_model, [first_fixture, approved_fixture], middleware=stack)
    else:
        spec.update({"model": child_model, "tools": [first_fixture, approved_fixture], "middleware": stack})
    parent = create_deep_agent(
        ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "task", "id": "delegation", "args": {"subagent_type": "helper", "description": "Run harmless effects"}}]),
            AIMessage(content="Parent completed"),
        ]), middleware=[EventLogMiddleware(agent="chief")],
        subagents=[spec], checkpointer=InMemorySaver(),
    )
    # Production's effective_manifest binds the actual SDK-generated/copy
    # graphs after parent compilation, rather than a pre-copy child object.
    dispatch.bind(child_graphs(tool_node(parent))["helper"])
    config = {"configurable": {"thread_id": "js-inline-thread"}}

    async def exercise():
        initial = {"messages": [HumanMessage(content="Delegate fixtures")]}
        paused = await parent.ainvoke(initial, config) if asynchronous else parent.invoke(initial, config)
        assert calls == ["first"]
        (record,) = store.list_tasks()
        assert record.state == "waiting_approval"
        pending = paused["__interrupt__"][0]
        assert {item.id for item in paused["__interrupt__"]} == {pending.id}
        assert pending.value["type"] == "tool_approval"
        assert pending.value["name"] == "approved_fixture"
        assert record.checkpoint["interrupts"] == {pending.id: pending.value}
        response = Command(resume={pending.id: {"type": "approve"}})
        result = await parent.ainvoke(response, config) if asynchronous else parent.invoke(response, config)
        assert result["messages"][-1].content == "Parent completed"
        assert calls == ["first", "approved"]
        final = store.get_task(record.id)
        assert final.state == "done" and not final.checkpoint
        assert len(store.list_tasks()) == 1

    asyncio.run(exercise())


def test_checkpoint_context_normalization_preserves_explicit_replay_and_original_configs():
    from manta_code.runtime import _normalize_checkpoint_context

    graph = SimpleNamespace(_own_checkpoint_config=lambda config: config)
    _normalize_checkpoint_context(graph)
    installed = graph._own_checkpoint_config
    _normalize_checkpoint_context(graph)
    assert graph._own_checkpoint_config is installed
    null_config = {"configurable": {"checkpoint_id": None, "thread_id": "thread", "checkpoint_ns": "child"}}
    assert installed(null_config) == {"configurable": {"thread_id": "thread", "checkpoint_ns": "child"}}
    assert null_config["configurable"]["checkpoint_id"] is None
    replay = {"configurable": {"checkpoint_id": "explicit-checkpoint", "thread_id": "thread"}}
    assert installed(replay) is replay
    latest = {"configurable": {"thread_id": "thread"}}
    assert installed(latest) is latest


def test_checkpoint_context_boundary_survives_real_graph_recursion_and_subagent_copies():
    from langchain.agents import create_agent
    from manta_code.runtime import _normalize_checkpoint_context

    graph = create_agent(ScriptedModel(responses=[AIMessage(content="Done")]))
    _normalize_checkpoint_context(graph)
    for copied in (graph.copy({"config": {"recursion_limit": 100}}), graph.with_config(run_name="helper")):
        assert copied is not graph
        assert copied._own_checkpoint_config({"configurable": {"checkpoint_id": None}}) == {"configurable": {}}
        twice = copied.with_config(run_name="nested")
        assert twice._own_checkpoint_config({"configurable": {"checkpoint_id": None}}) == {"configurable": {}}
        replay = {"configurable": {"checkpoint_id": "explicit-checkpoint"}}
        assert twice._own_checkpoint_config(replay) is replay


def test_checkpoint_context_normalization_keeps_intentional_graph_replay():
    from typing import TypedDict
    from langgraph.func import task
    from langgraph.graph import END, START, StateGraph
    from manta_code.runtime import _normalize_checkpoint_context

    class State(TypedDict):
        value: int

    calls = []

    @task
    def effect():
        calls.append("ran")
        return len(calls)

    def run(state):
        return {"value": effect().result()}

    builder = StateGraph(State)
    builder.add_node("run", run)
    builder.add_edge(START, "run")
    builder.add_edge("run", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    _normalize_checkpoint_context(graph)
    config = {"configurable": {"thread_id": "intentional-replay"}}
    assert graph.invoke({"value": 0}, config) == {"value": 1}
    before_effect = next(snapshot for snapshot in graph.get_state_history(config) if snapshot.next == ("run",))
    assert before_effect.config["configurable"]["checkpoint_id"]
    assert graph.invoke(None, before_effect.config) == {"value": 2}
    assert calls == ["ran", "ran"]


def test_actual_server_restart_nested_js_preserves_completed_effect_and_inline_identity(isolated, monkeypatch):
    """A new server reuses SQLite child task results rather than replaying JS effects."""
    import json
    from uuid import uuid4
    from deepagents_code import config as upstream_config, sessions
    from deepagents_code.client.launch.server import ServerProcess, generate_langgraph_json
    from deepagents_code.client.launch import server as server_module
    from deepagents_code.client.launch.server_manager import _write_checkpointer
    from deepagents_code.client.remote_client import RemoteAgent
    from deepagents_code.thread_ownership import OWNER_KEY, held_lease
    from manta_code.budget_runtime import pending_requests
    from manta_code._boot import allow_blocking_server

    monkeypatch.setattr(server_module, "_build_server_cmd", server_module._build_server_cmd)
    assert allow_blocking_server()

    db_path, effect_path = isolated / "sessions.db", isolated / "effects.txt"
    monkeypatch.setattr(sessions, "_db_path", db_path)
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(db_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(isolated / "deepagents"))
    monkeypatch.setenv("MANTA_TEAM_EFFECT_PATH", str(effect_path))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    selectors = upstream_config._langsmith_selectors_from({})
    monkeypatch.setattr(upstream_config, "_bootstrap_state", upstream_config._BootstrapState(
        done=True, launch_langsmith_env=selectors, user_langsmith_env=selectors))
    source = r'''
import os
from pathlib import Path
from deepagents import create_deep_agent
from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from manta_code.middleware.policy import ToolPolicyMiddleware
from manta_code.runtime import RuntimeDispatchMiddleware, child_graphs, tool_node
from manta_code.tasks.events import EventLogMiddleware

class Model(FakeMessagesListChatModel):
    child: bool = False
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        identity = "js-call" if self.child else "delegation"
        if any(isinstance(message, ToolMessage) and message.tool_call_id == identity for message in messages):
            message = AIMessage(content="Child completed" if self.child else "Parent completed")
        elif self.child:
            message = AIMessage(content="", tool_calls=[{"name": "js_eval", "id": identity,
                "args": {"code": "await tools.firstFixture({}); await tools.approvedFixture({})"}}])
        else:
            message = AIMessage(content="", tool_calls=[{"name": "task", "id": identity,
                "args": {"subagent_type": "helper", "description": "Run the harmless effects"}}])
        return ChatResult(generations=[ChatGeneration(message=message)])

@tool
def first_fixture() -> str:
    """Record one harmless completed effect."""
    with Path(os.environ["MANTA_TEAM_EFFECT_PATH"]).open("a") as handle:
        handle.write("first\n")
    return "first"

@tool
def approved_fixture() -> str:
    """Record one harmless approved effect."""
    with Path(os.environ["MANTA_TEAM_EFFECT_PATH"]).open("a") as handle:
        handle.write("approved\n")
    return "approved"

dispatch = RuntimeDispatchMiddleware(agent="helper")
stack = [EventLogMiddleware(agent="helper"), dispatch, ToolPolicyMiddleware(ask=["approved_fixture"]),
    CostAwareCodeInterpreterMiddleware(ptc=["first_fixture", "approved_fixture"], subagents=False, tool_name="js_eval")]
dispatch.configure(stack, {"approved_fixture": True})
graph = create_deep_agent(Model(responses=[], child=False), middleware=[EventLogMiddleware(agent="chief")], subagents=[{
    "name": "helper", "description": "Harmless helper", "model": Model(responses=[], child=True),
    "tools": [first_fixture, approved_fixture], "middleware": stack,
}])
dispatch.bind(child_graphs(tool_node(graph))["helper"])
'''
    work_dirs = []
    for index in range(2):
        work_dir = isolated / f"server-{index}"
        work_dir.mkdir()
        (work_dir / "graph.py").write_text(source, encoding="utf-8")
        _write_checkpointer(work_dir)
        generate_langgraph_json(work_dir, graph_ref="./graph.py:graph",
                                checkpointer_path="./checkpointer.py:create_checkpointer")
        work_dirs.append(work_dir)
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async def stream(agent, value):
        prepared = agent._prepare_mutation(config)
        token = prepared["configurable"].pop(OWNER_KEY)
        async for _ in agent._get_graph().astream(
            value, config=prepared, stream_mode=["updates"], headers={OWNER_KEY: token},
        ):
            pass

    async def exercise():
        servers = [ServerProcess(config_dir=path) for path in work_dirs]
        try:
            await servers[0].start()
            await servers[0].wait_for_graph_ready("agent")
            first = RemoteAgent(servers[0].url)
            await first.aensure_thread(config)
            await stream(first, {"messages": [HumanMessage(content="Delegate fixtures")]})
            requests = pending_requests(await first.aget_state(config))
            assert len(requests) == 1
            identity, request = next(iter(requests.items()))
            assert request["type"] == "tool_approval" and request["name"] == "approved_fixture"
            assert effect_path.read_text() == "first\n"
            (child,) = store.list_tasks()
            assert child.state == "waiting_approval" and child.checkpoint["interrupts"] == requests
            servers[0].stop()

            await servers[1].start()
            await servers[1].wait_for_graph_ready("agent")
            second = RemoteAgent(servers[1].url)
            await second.aensure_thread(config)
            assert pending_requests(await second.aget_state(config)) == json.loads(json.dumps(requests))
            await stream(second, Command(resume={identity: {"type": "approve"}}))
            finished = await second.aget_state(config)
            assert finished.values["messages"][-1]["content"] == "Parent completed"
            assert not finished.next and not pending_requests(finished)
            assert effect_path.read_text() == "first\napproved\n"
            (final,) = store.list_tasks()
            assert final.id == child.id and final.state == "done" and not final.checkpoint
        finally:
            for server in servers:
                server.stop()
            if lease := held_lease(thread_id):
                lease.release()

    asyncio.run(exercise())


@pytest.mark.parametrize("matching", [True, False])
def test_command_wrapped_backend_receipts_require_exact_tool_call_id(matching):
    record = store.create_task(store.TaskRecord(id="inline", agent="helper", prompt="check", execution_kind="inline", state="running"))
    token = inline.CURRENT_INLINE_TASK.set(record.id)
    try:
        request = SimpleNamespace(tool_call={"name": "execute", "id": "test-call", "args": {"command": "python -m pytest -q"}}, tool=None)
        result = Command(update={"messages": [ToolMessage(
            content="actual backend output", tool_call_id="test-call" if matching else "other-call",
            artifact={"exit_code": 1}, status="error",
        )]})
        EventLogMiddleware(agent="helper").wrap_tool_call(request, lambda _: result)
        tests = store.get_task(record.id).structured_result.get("tests", [])
        if matching:
            assert len(tests) == 1 and tests[0]["exit_code"] == 1
            assert any(event.kind == "tool_error" for event in store.recent_events(task_id=record.id))
        else:
            assert not tests
    finally:
        inline.CURRENT_INLINE_TASK.reset(token)


def test_command_wrapped_denial_is_audited_without_receipt_or_private_content():
    record = store.create_task(store.TaskRecord(id="inline", agent="helper", prompt="check", execution_kind="inline", state="running"))
    token = inline.CURRENT_INLINE_TASK.set(record.id)
    try:
        request = SimpleNamespace(tool_call={"name": "execute", "id": "test-call", "args": {"command": "python -m pytest -q"}}, tool=None)
        result = Command(update={"messages": [ToolMessage(content="Blocked by Manta tool policy: private details", tool_call_id="test-call", status="error")]})
        EventLogMiddleware(agent="helper").wrap_tool_call(request, lambda _: result)
        (event,) = store.recent_events(task_id=record.id)
        assert event.kind == "denied" and event.detail == "execute"
        assert not store.get_task(record.id).structured_result.get("tests")
    finally:
        inline.CURRENT_INLINE_TASK.reset(token)


def test_actual_server_hooks_compiled_tool_retains_observed_exit_receipt(isolated):
    from deepagents_code.hooks.server_middleware import ServerHooksMiddleware
    from langchain.agents import create_agent

    record = store.create_task(store.TaskRecord(id="inline", agent="helper", prompt="check", execution_kind="inline", state="running"))

    @tool(response_format="content_and_artifact")
    def execute(command: str) -> tuple[str, dict]:
        """Return a harmless backend result with an observed nonzero exit."""
        return "fixture backend test failed", {"exit_code": 2}

    graph = create_agent(ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "execute-call", "args": {"command": "python -m pytest -q"}}]),
        AIMessage(content="Reported failure"),
    ]), [execute], middleware=[EventLogMiddleware(agent="helper"), ServerHooksMiddleware(cwd=isolated, emit_stop=False)])
    token = inline.CURRENT_INLINE_TASK.set(record.id)
    try:
        result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Run fixture")]}))
        assert result["messages"][-1].content == "Reported failure"
        tests = store.get_task(record.id).structured_result["tests"]
        assert len(tests) == 1 and tests[0]["exit_code"] == 2
        assert tests[0]["output"] == "fixture backend test failed"
    finally:
        inline.CURRENT_INLINE_TASK.reset(token)


@pytest.mark.parametrize("command", ["echo hello && python -m pytest", "python -m pytest || true", "python -m pytest; echo passed"])
def test_shell_group_exit_code_does_not_prove_the_test_subcommand(command):
    record = store.create_task(store.TaskRecord(id="inline", agent="helper", prompt="check", execution_kind="inline", state="running"))
    token = inline.CURRENT_INLINE_TASK.set(record.id)
    try:
        request = SimpleNamespace(tool_call={"name": "execute", "id": "test", "args": {"command": command}}, tool=None)
        result = ToolMessage(content="tests passed", tool_call_id="test", artifact={"exit_code": 0})
        EventLogMiddleware(agent="helper").wrap_tool_call(request, lambda _: result)
        assert not store.get_task(record.id).structured_result.get("tests")
    finally:
        inline.CURRENT_INLINE_TASK.reset(token)
