"""ADR 0012 — self-writing memory, run diagnostics, notifications, receipts."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)


# --- self-writing memory -------------------------------------------------------


def test_manta_remember_roundtrip_with_recall():
    from manta_code.agents import memory as mem

    out = mem.manta_remember("we use pytest fixtures, never MagicMock")
    assert "Remembered" in out
    assert "orchestrator" in out  # base session -> orchestrator namespace
    store = mem.shared_memory_store()
    notes = mem.read_memories(store, ("memories", "orchestrator"))
    assert any("pytest fixtures" in n for n in notes)


def test_manta_remember_targets_active_agent(monkeypatch):
    from manta_code.agents import memory as mem

    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", "planning")
    out = mem.manta_remember("plans must name verification steps")
    assert "planning" in out
    notes = mem.read_memories(mem.shared_memory_store(), ("memories", "planning"))
    assert any("verification steps" in n for n in notes)


def test_manta_remember_redacts_secrets():
    from manta_code.agents import memory as mem

    fake_token = "dapi" + "1234abcd" * 5  # assembled so secret scanners don't flag the test fixture
    mem.manta_remember(f"the token is {fake_token}")
    notes = mem.read_memories(mem.shared_memory_store(), ("memories", "orchestrator"))
    assert notes and all(fake_token not in n for n in notes)
    assert any("redacted" in n for n in notes)


def test_memory_tools_compiled_and_injected():
    from manta_code import hook
    from manta_code.agents.memory import build_memory_tools

    (tool,) = build_memory_tools()
    assert tool.name == "manta_remember"
    kwargs: dict = {}
    hook.enrich_kwargs(kwargs)
    names = {getattr(t, "name", "") for t in (kwargs.get("tools") or [])}
    assert "manta_remember" in names


def test_orchestrator_recall_attached():
    from manta_code import hook

    names = {getattr(m, "name", type(m).__name__) for m in hook.build_orchestrator_middleware()}
    assert any("Memory.orchestrator" in str(n) for n in names)


def test_empty_note_rejected():
    from manta_code.agents.memory import manta_remember

    assert "Nothing to remember" in manta_remember("   ")


# --- run diagnostics -------------------------------------------------------------


class _Obj:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def test_get_run_diagnostics_surfaces_failed_task_output():
    from manta_code.databricks_tools import DatabricksTools

    failed_state = _Obj(life_cycle_state="TERMINATED", result_state="FAILED", state_message="boom")
    run = _Obj(
        state=failed_state,
        run_page_url="https://x/run/9",
        tasks=[
            _Obj(task_key="etl", run_id=901, state=_Obj(result_state="FAILED")),
            _Obj(task_key="ok-task", run_id=902, state=_Obj(result_state="SUCCESS")),
        ],
    )
    output = _Obj(
        error="Py4JJavaError: java.lang.OutOfMemoryError",
        error_trace="line1\nline2\nOOM at stage 3",
        logs="...\ndriver OOM dump",
    )

    class _Jobs:
        def get_run(self, run_id):
            return run

        def get_run_output(self, run_id):
            assert run_id == 901  # only the failed task is fetched
            return output

    tools = DatabricksTools.__new__(DatabricksTools)
    tools._client = _Obj(jobs=_Jobs())
    text = tools.get_run_diagnostics(9)
    assert "Task etl: result=FAILED" in text
    assert "OutOfMemoryError" in text
    assert "OOM at stage 3" in text
    assert "driver OOM dump" in text
    assert "ok-task" in text  # listed, but no output fetched


def test_run_diagnostics_in_tool_list():
    from manta_code.databricks_tools import DatabricksTools

    tools = DatabricksTools.__new__(DatabricksTools)
    names = {t.name for t in DatabricksTools.as_tools(tools)}
    assert "get_run_diagnostics" in names


# --- notifications ----------------------------------------------------------------


def test_notify_disabled_by_env(monkeypatch):
    from manta_code.tasks import notify as N

    monkeypatch.setenv("MANTA_NOTIFY", "0")
    assert N.notify("t", "m") is False


def test_notify_task_finished_invokes_platform_tool(monkeypatch):
    from manta_code.tasks import notify as N

    calls = {}
    monkeypatch.setattr(N.sys, "platform", "darwin")
    monkeypatch.setattr(N.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(
        N.subprocess, "run", lambda argv, **k: calls.update(argv=argv)
    )
    assert N.notify_task_finished("ab12", "swe", "done") is True
    assert "osascript" in calls["argv"][0]
    assert "ab12" in calls["argv"][2]


def test_notify_never_raises(monkeypatch):
    from manta_code.tasks import notify as N

    monkeypatch.setattr(N.shutil, "which", lambda name: "/bin/notify-send")
    monkeypatch.setattr(
        N.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
    )
    assert N.notify("t", "m") is False  # swallowed


# --- receipts ---------------------------------------------------------------------


def test_receipts_savings_counterfactual(tmp_path):
    from manta_code.agents import usage as U

    db = tmp_path / "u.db"
    # Cheap-model usage: real cost tiny, premium baseline large.
    U.record_usage(
        U.UsageRecord(
            agent="orchestrator", model="databricks-gpt-oss-120b",
            input_tokens=1_000_000, output_tokens=100_000, cost_usd=0.21,
        ),
        path=db,
    )
    r = U.receipts(days=7, path=db)
    assert r.calls == 1
    assert r.actual_usd == pytest.approx(0.21)
    # 1M input @ $15 + 100k output @ $75 = 15 + 7.5
    assert r.premium_baseline_usd == pytest.approx(22.5)
    assert r.estimated_savings_usd == pytest.approx(22.29)


def test_receipts_counts_advisor_activity(tmp_path):
    from manta_code.agents import usage as U

    db = tmp_path / "u.db"
    U.record_usage(
        U.UsageRecord(agent="a", model="m", input_tokens=10, output_tokens=5),
        path=db,
    )
    U.record_advice(
        U.AdviceRecord(agent="a", kind="downgrade", severity="note",
                       message="x", delivered="note"),
        path=db,
    )
    U.record_advice(
        U.AdviceRecord(agent="a", kind="budget_tradeoff", severity="interrupt",
                       message="y", delivered="interrupt:approved"),
        path=db,
    )
    r = U.receipts(days=7, path=db)
    assert r.advice_delivered == 2
    assert r.advice_accepted == 1


def test_receipts_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from manta_code.agents import usage as U
    from manta_code.main import app

    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    U.record_usage(
        U.UsageRecord(
            agent="orchestrator", model="databricks-gpt-oss-120b",
            input_tokens=50_000, output_tokens=5_000, cost_usd=0.01,
        )
    )
    result = CliRunner().invoke(app, ["receipts"])
    assert result.exit_code == 0
    assert "Premium baseline" in result.stdout
    assert "Est. savings" in result.stdout
