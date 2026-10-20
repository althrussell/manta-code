from __future__ import annotations

import json

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langchain_openai")

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)

from manta_code import databricks_chat as dc

REASONING_AND_TEXT = json.dumps(
    [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "private thoughts"}],
        },
        {"type": "text", "text": "Hello!"},
    ]
)
REASONING_ONLY = json.dumps(
    [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "hmm"}]}]
)


# --- _coerce_block_list ---------------------------------------------------------


def test_coerce_parses_serialized_block_list():
    blocks = dc._coerce_block_list(REASONING_AND_TEXT)
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["reasoning", "text"]


def test_coerce_accepts_already_parsed_list():
    parsed = json.loads(REASONING_AND_TEXT)
    assert dc._coerce_block_list(parsed) == parsed


def test_coerce_rejects_plain_text_delta():
    # Streamed answer deltas arrive as plain strings, not block JSON.
    assert dc._coerce_block_list("LO") is None
    assert dc._coerce_block_list("") is None


def test_coerce_rejects_unrelated_json_array():
    # A model legitimately answering with a JSON array must not be mangled.
    assert dc._coerce_block_list("[1, 2, 3]") is None
    assert dc._coerce_block_list('[{"foo": "bar"}]') is None
    assert dc._coerce_block_list('[{"type": "image"}]') is None


def test_coerce_rejects_non_string_non_list():
    assert dc._coerce_block_list(None) is None
    assert dc._coerce_block_list(42) is None


# --- _visible_text --------------------------------------------------------------


def test_visible_text_keeps_text_drops_reasoning():
    assert dc._visible_text(REASONING_AND_TEXT) == "Hello!"


def test_visible_text_reasoning_only_is_empty():
    assert dc._visible_text(REASONING_ONLY) == ""


def test_visible_text_passthrough_returns_none():
    assert dc._visible_text("just a normal answer") is None


# --- _normalize_message ---------------------------------------------------------


def test_normalize_message_rewrites_block_content():
    msg = AIMessage(content=REASONING_AND_TEXT, id="abc")
    out = dc._normalize_message(msg)
    assert out.content == "Hello!"
    assert out.id == "abc"


def test_normalize_message_preserves_tool_calls():
    msg = AIMessage(
        content=REASONING_ONLY,
        tool_calls=[{"name": "ls", "args": {}, "id": "t1"}],
    )
    out = dc._normalize_message(msg)
    assert out.content == ""
    assert out.tool_calls == [{"name": "ls", "args": {}, "id": "t1", "type": "tool_call"}]


def test_normalize_message_leaves_plain_content_identical():
    msg = AIMessage(content="plain answer")
    assert dc._normalize_message(msg) is msg


# --- _normalize_chunk -----------------------------------------------------------


def test_normalize_chunk_drops_reasoning():
    chunk = ChatGenerationChunk(message=AIMessageChunk(content=REASONING_ONLY))
    out = dc._normalize_chunk(chunk)
    assert out.message.content == ""


def test_normalize_chunk_passes_through_plain_delta():
    chunk = ChatGenerationChunk(message=AIMessageChunk(content="LO"))
    out = dc._normalize_chunk(chunk)
    assert out is chunk


# --- subclass overrides ---------------------------------------------------------


def test_generate_normalizes_result(monkeypatch):
    from langchain_openai import ChatOpenAI

    def fake_generate(self, *_a, **_k):
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content=REASONING_AND_TEXT))
            ]
        )

    monkeypatch.setattr(ChatOpenAI, "_generate", fake_generate, raising=True)
    model = dc.MantaChatDatabricks(model="endpoint-x")
    result = model._generate([])
    assert result.generations[0].message.content == "Hello!"


def test_astream_normalizes_chunks(monkeypatch):
    import asyncio

    from langchain_openai import ChatOpenAI

    async def fake_astream(self, *_a, **_k):
        yield ChatGenerationChunk(message=AIMessageChunk(content=REASONING_ONLY))
        yield ChatGenerationChunk(message=AIMessageChunk(content="Hel"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="lo"))

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)
    monkeypatch.setattr(dc.MantaChatDatabricks, "_root", lambda *_a, **_k: None)
    model = dc.MantaChatDatabricks(model="endpoint-x")

    async def collect():
        return [chunk.message.content async for chunk in model._astream([])]

    assert asyncio.run(collect()) == ["", "Hel", "lo"]


# --- subagent model resolver ----------------------------------------------------


def test_install_resolver_is_idempotent():
    assert dc._install_subagent_databricks_resolver() is True
    # A second call is a no-op (already installed) and still reports success.
    assert dc._install_subagent_databricks_resolver() is True


def test_resolver_builds_manta_chat_for_databricks_spec():
    # Importing the module installed the shim; the deepagents resolver now
    # understands databricks:<endpoint> specs that langchain cannot infer.
    from deepagents import _models

    model = _models.resolve_model("databricks:databricks-claude-opus-4-8")
    assert isinstance(model, dc.MantaChatDatabricks)
    assert model.model == "databricks-claude-opus-4-8"


def test_resolver_patches_graph_module_level_binding():
    # deepagents.graph binds resolve_model at import (top-level import), which is
    # the path create_deep_agent uses for subagents. The shim must rebind it too,
    # otherwise subagent models pinned to databricks:<endpoint> fail to resolve.
    from deepagents import graph

    # Ensure the shim's rebind reached an already-imported graph module.
    dc._install_subagent_databricks_resolver()
    model = graph.resolve_model("databricks:databricks-gpt-5-4")
    assert isinstance(model, dc.MantaChatDatabricks)
    assert model.model == "databricks-gpt-5-4"


def test_resolver_defers_non_databricks_specs(monkeypatch):
    from deepagents import _models

    seen = {}

    def fake_original(model):
        seen["model"] = model
        return "ORIGINAL"

    # Re-run the installer against a stubbed original to prove non-databricks
    # specs fall through to it untouched.
    monkeypatch.setattr(_models, "resolve_model", fake_original)
    monkeypatch.setattr(dc, "_resolver_installed", False)
    dc._install_subagent_databricks_resolver()

    assert _models.resolve_model("openai:gpt-5.5") == "ORIGINAL"
    assert seen["model"] == "openai:gpt-5.5"
    # databricks specs are intercepted before reaching the original.
    assert isinstance(
        _models.resolve_model("databricks:databricks-gpt-5-4"), dc.MantaChatDatabricks
    )


# --- streaming usage dedup (ADR 0010 Phase C accounting fix) --------------------


def test_usage_delta_converts_cumulative_to_increments():
    prev: dict = {}
    first = dc._usage_delta(prev, {"input_tokens": 105, "output_tokens": 10, "total_tokens": 115})
    assert first == {"input_tokens": 105, "output_tokens": 10, "total_tokens": 115}
    second = dc._usage_delta(
        {"input_tokens": 105, "output_tokens": 10, "total_tokens": 115},
        {"input_tokens": 105, "output_tokens": 25, "total_tokens": 130},
    )
    # Input already paid for: delta 0. Output grew by 15.
    assert second == {"input_tokens": 0, "output_tokens": 15, "total_tokens": 15}


def test_usage_delta_handles_nested_details_and_clamps_negative():
    delta = dc._usage_delta(
        {"input_tokens": 100, "input_token_details": {"cache_read": 80}},
        {"input_tokens": 90, "input_token_details": {"cache_read": 80}},
    )
    assert delta["input_tokens"] == 0  # restated lower total is noise, not refund
    assert delta["input_token_details"] == {"cache_read": 0}


def test_stream_dedup_makes_additive_merge_equal_final_totals():
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    # databricks-langchain attaches *cumulative* usage to every chunk; the
    # additive chunk merge must come out equal to the final cumulative totals.
    cumulative = [
        {"input_tokens": 105, "output_tokens": 10, "total_tokens": 115},
        {"input_tokens": 105, "output_tokens": 30, "total_tokens": 135},
        {"input_tokens": 105, "output_tokens": 51, "total_tokens": 156},
    ]
    dedup = dc._StreamUsageDeduplicator()
    merged = None
    for i, usage in enumerate(cumulative):
        chunk = ChatGenerationChunk(
            message=AIMessageChunk(content=f"c{i}", usage_metadata=dict(usage))
        )
        rewritten = dedup.rewrite(chunk)
        merged = rewritten.message if merged is None else merged + rewritten.message
    assert merged.usage_metadata == {
        "input_tokens": 105,
        "output_tokens": 51,
        "total_tokens": 156,
    }


def test_stream_dedup_passes_chunks_without_usage():
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    dedup = dc._StreamUsageDeduplicator()
    chunk = ChatGenerationChunk(message=AIMessageChunk(content="hello"))
    assert dedup.rewrite(chunk) is chunk


@pytest.mark.parametrize("asynchronous", [False, True])
def test_internal_stream_callbacks_see_normalized_usage_and_visible_content(monkeypatch, asynchronous):
    import asyncio
    from types import SimpleNamespace
    from langchain_openai import ChatOpenAI

    chunks = [
        ChatGenerationChunk(message=AIMessageChunk(content=REASONING_ONLY, usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11})),
        ChatGenerationChunk(message=AIMessageChunk(content="visible", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})),
    ]
    def stream(self, *args, **kwargs):
        assert kwargs["run_manager"] is None
        yield from chunks
    async def astream(self, *args, **kwargs):
        assert kwargs["run_manager"] is None
        for chunk in chunks:
            yield chunk
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    monkeypatch.setattr(ChatOpenAI, "_astream", astream)
    monkeypatch.setattr(dc.MantaChatDatabricks, "_root", lambda *a, **k: None)
    seen = []
    def token(text, *, chunk, **kwargs):
        seen.append((text, chunk.message.usage_metadata))
    async def atoken(text, **kwargs):
        token(text, **kwargs)
    manager = SimpleNamespace(on_llm_new_token=atoken if asynchronous else token)
    model = dc.MantaChatDatabricks(model="legacy-chat")
    async def collect():
        return [chunk async for chunk in model._astream([], run_manager=manager)]
    result = asyncio.run(collect()) if asynchronous else list(model._stream([], run_manager=manager))
    assert [text for text, usage in seen] == ["", "visible"]
    assert [usage["input_tokens"] for text, usage in seen] == [10, 0]
    assert [usage["output_tokens"] for text, usage in seen] == [1, 1]
    assert [chunk.message.usage_metadata for chunk in result] == [usage for text, usage in seen]
