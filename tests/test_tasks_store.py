from __future__ import annotations

import pytest

from manta_code.tasks import store


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


def _make(agent="swe", prompt="do the thing", **kw) -> store.TaskRecord:
    return store.create_task(
        store.TaskRecord(id=store.new_task_id(), agent=agent, prompt=prompt, **kw)
    )


def test_create_and_get_roundtrip():
    record = _make(timeout=600, max_turns=10)
    loaded = store.get_task(record.id)
    assert loaded is not None
    assert loaded.agent == "swe"
    assert loaded.prompt == "do the thing"
    assert loaded.state == "queued"
    assert loaded.timeout == 600
    assert loaded.max_turns == 10


def test_get_missing_returns_none():
    assert store.get_task("nope1234") is None


def test_list_tasks_orders_newest_first_and_filters():
    a = _make(prompt="first")
    b = store.create_task(
        store.TaskRecord(
            id=store.new_task_id(), agent="review", prompt="second",
            created_at=a.created_at + 10, state="running",
        )
    )
    all_tasks = store.list_tasks()
    assert [t.id for t in all_tasks] == [b.id, a.id]
    running = store.list_tasks(state="running")
    assert [t.id for t in running] == [b.id]


def test_update_task_fields():
    record = _make()
    assert store.update_task(record.id, state="running", pid=4242) is True
    loaded = store.get_task(record.id)
    assert loaded.state == "running"
    assert loaded.pid == 4242


def test_update_rejects_unknown_fields_and_states():
    record = _make()
    with pytest.raises(ValueError):
        store.update_task(record.id, nonsense="x")
    with pytest.raises(ValueError):
        store.update_task(record.id, state="exploded")


def test_conditional_update_protects_cancellation():
    # The runner finishing uses expect_state="running"; a cancel that landed
    # first must win (the task is never resurrected to done).
    record = _make()
    store.update_task(record.id, state="running")
    store.update_task(record.id, state="cancelled")
    changed = store.update_task(
        record.id, state="done", exit_code=0, expect_state="running"
    )
    assert changed is False
    assert store.get_task(record.id).state == "cancelled"


def test_events_roundtrip_and_task_filter():
    store.record_event(store.EventRecord(agent="swe", kind="tool", detail="ls"))
    store.record_event(
        store.EventRecord(agent="review", kind="denied", detail="execute", task_id="t1")
    )
    everything = store.recent_events(limit=10)
    assert {e.kind for e in everything} == {"tool", "denied"}
    only_t1 = store.recent_events(task_id="t1")
    assert len(only_t1) == 1
    assert only_t1[0].agent == "review"


def test_record_event_never_raises(monkeypatch):
    # Observability is best-effort: a broken store must not break a tool call.
    monkeypatch.setattr(store, "connect", lambda path=None: (_ for _ in ()).throw(RuntimeError))
    store.record_event(store.EventRecord(agent="swe", kind="tool"))  # no raise
