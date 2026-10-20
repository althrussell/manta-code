from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from manta_code import budget_runtime


@pytest.fixture
def client(monkeypatch):
    from deepagents_code.client import non_interactive as client
    for name in ("_process_hitl_interrupts", "_process_interrupts", "_stream_agent", "generate_thread_id"):
        monkeypatch.setattr(client, name, getattr(client, name))
    # Other tests/doctor may have installed an adapter already. Remove only its
    # marker so this isolated instance can wrap the stubbed upstream functions.
    async def original_process(*a):
        pytest.fail("Budget approval reached upstream automatic decisions")
    async def original_stream(*args):
        return args[1]
    monkeypatch.setattr(client, "_process_hitl_interrupts", original_process)
    monkeypatch.setattr(client, "_stream_agent", original_stream)
    return client


def test_unattended_budget_pause_is_durable_and_never_auto_approved(client, tmp_path, monkeypatch):
    from manta_code.tasks import store
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("MANTA_TASK_ID", "task")
    store.create_task(store.TaskRecord(id="task", agent="swe", prompt="work", state="running"))
    budget_runtime.install_budget_runtime()
    state = SimpleNamespace(thread_id="thread", pending_interrupts={"interrupt": {
        "action_requests": [{"name": "manta_budget_continue", "description": "cap reached"}],
    }})
    console = SimpleNamespace(print=lambda *a: None)
    with pytest.raises(budget_runtime.BudgetPaused):
        asyncio.run(client._process_hitl_interrupts(state, console))
    record = store.get_task("task")
    assert record.state == "paused_budget"
    assert record.thread_id == "thread"
    assert record.checkpoint["resume_value"] == {"interrupt": {"decisions": [{"type": "approve"}]}}


def test_resume_uses_saved_graph_without_replaying_prompt(client, monkeypatch):
    monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "saved")
    monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps({"id": {"decisions": [{"type": "approve"}]}}))
    monkeypatch.delenv("MANTA_TASK_ID", raising=False)
    calls = []
    async def ensure_thread(config):
        calls.append("register")
    async def get_state(config):
        calls.append("state")
        return SimpleNamespace(values={"messages": ["existing"]}, next=("model",),
            interrupts=(SimpleNamespace(id="id", value={"action_requests": [
                {"name": "manta_budget_continue"}]}),))
    budget_runtime.install_budget_runtime()
    assert client.generate_thread_id() == "saved"
    result = asyncio.run(client._stream_agent(SimpleNamespace(aget_state=get_state,
        aensure_thread=ensure_thread),
        {"messages": ["must not replay"]}, {"configurable": {"thread_id": "saved"}},
        SimpleNamespace(), None, None, None))
    assert result.resume == {"id": {"decisions": [{"type": "approve"}]}}
    assert calls == ["register", "state"]


def test_missing_checkpoint_cannot_resume_as_new_task(client, monkeypatch):
    monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "missing")
    monkeypatch.setenv("MANTA_RESUME_VALUE", '{"id":{"decisions":[{"type":"approve"}]}}')
    monkeypatch.delenv("MANTA_TASK_ID", raising=False)
    async def get_state(config):
        return SimpleNamespace(values={}, next=())
    async def ensure_thread(config):
        pass
    budget_runtime.install_budget_runtime()
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(client._stream_agent(SimpleNamespace(aget_state=get_state,
            aensure_thread=ensure_thread), {}, {}, None, None, None, None))


@pytest.mark.parametrize("interrupt_id,action", [
    ("changed", "manta_budget_continue"), ("id", "execute"),
])
def test_resume_refuses_stale_or_unrelated_interrupts(client, monkeypatch, interrupt_id, action):
    monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "saved")
    monkeypatch.setenv("MANTA_RESUME_VALUE", '{"id":{"decisions":[{"type":"approve"}]}}')
    monkeypatch.delenv("MANTA_TASK_ID", raising=False)
    async def ensure_thread(config):
        pass
    async def get_state(config):
        return SimpleNamespace(values={"messages": ["existing"]}, next=("model",),
            tasks=(SimpleNamespace(interrupts=(SimpleNamespace(id=interrupt_id,
                value={"action_requests": [{"name": action}]}),)),))
    budget_runtime.install_budget_runtime()
    with pytest.raises(ValueError, match="nothing resumed"):
        asyncio.run(client._stream_agent(SimpleNamespace(aget_state=get_state,
            aensure_thread=ensure_thread), {}, {}, None, None, None, None))


def test_resume_after_actual_server_restart_keeps_sqlite_checkpoint(client, tmp_path, monkeypatch):
    """Exercise HTTP registration and SQLite persistence across real processes."""
    from deepagents_code import config as upstream_config, sessions
    from deepagents_code.client.launch.server import ServerProcess, generate_langgraph_json
    from deepagents_code.client.launch.server_manager import _write_checkpointer
    from deepagents_code.client.remote_client import RemoteAgent
    from deepagents_code.thread_ownership import OWNER_KEY, held_lease

    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(sessions, "_db_path", db_path)
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(db_path))
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path / "deepagents"))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("MANTA_TASK_ID", raising=False)
    selectors = upstream_config._langsmith_selectors_from({})
    monkeypatch.setattr(upstream_config, "_bootstrap_state", upstream_config._BootstrapState(
        done=True, launch_langsmith_env=selectors, user_langsmith_env=selectors))
    work_dir = tmp_path / "server"
    work_dir.mkdir()
    (work_dir / "graph.py").write_text('''\
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.config import get_config
from manta_code.middleware.economy import TokenEconomyMiddleware

class State(TypedDict):
    prompt: str
    reads: int
    result: str

def read(state):
    return {"reads": state.get("reads", 0) + 1}

def pause(state):
    thread_id = get_config()["configurable"]["thread_id"]
    governor = TokenEconomyMiddleware(agent="restart-fixture", max_tokens=1)
    governor._totals[thread_id] = {"tokens": 1.0, "usd": 0.0, "unknown_calls": 0.0}
    assert governor._maybe_pause(thread_id) is None
    return {"result": state["prompt"]}

builder = StateGraph(State)
builder.add_node("read", read)
builder.add_node("pause", pause)
builder.add_edge(START, "read")
builder.add_edge("read", "pause")
builder.add_edge("pause", END)
graph = builder.compile()
''', encoding="utf-8")
    _write_checkpointer(work_dir)
    generate_langgraph_json(work_dir, graph_ref="./graph.py:graph",
        checkpointer_path="./checkpointer.py:create_checkpointer")
    second_work_dir = tmp_path / "restarted-server"
    second_work_dir.mkdir()
    for name in ("graph.py", "checkpointer.py", "langgraph.json"):
        (second_work_dir / name).write_bytes((work_dir / name).read_bytes())
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async def stream_raw(agent, value, config):
        prepared = agent._prepare_mutation(config)
        token = prepared["configurable"].pop(OWNER_KEY)
        async for _ in agent._get_graph().astream(value, config=prepared,
                stream_mode=["updates"], headers={OWNER_KEY: token}):
            pass

    async def run():
        first_server = ServerProcess(config_dir=work_dir)
        second_server = ServerProcess(config_dir=second_work_dir)
        try:
            await first_server.start()
            await first_server.wait_for_graph_ready("agent")
            first = RemoteAgent(first_server.url)
            await first.aensure_thread(config)
            await stream_raw(first, {"prompt": "original prompt", "reads": 0}, config)
            snapshot = await first.aget_state(config)
            pending = budget_runtime._pending_budget_interrupt_ids(snapshot)
            assert len(pending) == 1
            first_server.stop()

            await second_server.start()
            await second_server.wait_for_graph_ready("agent")
            second = RemoteAgent(second_server.url)
            # The live thread row disappeared; its SQLite checkpoint did not.
            assert await second.aget_state(config) is None
            monkeypatch.setenv("MANTA_RESUME_THREAD_ID", thread_id)
            monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps({
                next(iter(pending)): {"decisions": [{"type": "approve"}]}}))
            async def original_stream(agent, value, config, *_):
                assert value.resume
                await stream_raw(agent, value, config)
            monkeypatch.setattr(client, "_stream_agent", original_stream)
            budget_runtime.install_budget_runtime()
            await client._stream_agent(second, {"prompt": "must not replay", "reads": 0},
                config, None, None, None, None)
            finished = await second.aget_state(config)
            assert finished.values == {"prompt": "original prompt", "reads": 1,
                "result": "original prompt"}
            assert not finished.next
            assert not budget_runtime._pending_budget_interrupt_ids(finished)
        finally:
            first_server.stop()
            second_server.stop()
            if lease := held_lease(thread_id):
                lease.release()

    asyncio.run(run())
