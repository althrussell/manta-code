from __future__ import annotations

import pytest

from manta_code.tasks import store, tools


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


def _make_task(**kw) -> store.TaskRecord:
    defaults = dict(id=store.new_task_id(), agent="swe", prompt="fix the tests")
    defaults.update(kw)
    return store.create_task(store.TaskRecord(**defaults))


def test_submit_tool_reports_errors_as_text():
    # Tool functions return text, never raise — a store hiccup or bad input
    # must not crash an agent turn.
    out = tools.manta_task_submit("not-a-real-agent", "do something")
    assert out.startswith("Could not submit task:")


def test_submit_tool_returns_receipt(monkeypatch):
    from manta_code.tasks import executor

    record = store.TaskRecord(id="abcd1234", agent="swe", prompt="x")
    monkeypatch.setattr(executor, "submit_task", lambda a, p: record)
    out = tools.manta_task_submit("swe", "x")
    assert "abcd1234" in out
    assert "@swe" in out


def test_status_and_output_tools():
    record = _make_task(state="done", result="all tests pass")
    status = tools.manta_task_status(record.id)
    assert record.id in status
    assert "done" in status
    output = tools.manta_task_output(record.id)
    assert "all tests pass" in output
    assert "done" in output


def test_status_unknown_task():
    assert tools.manta_task_status("zzzz9999") == "No task 'zzzz9999'."


def test_list_tool_formats_and_validates_state():
    _make_task(prompt="first task")
    out = tools.manta_task_list()
    assert "@swe" in out
    assert "first task" in out
    assert "Unknown state" in tools.manta_task_list("exploded")
    assert tools.manta_task_list("failed") == "No background tasks yet."


def test_cancel_tool(monkeypatch):
    record = _make_task(state="running", pid=None)
    out = tools.manta_task_cancel(record.id)
    assert f"Cancelled task {record.id}" in out
    assert store.get_task(record.id).state == "cancelled"


def test_build_task_tools_compiles_structured_tools():
    pytest.importorskip("langchain_core")
    built = tools.build_task_tools()
    names = {t.name for t in built}
    assert names == {
        "manta_task_submit",
        "manta_task_status",
        "manta_task_output",
        "manta_task_list",
        "manta_task_cancel",
    }
