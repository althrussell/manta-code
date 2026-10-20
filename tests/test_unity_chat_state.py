"""Unity Chat Gemini thought signatures survive tool replay and checkpoints."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage, messages_from_dict, messages_to_dict
from test_databricks_transport import _chat
from test_databricks_transport import transport_model as transport_model


@pytest.fixture(autouse=True)
def isolated_usage_home(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


def _tool_call(index=1):
    return {"id": f"call_{index}", "type": "function", "thoughtSignature": f"opaque-signature-{index}",
            "function": {"name": "lookup", "arguments": "{}"}}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", [None, "v1"])
def test_chat_signatures_survive_tool_and_next_user_turn(transport_model, asynchronous, output_version):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            message = {"role": "assistant", "content": "", "tool_calls": [_tool_call(1), _tool_call(2)]}
        else:
            message = {"role": "assistant", "content": [{"type": "text", "text": "OK", "thoughtSignature": "opaque-text-signature"}]}
        return httpx.Response(200, json=_chat(message))

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=False,
                              output_version=output_version)

    async def run():
        first = await model.ainvoke("lookup")
        before = copy.deepcopy(first.model_dump())
        restored = messages_from_dict(messages_to_dict([first]))[0]
        history = [HumanMessage(content="lookup"), restored,
                   ToolMessage(content="one", tool_call_id="call_1"), ToolMessage(content="two", tool_call_id="call_2")]
        final = await model.ainvoke(history)
        assert first.model_dump() == before
        await model.ainvoke([*history, final, HumanMessage(content="continue")])

    if asynchronous:
        asyncio.run(run())
    else:
        first = model.invoke("lookup")
        before = copy.deepcopy(first.model_dump())
        restored = messages_from_dict(messages_to_dict([first]))[0]
        history = [HumanMessage(content="lookup"), restored,
                   ToolMessage(content="one", tool_call_id="call_1"), ToolMessage(content="two", tool_call_id="call_2")]
        final = model.invoke(history)
        assert first.model_dump() == before
        model.invoke([*history, final, HumanMessage(content="continue")])
    calls = bodies[1]["messages"][1]["tool_calls"]
    assert {call["id"]: call["thoughtSignature"] for call in calls} == {
        "call_1": "opaque-signature-1", "call_2": "opaque-signature-2",
    }
    assert bodies[2]["messages"][-2]["content"][0]["thoughtSignature"] == "opaque-text-signature"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", [None, "v1"])
def test_chat_stream_signature_replay_and_callback(transport_model, asynchronous, output_version):
    from langchain_core.callbacks import BaseCallbackHandler

    bodies = []
    callback_states = []

    class Callback(BaseCallbackHandler):
        def on_llm_new_token(self, token, *, chunk=None, **kwargs):
            if state := chunk.message.additional_kwargs.get("manta_chat_tool_state"):
                callback_states.append(copy.deepcopy(state))

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json=_chat())
        call = {**_tool_call(), "index": 0}
        events = [{"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0,
                   "model": "fixture", "choices": [{"index": 0, "delta": {
                       "role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": None}]},
                  {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0,
                   "model": "fixture", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]},
                  {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0,
                   "model": "fixture", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}]
        data = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=False,
                              output_version=output_version)

    async def run():
        chunks = [chunk async for chunk in model.astream("lookup", config={"callbacks": [Callback()]})]
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged += chunk
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        await model.ainvoke([HumanMessage(content="lookup"), restored, ToolMessage(content="one", tool_call_id="call_1")])
        return merged

    if asynchronous:
        merged = asyncio.run(run())
    else:
        chunks = list(model.stream("lookup", config={"callbacks": [Callback()]}))
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged += chunk
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        model.invoke([HumanMessage(content="lookup"), restored, ToolMessage(content="one", tool_call_id="call_1")])
    assert bodies[1]["messages"][1]["tool_calls"][0]["thoughtSignature"] == "opaque-signature-1"
    assert merged.usage_metadata["total_tokens"] == 12
    assert callback_states == [{"call_1": {"thoughtSignature": "opaque-signature-1"}}]


def _stream_response(deltas):
    events = [{"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0,
               "model": "fixture", "choices": [{"index": 0, "delta": delta,
               "finish_reason": None}]} for delta in deltas]
    events.append({"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0,
                   "model": "fixture", "choices": [{"index": 0, "delta": {},
                   "finish_reason": "tool_calls"}]})
    data = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})


def _merge_chunks(chunks):
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    return merged


def test_signature_only_chunk_preparation_preserves_wire_data():
    from manta_code.providers.chat_compat import prepare_chat_stream_chunk

    chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "thoughtSignature": "opaque", "function": None},
        {"index": 1, "function": {"arguments": "{}"}},
    ]}}]}
    before = copy.deepcopy(chunk)
    prepared = prepare_chat_stream_chunk(chunk)
    assert chunk == before
    calls = prepared["choices"][0]["delta"]["tool_calls"]
    assert calls[0] == {"index": 0, "thoughtSignature": "opaque", "function": {}}
    assert calls[1] == before["choices"][0]["delta"]["tool_calls"][1]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", [None, "v1"])
def test_late_chat_signatures_follow_interleaved_tool_ids_and_checkpoint(
    transport_model, asynchronous, output_version,
):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json=_chat())
        return _stream_response([
            {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "lookup", "arguments": '{"value":'}},
                {"index": 1, "id": "call_2", "type": "function",
                 "function": {"name": "lookup", "arguments": '{"value":'}},
            ]},
            {"tool_calls": [{"index": 1, "function": {"arguments": '"two"}'},
                             "thoughtSignature": "signature-two"}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"one"}'}}]},
            # A signature-only fragment also need not repeat the call ID.
            {"tool_calls": [{"index": 0, "thoughtSignature": "signature-one"}]},
        ])

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", api="chat",
                              output_version=output_version)

    def history(chunks):
        merged = _merge_chunks(chunks)
        assert [(call["id"], call["args"]) for call in merged.tool_calls] == [
            ("call_1", {"value": "one"}), ("call_2", {"value": "two"}),
        ]
        before = copy.deepcopy(merged.model_dump())
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        assert merged.model_dump() == before
        return [HumanMessage(content="lookup"), restored,
                ToolMessage(content="one", tool_call_id="call_1"),
                ToolMessage(content="two", tool_call_id="call_2")]

    async def run():
        chunks = [chunk async for chunk in model.astream("lookup")]
        await model.ainvoke(history(chunks))

    if asynchronous:
        asyncio.run(run())
    else:
        model.invoke(history(list(model.stream("lookup"))))
    calls = bodies[1]["messages"][1]["tool_calls"]
    assert {call["id"]: call["thoughtSignature"] for call in calls} == {
        "call_1": "signature-one", "call_2": "signature-two",
    }


@pytest.mark.parametrize("asynchronous", [False, True])
def test_overlapping_chat_streams_keep_fragment_identities_isolated(transport_model, asynchronous):
    def handler(request):
        label = json.loads(request.content)["messages"][-1]["content"]
        first = {"index": 0, "type": "function",
                 "function": {"name": "lookup", "arguments": "{}"}}
        if label != "orphan":
            first["id"] = f"call_{label}"
        return _stream_response([
            {"role": "assistant", "tool_calls": [first]},
            {"tool_calls": [{"index": 0, "thoughtSignature": f"signature-{label}"}]},
        ])

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", api="chat")

    async def run():
        one, two = model.astream("one"), model.astream("two")
        first_one, first_two = await anext(one), await anext(two)

        async def collect(stream, first):
            return _merge_chunks([first, *[chunk async for chunk in stream]])

        results = await asyncio.gather(collect(one, first_one), collect(two, first_two))
        orphan = _merge_chunks([chunk async for chunk in model.astream("orphan")])
        return results, orphan

    if asynchronous:
        results, orphan = asyncio.run(run())
    else:
        one, two = model.stream("one"), model.stream("two")
        first_one, first_two = next(one), next(two)
        results = [_merge_chunks([first_one, *one]), _merge_chunks([first_two, *two])]
        orphan = _merge_chunks(list(model.stream("orphan")))
    for label, result in zip(("one", "two"), results, strict=True):
        assert result.additional_kwargs["manta_chat_tool_state"] == {
            f"call_{label}": {"thoughtSignature": f"signature-{label}"},
        }
    # A fresh stream with no established identity must never reuse an old one.
    assert "manta_chat_tool_state" not in orphan.additional_kwargs


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", [None, "v1"])
def test_repeated_chat_tool_signatures_are_preserved_once_per_call(
    transport_model, asynchronous, output_version,
):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json=_chat())
        return _stream_response([
            {"role": "assistant", "tool_calls": [
                {"index": index, "id": f"call_{index}", "type": "function",
                 "function": {"name": "lookup", "arguments": "{}"},
                 "thoughtSignature": "same-opaque-value"} for index in (0, 1)
            ]},
            {"tool_calls": [{"index": 0, "thoughtSignature": "same-opaque-value",
                             "encrypted_content": "separate-opaque-field"}]},
            {"tool_calls": [{"index": 1, "thoughtSignature": "same-opaque-value",
                             "extra_content": {"google": {"thought_signature": "nested-opaque"}}}]},
            {"tool_calls": [{"index": 1,
                             "extra_content": {"google": {"thought_signature": "nested-opaque"}}}]},
        ])

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", api="chat",
                              output_version=output_version)
    expected = {
        "call_0": {"thoughtSignature": "same-opaque-value",
                   "encrypted_content": "separate-opaque-field"},
        "call_1": {"thoughtSignature": "same-opaque-value",
                   "extra_content": {"google": {"thought_signature": "nested-opaque"}}},
    }

    def history(chunks):
        merged = _merge_chunks(chunks)
        assert merged.additional_kwargs["manta_chat_tool_state"] == expected
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        return [HumanMessage(content="lookup"), restored,
                ToolMessage(content="zero", tool_call_id="call_0"),
                ToolMessage(content="one", tool_call_id="call_1")]

    async def run():
        await model.ainvoke(history([chunk async for chunk in model.astream("lookup")]))

    if asynchronous:
        asyncio.run(run())
    else:
        model.invoke(history(list(model.stream("lookup"))))
    for call in bodies[1]["messages"][1]["tool_calls"]:
        assert {key: call[key] for key in expected[call["id"]]} == expected[call["id"]]
