"""Mixed server-owned and human interrupts retain one exact durable group."""

from __future__ import annotations

import asyncio
import copy
import io
import json
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from rich.console import Console

from manta_code import budget_runtime
from manta_code.tasks import approvals, executor, store


def hook_pair(tmp_path):
    request = {"type": "hook_invocation", "request": {
        "protocol_version": 1, "invocation_id": str(uuid4()),
        "snapshot_id": "fixture-snapshot", "run_id": "fixture-run",
        "deadline": "2099-01-01T00:00:00Z", "invocation": {
            "context": {"thread_id": "thread", "cwd": str(tmp_path), "approval_mode": "manual"},
            "event": {"event": "SessionStart", "cause": "resume"},
        },
    }}
    response = {"protocol_version": 1, "invocation_id": request["request"]["invocation_id"],
                "snapshot_id": "fixture-snapshot",
                "decision": {"event": "SessionStart", "context": ["already computed"]}}
    return request, response


def test_actual_mixed_hook_skill_and_human_checkpoint_survives_runner_restart(tmp_path, monkeypatch):
    from deepagents_code.client import non_interactive as client

    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setenv("MANTA_TASK_ID", "task")
    for key in ("MANTA_TASK_DB_PATH", "MANTA_RESUME_THREAD_ID", "MANTA_RESUME_VALUE", "MANTA_RESUME_PENDING"):
        monkeypatch.delenv(key, raising=False)
    store.create_task(store.TaskRecord(id="task", agent="swe", prompt="original prompt", state="running"))
    hook, hook_response = hook_pair(tmp_path)
    skill = {"type": "skill_trust", "skill": "fixture", "directory": str(tmp_path / "fixture")}
    human = {"type": "tool_approval", "tool_call_id": "call-one", "name": "write_file",
             "args": {"path": "result.py"}, "description": "Write the reviewed file"}
    effects, hooks_run, spawned, inputs = [], [], [], []

    class State(TypedDict, total=False):
        prompt: str
        hook: dict
        skill: dict
        human: dict
        result: str

    def hook_node(state):
        return {"hook": interrupt(hook)}

    def skill_node(state):
        return {"skill": interrupt(skill)}

    def human_node(state):
        return {"human": interrupt(human)}

    def finish(state):
        assert state["hook"] == hook_response
        assert state["skill"] == {"approved": False}
        assert state["human"] == {"type": "approve"}
        effects.append("one reviewed effect")
        return {"result": state["prompt"]}

    builder = StateGraph(State)
    for name, node in (("hook", hook_node), ("skill", skill_node), ("human", human_node)):
        builder.add_node(name + "_node", node)
        builder.add_edge(START, name + "_node")
    builder.add_node("finish", finish)
    builder.add_edge(["hook_node", "skill_node", "human_node"], "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    console = Console(file=io.StringIO())

    async def fulfill(pending):
        hooks_run.append(copy.deepcopy(pending))
        assert len(pending) == 1 and next(iter(pending.values())) == hook
        return {identity: hook_response for identity in pending}

    async def upstream_stream(agent, value, config, state, console, tracker, context):
        inputs.append(value)
        async for chunk in graph.astream(value, config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                client._process_interrupts(chunk, state, console)

    async def forbidden(*args):
        pytest.fail("Managed human requests must not reach upstream autoapproval")

    async def ensure(config):
        pass

    agent = SimpleNamespace(aensure_thread=ensure, aget_state=graph.aget_state)
    monkeypatch.setattr(client, "_process_interrupts", client._process_interrupts)
    monkeypatch.setattr(client, "generate_thread_id", lambda: "thread")
    monkeypatch.setattr(client, "_process_hitl_interrupts", forbidden)
    monkeypatch.setattr(client, "_stream_agent", upstream_stream)
    monkeypatch.setattr(executor, "validate_execution_manifest", lambda record: None)

    def spawn(record, **kwargs):
        spawned.append((copy.deepcopy(record), copy.deepcopy(kwargs)))
        return store.get_task(record.id)

    monkeypatch.setattr(executor, "_spawn_task", spawn)
    budget_runtime.install_budget_runtime()

    async def run():
        state = client.StreamState(thread_id="thread")
        state.hooks = SimpleNamespace(fulfill_pending_interrupts=fulfill, has_handlers=lambda _: False)
        await client._stream_agent(agent, {"prompt": "original prompt"}, config, state, console, None, None)
        await client._fulfill_pending_hook_interrupts(state)
        with pytest.raises(budget_runtime.TaskPaused):
            await client._process_hitl_interrupts(state, console)
        assert not effects and len(hooks_run) == 1
        record = store.get_task("task")
        assert record.state == "waiting_approval"
        checkpoint = record.checkpoint
        assert sorted(request["type"] for request in checkpoint["interrupts"].values()) == [
            "hook_invocation", "skill_trust", "tool_approval",
        ]
        assert len(checkpoint["automatic_responses"]) == 2
        approvals.respond_task("task", pending_id=checkpoint["pending_id"], decision="approve")
        resume_value = spawned[0][1]["resume_value"]
        assert set(resume_value) == set(checkpoint["interrupts"])

        # A fresh runner gets only durable state, not the previous stream's
        # hook manager or response dictionaries. The hook must not run twice.
        monkeypatch.setattr(client, "_process_hitl_interrupts", forbidden)
        monkeypatch.setattr(client, "_stream_agent", upstream_stream)
        budget_runtime.install_budget_runtime()
        monkeypatch.setenv("MANTA_RESUME_THREAD_ID", "thread")
        monkeypatch.setenv("MANTA_RESUME_PENDING", json.dumps(checkpoint["interrupts"]))
        monkeypatch.setenv("MANTA_RESUME_VALUE", json.dumps(resume_value))
        await client._stream_agent(agent, {"prompt": "must not replay"}, config,
                                   client.StreamState(thread_id="thread"), console, None, None)
        snapshot = await graph.aget_state(config)
        assert not snapshot.next and not snapshot.interrupts
        assert snapshot.values["result"] == "original prompt"
        assert effects == ["one reviewed effect"] and len(hooks_run) == 1
        assert inputs[-1].resume == resume_value

    asyncio.run(run())


@pytest.mark.parametrize("field", ["invocation_id", "snapshot_id"])
def test_retained_hook_response_must_match_exact_invocation(tmp_path, field):
    request, response = hook_pair(tmp_path)
    response[field] = str(uuid4())
    with pytest.raises(ValueError, match="mismatch"):
        approvals.validate_automatic_responses({"hook": request}, {"hook": response})


def test_retained_skill_response_cannot_grant_new_trust():
    with pytest.raises(ValueError, match="cannot grant"):
        approvals.validate_automatic_responses(
            {"skill": {"type": "skill_trust", "skill": "fixture", "directory": "/tmp/fixture"}},
            {"skill": {"approved": True}},
        )


def test_budget_resume_retains_computed_automatic_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.delenv("MANTA_TASK_DB_PATH", raising=False)
    store.create_task(store.TaskRecord(id="task", agent="swe", prompt="work", state="running"))
    request, response = hook_pair(tmp_path)
    checkpoint = approvals.checkpoint_requests("task", "thread", {
        "budget": {"action_requests": [{"name": "manta_budget_continue", "description": "cap reached"}]},
    }, automatic_requests={"hook": request}, automatic_responses={"hook": response})
    assert store.get_task("task").state == "paused_budget"
    assert checkpoint["resume_value"] == {"hook": response, "budget": {"decisions": [{"type": "approve"}]}}
