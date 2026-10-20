"""Human task decisions survive checkpoints without granting ambient consent."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from manta_code import budget_runtime
from manta_code.tasks import approvals, executor, store


def action(name="write_file", **args):
    return {"action_requests": [{"name": name, "args": args, "description": name}],
            "review_configs": [{"action_name": name, "allowed_decisions": ["approve", "reject"]}]}


def per_call(name="write_file", **args):
    return {"type": "tool_approval", "tool_call_id": "tool-call-one", "name": name,
            "args": args, "description": "Review the exact tool call"}


@pytest.fixture
def task_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    for key in ("MANTA_TASK_DB_PATH", "MANTA_TASK_ID", "MANTA_RESUME_THREAD_ID", "MANTA_RESUME_VALUE", "MANTA_RESUME_PENDING"):
        monkeypatch.delenv(key, raising=False)
    record = store.create_task(store.TaskRecord(id="task", agent="swe", prompt="original prompt", state="running"))
    spawned = []

    def spawn(record, **kwargs):
        spawned.append((copy.deepcopy(record), copy.deepcopy(kwargs)))
        return store.get_task(record.id)

    monkeypatch.setattr(executor, "_spawn_task", spawn)
    monkeypatch.setattr(executor, "validate_execution_manifest", lambda _: None)
    return record, spawned


@pytest.fixture
def runtime_client(monkeypatch):
    from deepagents_code.client import non_interactive as client

    monkeypatch.setattr(client, "_process_interrupts", client._process_interrupts)
    monkeypatch.setattr(client, "generate_thread_id", lambda: "new-thread")

    async def fail_process(*args):
        pytest.fail("Managed task decisions reached upstream automatic approval")

    async def capture_stream(agent, value, config, *args):
        return value

    monkeypatch.setattr(client, "_process_hitl_interrupts", fail_process)
    monkeypatch.setattr(client, "_stream_agent", capture_stream)
    return client


def test_pending_decision_exact_token_is_one_use_and_covers_every_displayed_action(task_state):
    _, spawned = task_state
    requests = {"a": action("write_file", path="a.py"), "b": action("delete_file", path="old.py")}
    checkpoint = approvals.checkpoint_requests("task", "thread", requests)
    assert store.get_task("task").state == "waiting_approval"
    # Mutating the client stream object cannot mutate the persisted consent scope.
    requests["a"]["action_requests"][0]["args"]["path"] = "different.py"
    with pytest.raises(executor.TaskError, match="pending request changed"):
        approvals.respond_task("task", pending_id="stale", decision="approve")
    assert not spawned
    assert store.get_task("task").checkpoint["interrupts"]["a"]["action_requests"][0]["args"]["path"] == "a.py"
    result = approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="approve")
    assert result.id == "task" and result.state == "starting"
    assert spawned[0][1]["resume_value"] == {
        "a": {"decisions": [{"type": "approve"}]}, "b": {"decisions": [{"type": "approve"}]},
    }
    assert spawned[0][0].thread_id == "thread"
    with pytest.raises(executor.TaskError, match="no longer waiting"):
        approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="approve")
    assert len(spawned) == 1
    decisions = [event for event in store.recent_events(task_id="task") if event.kind == "task_decision"]
    assert len(decisions) == 1 and decisions[0].detail == "approve"


def test_pending_group_cas_rejects_replacement_during_manifest_validation(task_state, monkeypatch):
    _, spawned = task_state
    old = approvals.checkpoint_requests("task", "thread", {"a": action("write_file", path="a.py")})

    def changed(_):
        store.update_task("task", checkpoint={"pending_id": "new-token", "interrupts": {"b": action("delete_file", path="old.py")}})

    monkeypatch.setattr(executor, "validate_execution_manifest", changed)
    with pytest.raises(executor.TaskError, match="changed while resuming"):
        approvals.respond_task("task", pending_id=old["pending_id"], decision="approve")
    assert not spawned
    record = store.get_task("task")
    assert record.state == "waiting_approval"
    assert record.checkpoint["pending_id"] == "new-token"


def test_cancelled_task_wins_against_late_pause_and_cannot_be_approved(task_state):
    _, spawned = task_state
    store.update_task("task", state="cancelled")
    checkpoint = approvals.checkpoint_requests("task", "thread", {"a": action()})
    assert store.get_task("task").state == "cancelled"
    with pytest.raises(executor.TaskError, match="no longer waiting"):
        approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="approve")
    assert not spawned


def test_mixed_groups_require_explicit_exact_interrupt_decisions():
    requests = {
        "input": action(approvals.INPUT_ACTION, question="Which API?"),
        "edit": action("write_file", path="a.py"),
    }
    with pytest.raises(ValueError, match="Answer this input"):
        approvals.decision_mapping(requests, "approve")
    with pytest.raises(ValueError, match="JSON object keyed by every interrupt"):
        approvals.decision_mapping(requests, "answer", "v1")
    result = approvals.decision_mapping(requests, "answer", json.dumps({
        "input": "Keep v1", "edit": {"decision": "approve"},
    }))
    assert result == {
        "input": {"status": "answered", "answer": "Keep v1", "answers": ["Keep v1"]},
        "edit": {"decisions": [{"type": "approve"}]},
    }
    with pytest.raises(ValueError, match="JSON object keyed by every interrupt"):
        approvals.decision_mapping(requests, "answer", json.dumps({"input": "v1", "wrong-id": "approve"}))
    same_interrupt = {"mixed": {"action_requests": [
        {"name": approvals.INPUT_ACTION, "args": {"question": "Which API?"}},
        {"name": "write_file", "args": {"path": "a.py"}},
    ]}}
    with pytest.raises(ValueError, match="interactive review"):
        approvals.decision_mapping(same_interrupt, "answer", "v1")


@pytest.mark.parametrize("native", [False, True])
def test_input_text_cancel_and_invalid_answers_preserve_pending_state(task_state, native):
    _, spawned = task_state
    request = ({"type": "ask_user", "questions": [{"question": "Which API?", "type": "text"}]}
               if native else action(approvals.INPUT_ACTION, question="Which API?"))
    checkpoint = approvals.checkpoint_requests("task", "thread", {"input": request})
    assert store.get_task("task").state == "waiting_input"
    with pytest.raises(executor.TaskError, match="non-empty"):
        approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="answer", value=" ")
    assert not spawned and store.get_task("task").state == "waiting_input"
    approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="cancel")
    assert spawned[0][1]["resume_value"] == {"input": {"status": "cancelled", "answer": "", "answers": []}}


def test_native_structured_questions_stay_in_the_owning_conversation():
    request = {"native": {"type": "ask_user", "questions": [{"question": "Choose", "type": "select", "options": ["a", "b"]}]}}
    with pytest.raises(ValueError, match="owning conversation"):
        approvals.decision_mapping(request, "answer", "a")


@pytest.mark.parametrize("pending,expected_state", [
    (action("write_file", path="a.py"), "waiting_approval"),
    (action(approvals.INPUT_ACTION, question="Which API?"), "waiting_input"),
    (action("manta_budget_continue", max_tokens=1), "paused_budget"),
])
def test_managed_headless_requests_checkpoint_before_autoapproval(task_state, runtime_client, monkeypatch, pending, expected_state):
    _, spawned = task_state
    monkeypatch.setenv("MANTA_TASK_ID", "task")
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_AUTO_APPROVE", "true")
    budget_runtime.install_budget_runtime()
    state = SimpleNamespace(thread_id="thread", pending_interrupts={"interrupt": pending})
    with pytest.raises(budget_runtime.TaskPaused):
        asyncio.run(runtime_client._process_hitl_interrupts(state, SimpleNamespace(print=lambda *_: None)))
    record = store.get_task("task")
    assert record.state == expected_state and record.thread_id == "thread"
    assert record.checkpoint["pending_id"]
    assert record.checkpoint["interrupts"] == {"interrupt": pending}
    assert not spawned


@pytest.mark.parametrize("actual", [
    {"same-id": action("write_file", path="changed.py")},
    {"different-id": action("write_file", path="a.py")},
])
def test_resume_rejects_same_interrupt_id_with_changed_content(task_state, runtime_client, monkeypatch, actual):
    expected = {"same-id": action("write_file", path="a.py")}
    monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "thread")
    monkeypatch.setenv("MANTA_RESUME_PENDING", json.dumps(expected))
    monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps({"same-id": {"decisions": [{"type": "approve"}]}}))

    async def ensure(config):
        pass

    async def snapshot(config):
        return SimpleNamespace(values={"read": "preserved"}, interrupts=tuple(
            SimpleNamespace(id=key, value=value) for key, value in actual.items()
        ), tasks=())

    budget_runtime.install_budget_runtime()
    with pytest.raises(ValueError, match="pending checkpoint content; nothing resumed"):
        asyncio.run(runtime_client._stream_agent(
            SimpleNamespace(aensure_thread=ensure, aget_state=snapshot), {"prompt": "do not replay"},
            {"configurable": {"thread_id": "thread"}}, None, None, None, None,
        ))


def test_legacy_budget_checkpoint_requires_explicit_resume_and_preserves_mapping(task_state):
    _, spawned = task_state
    saved = {"budget-id": {"decisions": [{"type": "approve"}]}}
    store.pause_task("task", "Budget exceeded", thread_id="thread", checkpoint={"resume_value": saved})
    assert store.get_task("task").state == "paused_budget" and not spawned
    result = executor.resume_task("task")
    assert result.id == "task" and result.thread_id == "thread"
    assert spawned[0][1]["resume_value"] == saved


def test_pending_reader_rejects_conflicting_duplicate_interrupt_content():
    snapshot = SimpleNamespace(
        interrupts=(SimpleNamespace(id="same", value=action("write_file", path="a.py")),),
        tasks=(SimpleNamespace(interrupts=(SimpleNamespace(id="same", value=action("write_file", path="b.py")),)),),
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        budget_runtime.pending_requests(snapshot)


@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
def test_raw_per_call_decision_preserves_raw_identity_and_uses_single_decision(task_state, decision):
    _, spawned = task_state
    raw = per_call(path="a.py")
    checkpoint = approvals.checkpoint_requests("task", "thread", {"raw-id": raw})
    record = store.get_task("task")
    assert record.state == "waiting_approval" and record.checkpoint["interrupts"] == {"raw-id": raw}
    assert "Review the exact tool call" in record.pending_reason
    approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision=decision)
    assert spawned[0][1]["resume_value"] == {"raw-id": {"type": "approve" if decision == "approve" else "reject"}}


def test_raw_per_call_mixed_with_input_needs_explicit_decisions():
    requests = {"raw": per_call(path="a.py"), "input": action(approvals.INPUT_ACTION, question="Which API?")}
    with pytest.raises(ValueError, match="Answer this input"):
        approvals.decision_mapping(requests, "approve")
    assert approvals.decision_mapping(requests, "answer", '{"raw":{"decision":"reject"},"input":"v1"}') == {
        "raw": {"type": "reject"}, "input": {"status": "answered", "answer": "v1", "answers": ["v1"]},
    }


def test_raw_tool_and_budget_group_never_inherits_budget_resume_shortcut(task_state):
    _, spawned = task_state
    checkpoint = approvals.checkpoint_requests("task", "thread", {
        "raw": per_call(path="a.py"), "budget": action("manta_budget_continue", max_tokens=1),
    })
    assert store.get_task("task").state == "waiting_approval"
    assert "resume_value" not in checkpoint
    with pytest.raises(executor.TaskError, match="explicit decision"):
        executor.resume_task("task")
    assert not spawned


def test_raw_extraction_precedes_upstream_malformed_rejection_and_is_idempotent(task_state, runtime_client, monkeypatch):
    monkeypatch.setenv("MANTA_TASK_ID", "task")
    delegated = []

    def original(data, state, console):
        delegated.append(data)

    monkeypatch.setattr(runtime_client, "_process_interrupts", original)
    assert budget_runtime.install_budget_runtime()
    installed = runtime_client._process_interrupts
    assert budget_runtime.install_budget_runtime() and runtime_client._process_interrupts is installed
    raw = per_call(path="a.py")
    native = {"type": "ask_user", "questions": [{"question": "Which API?", "type": "text"}]}
    batched, hook = action(), {"opaque": "upstream hook handling"}
    state = SimpleNamespace(thread_id="thread", pending_interrupts={}, interrupt_occurred=False, hitl_response={})
    untouched = [SimpleNamespace(id="batch", value=batched), SimpleNamespace(id="hook", value=hook)]
    data = {"__interrupt__": [SimpleNamespace(id="raw", value=raw), SimpleNamespace(id="native", value=native), *untouched]}
    runtime_client._process_interrupts(data, state, SimpleNamespace(print=lambda *_: None))
    assert state.pending_interrupts == {"raw": raw, "native": native}
    assert state.interrupt_occurred and not state.hitl_response
    assert delegated == [{"__interrupt__": untouched}]
    assert len(data["__interrupt__"]) == 4
    with pytest.raises(budget_runtime.TaskPaused):
        asyncio.run(runtime_client._process_hitl_interrupts(state, SimpleNamespace(print=lambda *_: None)))
    assert store.get_task("task").checkpoint["interrupts"] == {"raw": raw, "native": native}


def test_unmanaged_raw_interrupts_keep_upstream_behavior(task_state, runtime_client, monkeypatch):
    delegated = []
    monkeypatch.setattr(runtime_client, "_process_interrupts", lambda *args: delegated.append(args))
    budget_runtime.install_budget_runtime()
    data = {"__interrupt__": [SimpleNamespace(id="raw", value=per_call(path="a.py"))]}
    state, console = SimpleNamespace(), SimpleNamespace()
    runtime_client._process_interrupts(data, state, console)
    assert delegated == [(data, state, console)]


def test_raw_per_call_runtime_validates_exact_tool_call_id_and_args(task_state, runtime_client, monkeypatch):
    expected = {"raw": per_call(path="a.py")}
    actual = copy.deepcopy(expected)
    actual["raw"]["tool_call_id"] = "different-tool-call"
    monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "thread")
    monkeypatch.setenv("MANTA_RESUME_PENDING", json.dumps(expected))
    monkeypatch.setenv("MANTA_RESUME_VALUE", '{"raw":{"type":"approve"}}')

    async def ensure(config):
        pass

    async def snapshot(config):
        return SimpleNamespace(values={"saved": True}, interrupts=(SimpleNamespace(id="raw", value=actual["raw"]),), tasks=())

    budget_runtime.install_budget_runtime()
    with pytest.raises(ValueError, match="pending checkpoint content"):
        asyncio.run(runtime_client._stream_agent(SimpleNamespace(aensure_thread=ensure, aget_state=snapshot), {},
                                               {"configurable": {"thread_id": "thread"}}, None, None, None, None))


def test_actual_server_restarts_keep_input_then_approval_without_repeating_effects(
    task_state, runtime_client, tmp_path, monkeypatch,
):
    """Three fresh HTTP servers share SQLite, one task/thread and two decisions."""
    from deepagents_code import config as upstream_config, sessions
    from deepagents_code.client.launch.server import ServerProcess, generate_langgraph_json
    from deepagents_code.client.launch.server_manager import _write_checkpointer
    from deepagents_code.client.remote_client import RemoteAgent
    from deepagents_code.thread_ownership import OWNER_KEY, held_lease

    _, spawned = task_state
    db_path = tmp_path / "sessions.db"
    effect_path = tmp_path / "effects.txt"
    monkeypatch.setattr(sessions, "_db_path", db_path)
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(db_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path / "deepagents"))
    monkeypatch.setenv("MANTA_TASK_ID", "task")
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    monkeypatch.setenv("MANTA_TEAM_EFFECT_PATH", str(effect_path))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    selectors = upstream_config._langsmith_selectors_from({})
    monkeypatch.setattr(upstream_config, "_bootstrap_state", upstream_config._BootstrapState(
        done=True, launch_langsmith_env=selectors, user_langsmith_env=selectors))
    graph_source = '''\
import os
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from manta_code.tasks.approvals import manta_request_input

class State(TypedDict):
    prompt: str
    reads: int
    answer: str
    writes: int
    result: str

def read(state):
    with Path(os.environ["MANTA_TEAM_EFFECT_PATH"]).open("a") as handle:
        handle.write("read\\n")
    return {"reads": state.get("reads", 0) + 1}

def ask(state):
    return {"answer": manta_request_input("Which API should remain compatible?")}

def approval(state):
    decision = interrupt({
        "action_requests": [{"name": "fixture_write", "args": {"answer": state["answer"]}, "description": "Save the reviewed answer"}],
        "review_configs": [{"action_name": "fixture_write", "allowed_decisions": ["approve", "reject"]}],
    })
    if decision["decisions"][0]["type"] != "approve":
        return {"result": "rejected"}
    with Path(os.environ["MANTA_TEAM_EFFECT_PATH"]).open("a") as handle:
        handle.write("approved\\n")
    return {"writes": state.get("writes", 0) + 1, "result": state["prompt"] + "|" + state["answer"]}

builder = StateGraph(State)
builder.add_node("read", read)
builder.add_node("ask", ask)
builder.add_node("approval", approval)
builder.add_edge(START, "read")
builder.add_edge("read", "ask")
builder.add_edge("ask", "approval")
builder.add_edge("approval", END)
graph = builder.compile()
'''
    work_dirs = []
    for index in range(3):
        work_dir = tmp_path / f"server-{index}"
        work_dir.mkdir()
        (work_dir / "graph.py").write_text(graph_source, encoding="utf-8")
        _write_checkpointer(work_dir)
        generate_langgraph_json(work_dir, graph_ref="./graph.py:graph",
                                checkpointer_path="./checkpointer.py:create_checkpointer")
        work_dirs.append(work_dir)
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async def stream_raw(agent, value, config):
        prepared = agent._prepare_mutation(config)
        token = prepared["configurable"].pop(OWNER_KEY)
        async for _ in agent._get_graph().astream(
            value, config=prepared, stream_mode=["updates"], headers={OWNER_KEY: token},
        ):
            pass

    async def original_stream(agent, value, config, *_):
        await stream_raw(agent, value, config)

    monkeypatch.setattr(runtime_client, "_stream_agent", original_stream)
    console = SimpleNamespace(print=lambda *_: None)

    async def persist_pause(agent, expected_state):
        snapshot = await agent.aget_state(config)
        requests = budget_runtime.pending_requests(snapshot)
        assert len(requests) == 1
        with pytest.raises(budget_runtime.TaskPaused):
            await runtime_client._process_hitl_interrupts(
                SimpleNamespace(thread_id=thread_id, pending_interrupts=requests), console,
            )
        record = store.get_task("task")
        assert record.state == expected_state and record.thread_id == thread_id
        return record

    async def run():
        servers = [ServerProcess(config_dir=work_dir) for work_dir in work_dirs]
        try:
            budget_runtime.install_budget_runtime()
            await servers[0].start()
            await servers[0].wait_for_graph_ready("agent")
            first = RemoteAgent(servers[0].url)
            await first.aensure_thread(config)
            await stream_raw(first, {"prompt": "original prompt", "reads": 0, "writes": 0}, config)
            input_record = await persist_pause(first, "waiting_input")
            assert effect_path.read_text() == "read\n"
            servers[0].stop()

            approvals.respond_task("task", pending_id=input_record.checkpoint["pending_id"],
                                   decision="answer", value="Keep v1")
            input_mapping = spawned[-1][1]["resume_value"]
            assert set(input_mapping) == set(input_record.checkpoint["interrupts"])
            store.update_task("task", state="running", expect_state="starting")
            await servers[1].start()
            await servers[1].wait_for_graph_ready("agent")
            second = RemoteAgent(servers[1].url)
            assert await second.aget_state(config) is None
            monkeypatch.setenv("MANTA_RESUME_THREAD_ID", thread_id)
            monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps(input_mapping))
            monkeypatch.setenv("MANTA_RESUME_PENDING", json.dumps(input_record.checkpoint["interrupts"]))
            # A new detached process installs a fresh one-use resume wrapper.
            monkeypatch.setattr(runtime_client, "_stream_agent", original_stream)
            async def no_auto(*_):
                pytest.fail("Pending task was auto-approved")
            monkeypatch.setattr(runtime_client, "_process_hitl_interrupts", no_auto)
            budget_runtime.install_budget_runtime()
            await runtime_client._stream_agent(second, {"prompt": "must not replay"}, config, None, None, None, None)
            approval_record = await persist_pause(second, "waiting_approval")
            assert approval_record.id == input_record.id
            assert approval_record.checkpoint["pending_id"] != input_record.checkpoint["pending_id"]
            assert effect_path.read_text() == "read\n"
            servers[1].stop()

            approvals.respond_task("task", pending_id=approval_record.checkpoint["pending_id"], decision="approve")
            store.update_task("task", state="running", expect_state="starting")
            await servers[2].start()
            await servers[2].wait_for_graph_ready("agent")
            third = RemoteAgent(servers[2].url)
            monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps(spawned[-1][1]["resume_value"]))
            monkeypatch.setenv("MANTA_RESUME_PENDING", json.dumps(approval_record.checkpoint["interrupts"]))
            monkeypatch.setattr(runtime_client, "_stream_agent", original_stream)
            monkeypatch.setattr(runtime_client, "_process_hitl_interrupts", no_auto)
            budget_runtime.install_budget_runtime()
            await runtime_client._stream_agent(third, {"prompt": "must not replay either"}, config, None, None, None, None)
            finished = await third.aget_state(config)
            assert finished.values == {
                "prompt": "original prompt", "reads": 1, "answer": "Keep v1", "writes": 1,
                "result": "original prompt|Keep v1",
            }
            assert not finished.next and not budget_runtime.pending_requests(finished)
            assert effect_path.read_text() == "read\napproved\n"
            assert len(spawned) == 2
        finally:
            for server in servers:
                server.stop()
            if lease := held_lease(thread_id):
                lease.release()

    asyncio.run(run())
