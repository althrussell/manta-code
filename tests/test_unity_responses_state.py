"""Gemini's opaque Unity function-call signatures survive full conversations."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict

from test_databricks_transport import _response
from test_databricks_transport import transport_model as transport_model

from manta_code.providers.response_compat import restore_responses_state


def function_call(index, *, arguments="{}", encrypted=True):
    result = {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
              "name": "lookup", "arguments": arguments, "status": "completed"}
    if encrypted:
        result["encrypted_content"] = f"opaque-gemini-signature-{index}"
    return result


def test_restore_standard_content_extra_state_is_immutable():
    message = AIMessage(content=[{
        "type": "tool_call", "id": "call_1", "name": "lookup", "args": {},
        "extras": {"encrypted_content": "opaque", "thought_signature": "native-opaque"},
    }], tool_calls=[{"name": "lookup", "args": {}, "id": "call_1", "type": "tool_call"}])
    payload = {"input": [{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"}]}
    before = copy.deepcopy((message.model_dump(), payload))
    result = restore_responses_state(payload, [message])
    assert result["input"][0]["encrypted_content"] == "opaque"
    assert result["input"][0]["thought_signature"] == "native-opaque"
    assert (message.model_dump(), payload) == before


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", ["responses/v1", "v1"])
def test_responses_invoke_parallel_function_opaque_replay(transport_model, asynchronous, output_version):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        output = [function_call(1), function_call(2)] if len(bodies) == 1 else []
        return httpx.Response(200, json=_response(output))

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=True,
                              output_version=output_version)

    async def run():
        first = await model.ainvoke("lookup")
        before = copy.deepcopy(first.model_dump())
        # Model history/checkpoint serialization must retain provider state.
        restored = messages_from_dict(messages_to_dict([first]))[0]
        await model.ainvoke([HumanMessage(content="lookup"), restored,
                             ToolMessage(content="one", tool_call_id="call_1"),
                             ToolMessage(content="two", tool_call_id="call_2")])
        assert first.model_dump() == before

    if asynchronous:
        asyncio.run(run())
    else:
        first = model.invoke("lookup")
        before = copy.deepcopy(first.model_dump())
        restored = messages_from_dict(messages_to_dict([first]))[0]
        model.invoke([HumanMessage(content="lookup"), restored,
                      ToolMessage(content="one", tool_call_id="call_1"),
                      ToolMessage(content="two", tool_call_id="call_2")])
        assert first.model_dump() == before
    calls = [item for item in bodies[1]["input"] if item.get("type") == "function_call"]
    assert len(calls) == 2
    assert {item["call_id"]: item["encrypted_content"] for item in calls} == {
        "call_1": "opaque-gemini-signature-1", "call_2": "opaque-gemini-signature-2",
    }


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("output_version", ["responses/v1", "v1"])
def test_responses_stream_function_done_state_replayed_once(transport_model, asynchronous, output_version):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json=_response([]))
        events = [{"type": "response.created", "response": _response([])}]
        # Added events may contain incomplete state. Only done is authoritative.
        for index in (1, 2):
            added = function_call(index, arguments="", encrypted=False)
            events.append({"type": "response.output_item.added", "output_index": index - 1, "item": added})
            events.append({"type": "response.function_call_arguments.delta", "output_index": index - 1,
                           "item_id": f"fc_{index}", "delta": "{}"})
            events.append({"type": "response.output_item.done", "output_index": index - 1,
                           "item": function_call(index)})
        events.append({"type": "response.completed", "response": _response([function_call(1), function_call(2)])})
        data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=True,
                              output_version=output_version)

    async def run():
        chunks = [chunk async for chunk in model.astream("lookup")]
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged += chunk
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        await model.ainvoke([HumanMessage(content="lookup"), restored,
                             ToolMessage(content="one", tool_call_id="call_1"),
                             ToolMessage(content="two", tool_call_id="call_2")])
        return merged

    if asynchronous:
        merged = asyncio.run(run())
    else:
        chunks = list(model.stream("lookup"))
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged += chunk
        restored = messages_from_dict(messages_to_dict([merged]))[0]
        model.invoke([HumanMessage(content="lookup"), restored,
                      ToolMessage(content="one", tool_call_id="call_1"),
                      ToolMessage(content="two", tool_call_id="call_2")])
    assert merged.usage_metadata["total_tokens"] == 13
    calls = [item for item in bodies[1]["input"] if item.get("type") == "function_call"]
    assert len(calls) == 2
    assert {item["call_id"]: item["encrypted_content"] for item in calls} == {
        "call_1": "opaque-gemini-signature-1", "call_2": "opaque-gemini-signature-2",
    }


def test_responses_state_passes_real_agent_tool_loop(transport_model):
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    bodies = []

    @tool
    def lookup() -> str:
        """Return the fixture result."""
        return "fixture result"

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json=_response([function_call(1)]))
        call = next(item for item in body["input"] if item.get("type") == "function_call")
        assert call["encrypted_content"] == "opaque-gemini-signature-1"
        assert any(item.get("type") == "function_call_output" for item in body["input"])
        return httpx.Response(200, json=_response([{
            "type": "message", "id": "msg_done", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": "fixture result", "annotations": []}],
        }]))

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=True)
    agent = create_agent(model=model, tools=[lookup])
    final = agent.invoke({"messages": [HumanMessage(content="lookup")]}, config={"recursion_limit": 5})
    assert final["messages"][-1].text == "fixture result"
    assert len(bodies) == 2


def test_scoped_stream_converter_leaves_other_openai_converters_unchanged():
    from types import SimpleNamespace
    from langchain_openai.chat_models import base
    from manta_code.providers.response_compat import install_stream_converter

    install_stream_converter()
    event = SimpleNamespace(type="response.output_item.done", output_index=0,
                            item=SimpleNamespace(type="function_call", id="fc_1", encrypted_content="opaque"))
    # Databricks compatibility is inactive outside an advancing Manta generator.
    assert base._convert_responses_chunk_to_generation_chunk(event, 0, 0, 0)[-1] is None


def test_cached_v1_response_preserves_function_state_for_followup(transport_model):
    from langchain_core.caches import InMemoryCache

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_response([function_call(1)] if len(bodies) == 1 else []))

    model, _ = transport_model(handler, model="system.ai.gemini-fixture", use_responses_api=True,
                              output_version="v1", cache=InMemoryCache())
    first = model.invoke("lookup")
    cached = model.invoke("lookup")
    assert len(bodies) == 1
    assert cached.additional_kwargs == first.additional_kwargs
    model.invoke([HumanMessage(content="lookup"), cached, ToolMessage(content="one", tool_call_id="call_1")])
    call = next(item for item in bodies[1]["input"] if item.get("type") == "function_call")
    assert call["encrypted_content"] == "opaque-gemini-signature-1"
