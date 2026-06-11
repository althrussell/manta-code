"""ADR 0011 — steerable tasks, ASK policy tier, SDK, pause repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from manta_code.tasks import executor, store


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    for var in ("MANTA_TASK_ID", "MANTA_UNATTENDED", "MANTA_ALLOW_ASKS",
                "DEEPAGENTS_CODE_SERVER_AUTO_APPROVE"):
        monkeypatch.delenv(var, raising=False)


# --- task inbox (store) ------------------------------------------------------------


def test_inbox_roundtrip_and_per_row_consumption():
    record = store.create_task(store.TaskRecord(id="t1ask", agent="swe", prompt="x"))
    first = store.add_inbox_message(record.id, "focus on the parser")
    store.add_inbox_message(record.id, "skip the docs")

    pending = store.unconsumed_inbox(record.id)
    assert [m.message for m in pending] == ["focus on the parser", "skip the docs"]

    # Per-row consumption: marking only the first leaves the second pending —
    # a message inserted between read and mark is never silently swallowed.
    store.mark_inbox_consumed([first.id])
    remaining = store.unconsumed_inbox(record.id)
    assert [m.message for m in remaining] == ["skip the docs"]
    assert store.inbox_count(record.id) == 2


def test_tasks_table_migrates_allow_asks_in_place(tmp_path):
    # Simulate a pre-ADR-0011 database (no allow_asks column).
    import sqlite3

    db = tmp_path / "old-tasks.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, agent TEXT NOT NULL, "
        "prompt TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, "
        "started_at REAL, finished_at REAL, pid INTEGER, exit_code INTEGER, "
        "log_path TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', "
        "timeout INTEGER, max_turns INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks (id, agent, prompt, state, created_at) "
        "VALUES ('old00001','swe','x','done',1.0)"
    )
    conn.commit()
    conn.close()

    loaded = store.get_task("old00001", path=db)
    assert loaded is not None
    assert loaded.allow_asks is False  # migrated column, sane default


# --- steering (executor) -------------------------------------------------------------


class _FakeProcess:
    pid = 4242


@pytest.fixture()
def _fake_spawn(monkeypatch):
    spawned = {}

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    return spawned


def test_send_to_task_queues_and_audits(_fake_spawn):
    record = executor.submit_task("swe", "long thing")
    executor.send_to_task(record.id, "change of plan: use the adapter")
    assert [m.message for m in store.unconsumed_inbox(record.id)] == [
        "change of plan: use the adapter"
    ]
    kinds = [e.kind for e in store.recent_events(task_id=record.id)]
    assert "task_steered" in kinds


def test_send_to_finished_task_rejected(_fake_spawn):
    record = executor.submit_task("swe", "thing")
    store.update_task(record.id, state="done")
    with pytest.raises(executor.TaskError, match="already done"):
        executor.send_to_task(record.id, "too late")


def test_submit_allow_asks_sets_env_and_column(_fake_spawn):
    record = executor.submit_task("swe", "risky thing", allow_asks=True)
    env = _fake_spawn["kwargs"]["env"]
    assert env["MANTA_ALLOW_ASKS"] == "1"
    assert store.get_task(record.id).allow_asks is True
    # Default: no pre-approval leaks into the runner env.
    record2 = executor.submit_task("swe", "normal thing")
    assert "MANTA_ALLOW_ASKS" not in _fake_spawn["kwargs"]["env"]
    assert store.get_task(record2.id).allow_asks is False


# --- InboxMiddleware -----------------------------------------------------------------


def test_inbox_middleware_delivers_as_checkpointed_state(monkeypatch):
    from manta_code.middleware.inbox import STEERING_PREFIX, InboxMiddleware

    record = store.create_task(store.TaskRecord(id="t2ask", agent="swe", prompt="x"))
    store.add_inbox_message(record.id, "prefer small diffs")

    mw = InboxMiddleware(task_id=record.id)
    update = mw.before_model({}, None)
    assert update is not None
    (note,) = update["messages"]
    assert isinstance(note, HumanMessage)
    assert note.content.startswith(STEERING_PREFIX)
    assert "prefer small diffs" in note.content
    # Consumed: a second model call delivers nothing new.
    assert mw.before_model({}, None) is None


def test_inbox_middleware_factory_inactive_outside_tasks(monkeypatch):
    from manta_code.middleware.inbox import task_inbox_middleware

    assert task_inbox_middleware() is None
    monkeypatch.setenv("MANTA_TASK_ID", "t3ask")
    assert task_inbox_middleware() is not None


def test_inbox_middleware_guarded_against_store_failure(monkeypatch):
    from manta_code.middleware import inbox as I

    mw = I.InboxMiddleware(task_id="whatever")
    monkeypatch.setattr(
        "manta_code.tasks.store.unconsumed_inbox",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError),
    )
    assert mw.before_model({}, None) is None  # never breaks the task


def test_hook_attaches_inbox_only_inside_tasks(monkeypatch, tmp_path):
    from manta_code import hook

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    names = {type(m).__name__ for m in hook.build_orchestrator_middleware()}
    assert "InboxMiddleware" not in names
    monkeypatch.setenv("MANTA_TASK_ID", "t4ask")
    names = {type(m).__name__ for m in hook.build_orchestrator_middleware()}
    assert "InboxMiddleware" in names


# --- ASK policy tier -----------------------------------------------------------------


def _ask_policy():
    from manta_code.middleware.policy import ToolPolicyMiddleware

    return ToolPolicyMiddleware(ask=["run_job"], agent_name="data-ops")


def test_ask_denied_in_unattended_runs(monkeypatch):
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    reason = _ask_policy()._denial_reason("run_job", {})
    assert reason is not None
    assert "requires human approval" in reason
    assert "--allow-asks" in reason


def test_ask_allowed_when_preapproved(monkeypatch):
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    monkeypatch.setenv("MANTA_ALLOW_ASKS", "1")
    assert _ask_policy()._denial_reason("run_job", {}) is None


def test_ask_passes_through_interactively():
    # Interactive sessions: upstream interrupt_on owns the prompt; the policy
    # layer must not deny.
    assert _ask_policy()._denial_reason("run_job", {}) is None


def test_ask_evaluated_after_other_rules(monkeypatch):
    # A deny-listed tool never reaches the ask tier — no pointless prompt.
    from manta_code.middleware.policy import ToolPolicyMiddleware

    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    mw = ToolPolicyMiddleware(deny=["run_job"], ask=["run_job"])
    reason = mw._denial_reason("run_job", {})
    assert "deny-list" in reason


def test_factory_merges_tools_ask_into_interrupt_on():
    from manta_code.agents.factory import _interrupt_on, _tool_policy_middleware
    from manta_code.agents.registry import AgentDef

    defn = AgentDef(
        name="data-ops", approval=["write_file"], tools_ask=["run_job", "write_file"]
    )
    interrupt_on = _interrupt_on(defn)
    assert interrupt_on == {"write_file": True, "run_job": True}  # once each
    (policy,) = [m for m in _tool_policy_middleware(defn)]
    assert policy._ask == {"run_job", "write_file"}


def test_unattended_detection_markers(monkeypatch):
    from manta_code.tasks.events import unattended_run

    assert unattended_run() is False
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    assert unattended_run() is True
    monkeypatch.delenv("MANTA_UNATTENDED")
    monkeypatch.setenv("MANTA_TASK_ID", "t")
    assert unattended_run() is True
    monkeypatch.delenv("MANTA_TASK_ID")
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_AUTO_APPROVE", "true")
    assert unattended_run() is True


def test_run_headless_exports_unattended_marker(tmp_path, monkeypatch):
    from manta_code import dcode

    captured = {}

    def fake_run(argv, env=None, check=False):
        captured["env"] = env

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(dcode.subprocess, "run", fake_run)
    monkeypatch.setattr(dcode, "ensure_dcode_config", lambda *a, **k: tmp_path)
    monkeypatch.setattr(dcode, "mark_onboarding_complete", lambda **k: tmp_path)
    monkeypatch.setattr(dcode, "sync_agent_profiles", lambda: None)
    dcode.run_headless(
        profile=None,
        default_endpoint=None,
        endpoints=[],
        message="x",
        env_extra={"MANTA_TASK_ID": "tag-1"},
    )
    assert captured["env"]["MANTA_UNATTENDED"] == "1"
    assert captured["env"]["MANTA_TASK_ID"] == "tag-1"


# --- pause repair: resume decisions --------------------------------------------------


@dataclass
class _Runtime:
    thread_id: str = "t-lv"


@dataclass
class _Request:
    model: Any = "databricks-claude-opus-4-8"
    messages: list = field(default_factory=list)
    system_message: Any = None
    tools: list = field(default_factory=list)
    runtime: _Runtime = field(default_factory=_Runtime)
    _overridden: Any = None

    def override(self, **kwargs):
        new = _Request(**{k: v for k, v in self.__dict__.items() if k != "_overridden"})
        new._overridden = kwargs
        return new


def _ai(output_tokens=50):
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "total_tokens": 100 + output_tokens,
        },
    )


def test_budget_reject_ends_turn_gracefully(tmp_path, monkeypatch):
    from manta_code.middleware import economy as E

    import langgraph.types as lt

    monkeypatch.setattr(
        lt, "interrupt", lambda payload: {"decisions": [{"type": "reject"}]}
    )
    mw = E.TokenEconomyMiddleware(
        agent="swe", max_tokens=10, ledger_path=tmp_path / "u.db"
    )
    req = _Request()

    class _Resp:
        result = [_ai()]

    mw.wrap_model_call(req, lambda r: _Resp())  # accrue past the cap
    result = mw.wrap_model_call(req, lambda r: _Resp())
    assert isinstance(result, AIMessage)
    assert "Stopped at the budget cap" in result.content
    # And it stays stopped — no further model calls on this thread.
    again = mw.wrap_model_call(req, lambda r: _Resp())
    assert "Stopped at the budget cap" in again.content


def test_budget_unattended_logs_event_and_continues(tmp_path, monkeypatch):
    from manta_code.middleware import economy as E

    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    mw = E.TokenEconomyMiddleware(
        agent="swe", max_tokens=10, ledger_path=tmp_path / "u.db"
    )
    req = _Request()

    class _Resp:
        result = [_ai()]

    mw.wrap_model_call(req, lambda r: _Resp())
    result = mw.wrap_model_call(req, lambda r: _Resp())  # over cap, no human
    assert not isinstance(result, AIMessage) or "Stopped" not in str(result)
    kinds = {e.kind for e in store.recent_events(limit=10)}
    assert "budget" in kinds  # visible, never silent


def test_daily_budget_cap_triggers_pause(tmp_path, monkeypatch):
    from manta_code.agents import usage as U
    from manta_code.middleware import economy as E

    import langgraph.types as lt

    interrupts: list = []
    monkeypatch.setattr(
        lt,
        "interrupt",
        lambda payload: interrupts.append(payload) or {"decisions": [{"type": "approve"}]},
    )
    monkeypatch.setattr(U, "today_total_usd", lambda **k: 12.50)
    mw = E.TokenEconomyMiddleware(
        agent="orchestrator", daily_max_usd=10.0, ledger_path=tmp_path / "u.db"
    )

    class _Resp:
        result = [_ai()]

    mw.wrap_model_call(_Request(), lambda r: _Resp())
    assert len(interrupts) == 1
    (action,) = interrupts[0]["action_requests"]
    assert action["name"] == "manta_budget_continue"
    assert "daily budget" in action["description"]


def test_advice_reject_downgrades_thread(monkeypatch):
    from manta_code.agents.usage import Price
    from manta_code.middleware import advice as ADV

    import langgraph.types as lt

    monkeypatch.setattr(
        lt, "interrupt", lambda payload: {"decisions": [{"type": "reject"}]}
    )
    pricing = {"opus": Price(input=15.0, output=75.0)}
    mw = ADV.AdviceMiddleware(agent="t", max_usd=0.001, pricing=pricing)
    monkeypatch.setattr(mw, "_cheap_default_model", lambda: "CHEAP_MODEL")
    req = _Request(model="databricks-opus-x")

    class _Resp:
        result = [_ai(output_tokens=500)]
        tool_calls = None
        content = "ok"

    mw.wrap_model_call(req, lambda r: _Resp())  # accrue spend
    captured = {}
    mw.wrap_model_call(req, lambda r: captured.update(model=r._overridden) or _Resp())
    assert captured["model"] == {"model": "CHEAP_MODEL"}


# --- SDK -----------------------------------------------------------------------------


def test_sdk_submit_send_wait(monkeypatch, _fake_spawn):
    from manta_code import sdk

    handle = sdk.submit("swe", "do the thing", allow_asks=True)
    assert store.get_task(handle.id).allow_asks is True
    handle.send("steer it")
    assert store.inbox_count(handle.id) == 1

    store.update_task(handle.id, state="done", result="all done")
    record = handle.wait(poll_seconds=0.01, timeout=5)
    assert record.state == "done"
    assert handle.output() == "all done"


def test_sdk_wait_times_out(_fake_spawn):
    from manta_code import sdk

    handle = sdk.submit("swe", "never finishes")
    store.update_task(handle.id, state="running", pid=4242)
    with pytest.raises(TimeoutError):
        handle.wait(poll_seconds=0.01, timeout=0.05)


def test_sdk_run_captures_output_and_tagged_cost(monkeypatch):
    from manta_code import sdk

    class _Done:
        returncode = 0
        stdout = "THE ANSWER\n"
        stderr = ""

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return _Done()

    monkeypatch.setattr(sdk.subprocess, "run", fake_run)
    monkeypatch.setattr(sdk, "_ledger_for_tag", lambda tag, since: (0.0042, 1234))
    result = sdk.run("question", agent="review", timeout=120, max_turns=4)
    assert result.ok and result.output == "THE ANSWER"
    assert result.cost_usd == 0.0042 and result.tokens == 1234
    assert captured["env"]["MANTA_TASK_ID"].startswith("sdk-")
    assert captured["argv"][-2:] == ["-a", "review"]
    import os as _os

    assert "MANTA_TASK_ID" not in _os.environ  # global env never mutated


def test_sdk_read_surfaces(_fake_spawn):
    from manta_code import sdk

    sdk.submit("swe", "a")
    assert sdk.tasks()[0].agent == "swe"
    assert any(a.name == "chief" for a in sdk.agents())
    assert isinstance(sdk.cost(by="agent"), list)
