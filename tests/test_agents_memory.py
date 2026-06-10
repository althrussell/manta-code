from __future__ import annotations

import pytest

pytest.importorskip("langgraph.store.sqlite")

from manta_code.agents import memory  # noqa: E402
from manta_code.agents.registry import AgentDef  # noqa: E402


# Synthetic, NON-REAL credential fixtures used only to verify the redactor. They
# are assembled from parts at runtime so no contiguous token literal exists in the
# source (which would trip secret scanners) while the runtime value still matches
# the redaction patterns being tested.
_FAKE_DBX_TOKEN = "dapi" + ("0123456789abcdef" * 2)
_FAKE_AWS_KEY = "AKIA" + ("ABCDEFGHIJKLMNOP")


def test_redact_text_scrubs_secrets_and_pii():
    raw = (
        f"token=abcd1234secret email me@example.com "
        f"{_FAKE_DBX_TOKEN} key {_FAKE_AWS_KEY} "
        "ssn 123-45-6789"
    )
    out = memory.redact_text(raw)
    assert "me@example.com" not in out
    assert _FAKE_DBX_TOKEN not in out
    assert _FAKE_AWS_KEY not in out
    assert "123-45-6789" not in out
    assert "[redacted-email]" in out


def test_redact_value_recurses():
    out = memory.redact_value({"a": ["contact me@example.com"], "b": {"c": "ok"}})
    assert "[redacted-email]" in out["a"][0]
    assert out["b"]["c"] == "ok"


def test_store_redacts_on_write_and_persists(tmp_path):
    db = tmp_path / "mem.db"
    store = memory.open_store(db)
    ns = ("memories", "reviewer")
    memory.write_memory(store, ns, "n1", "secret token=supersecret123 and email me@x.com")
    notes = memory.read_memories(store, ns)
    assert notes
    assert "me@x.com" not in notes[0]
    assert "supersecret123" not in notes[0]

    # Persists across reopen (durable).
    store2 = memory.open_store(db)
    notes2 = memory.read_memories(store2, ns)
    assert notes2
    assert "[redacted-email]" in notes2[0]


def test_namespace_isolation(tmp_path):
    store = memory.open_store(tmp_path / "m.db")
    memory.write_memory(store, ("memories", "a"), "k", "alpha note")
    memory.write_memory(store, ("memories", "b"), "k", "beta note")
    assert memory.read_memories(store, ("memories", "a")) == ["alpha note"]
    assert memory.read_memories(store, ("memories", "b")) == ["beta note"]


def test_clear_memories(tmp_path):
    store = memory.open_store(tmp_path / "m.db")
    ns = ("memories", "x")
    memory.write_memory(store, ns, "k1", "one")
    memory.write_memory(store, ns, "k2", "two")
    assert memory.clear_memories(store, ns) == 2
    assert memory.read_memories(store, ns) == []


def test_memory_namespace_from_def():
    assert memory.memory_namespace(AgentDef(name="rev")) == ("memories", "rev")
    assert memory.memory_namespace(
        AgentDef(name="rev", memory_namespace="shared")
    ) == ("memories", "shared")


def test_agent_memory_middleware_none_when_disabled():
    assert memory.agent_memory_middleware(AgentDef(name="x", memory=False)) is None


def test_recall_middleware_injects_notes(tmp_path):
    store = memory.open_store(tmp_path / "m.db")
    ns = ("memories", "rev")
    memory.write_memory(store, ns, "k", "prefers tabs over spaces")

    mw = memory.agent_memory_middleware(AgentDef(name="rev"), store=store)
    assert mw is not None

    from langchain_core.messages import SystemMessage

    class _Req:
        def __init__(self):
            self.system_message = SystemMessage(content="Base prompt.")

        def override(self, **kwargs):
            new = _Req()
            new.system_message = kwargs.get("system_message", self.system_message)
            return new

    captured = {}

    def handler(req):
        captured["text"] = req.system_message.content
        return "ok"

    assert mw.wrap_model_call(_Req(), handler) == "ok"
    assert "Base prompt." in captured["text"]
    assert "prefers tabs over spaces" in captured["text"]


def test_recall_middleware_async_injects_notes(tmp_path):
    # deepagents invokes the agent asynchronously (astream/ainvoke), so the async
    # path must also inject recall — not raise NotImplementedError.
    import asyncio

    store = memory.open_store(tmp_path / "m.db")
    memory.write_memory(store, ("memories", "rev"), "k", "prefers tabs over spaces")
    mw = memory.agent_memory_middleware(AgentDef(name="rev"), store=store)

    from langchain_core.messages import SystemMessage

    class _Req:
        def __init__(self):
            self.system_message = SystemMessage(content="Base prompt.")

        def override(self, **kwargs):
            new = _Req()
            new.system_message = kwargs.get("system_message", self.system_message)
            return new

    captured = {}

    async def handler(req):
        captured["text"] = req.system_message.content
        return "ok"

    out = asyncio.run(mw.awrap_model_call(_Req(), handler))
    assert out == "ok"
    assert "Base prompt." in captured["text"]
    assert "prefers tabs over spaces" in captured["text"]


def test_recall_middleware_noop_with_empty_store(tmp_path):
    # An agent with memory enabled but no notes yet must pass through untouched.
    empty = memory.open_store(tmp_path / "empty.db")
    mw = memory.agent_memory_middleware(AgentDef(name="rev"), store=empty)

    class _Req:
        system_message = None

    out = mw.wrap_model_call(_Req(), lambda r: "passthrough")
    assert out == "passthrough"
