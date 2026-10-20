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
