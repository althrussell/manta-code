"""Offline wire contracts for the Databricks OpenAI-compatible adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("langchain_openai")

import httpx
from langchain_core.messages import HumanMessage, ToolMessage

from manta_code.databricks_chat import MantaChatDatabricks


def _chat(message=None, usage=None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message or {
            "role": "assistant", "content": "OK"
        }}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def _response(output):
    return {
        "id": "resp_fixture", "object": "response", "created_at": 0,
        "status": "completed", "model": "fixture", "output": output,
        "error": None, "incomplete_details": None, "service_tier": "default",
        "usage": {
            "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


@pytest.mark.parametrize("dialect", ["chat", "responses"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_structured_output_schema_and_cap_reach_official_wire(transport_model, dialect, asynchronous):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = _chat({"role": "assistant", "content": '{"answer":"OK"}'}) if dialect == "chat" else _response([{
            "type": "message", "id": "msg-structured", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": '{"answer":"OK"}', "annotations": []}],
        }])
        return httpx.Response(200, json=response)

    model, _ = transport_model(handler, model="custom.team.structured", api=dialect, max_tokens=128)
    schema = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
    structured = model.with_structured_output(schema, method="json_schema", strict=True)
    result = asyncio.run(structured.ainvoke("Answer", max_output_tokens=19)) if asynchronous else structured.invoke("Answer", max_output_tokens=19)
    assert result == {"answer": "OK"}
    payload = requests[0]
    format_ = payload["response_format"]["json_schema"] if dialect == "chat" else payload["text"]["format"]
    assert format_["name"] == "Answer"
    assert format_["strict"] is True
    assert format_["schema"]["properties"] == schema["properties"]
    assert payload["max_tokens" if dialect == "chat" else "max_output_tokens"] == 19


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_refusal_remains_explicit_provider_state(transport_model, dialect):
    refusal = "Fixture service refused this request"

    def handler(request):
        response = _chat({"role": "assistant", "content": None, "refusal": refusal}) if dialect == "chat" else _response([{
            "type": "message", "id": "msg-refusal", "role": "assistant", "status": "completed",
            "content": [{"type": "refusal", "refusal": refusal}],
        }])
        return httpx.Response(200, json=response)

    model, _ = transport_model(handler, model="custom.team.refusal", api=dialect)
    result = model.invoke("test")
    if dialect == "chat":
        assert result.additional_kwargs["refusal"] == refusal
    else:
        assert any(block.get("type") == "refusal" and block["refusal"] == refusal for block in result.content)
    assert result.text == ""


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_multimodal_image_and_file_inputs_preserve_content(transport_model, dialect):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat() if dialect == "chat" else _response([]))

    model, _ = transport_model(handler, model="custom.team.multimodal", api=dialect)
    image = "data:image/png;base64,aW1hZ2U="
    file = "data:application/pdf;base64,cGRm"
    content = [{"type": "text", "text": "Describe"}, {"type": "image_url", "image_url": {"url": image, "detail": "low"}}, {"type": "file", "file": {"filename": "fixture.pdf", "file_data": file}}]
    model.invoke([HumanMessage(content=content)])
    payload = requests[0]
    actual = payload["messages"][0]["content"] if dialect == "chat" else payload["input"][0]["content"]
    if dialect == "chat":
        assert actual == content
    else:
        assert actual == [{"type": "input_text", "text": "Describe"}, {"type": "input_image", "image_url": image, "detail": "low"}, {"type": "input_file", "filename": "fixture.pdf", "file_data": file}]


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_forced_tool_selection_keeps_selected_function_on_wire(transport_model, dialect):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat() if dialect == "chat" else _response([]))

    model, _ = transport_model(handler, model="custom.team.tool-capable", api=dialect)
    tools = [{"type": "function", "function": {"name": "echo", "description": "Echo", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}]
    model.bind_tools(tools, tool_choice="echo").invoke("Echo")
    expected = {"type": "function", "function": {"name": "echo"}} if dialect == "chat" else {"type": "function", "name": "echo"}
    assert requests[0]["tool_choice"] == expected


@pytest.fixture
def transport_model():
    clients = []

    def build(handler, **kwargs):
        auth_calls = []

        def authenticate():
            auth_calls.append(threading.get_ident())
            return {
                "Authorization": f"Bearer fixture-{len(auth_calls)}",
                "X-Databricks-Azure-SP-Management-Token": f"auxiliary-{len(auth_calls)}",
                "X-Databricks-Azure-Workspace-Resource-Id": "fixture-resource",
            }

        workspace = SimpleNamespace(config=SimpleNamespace(
            host="https://workspace.invalid", authenticate=authenticate
        ))
        transport = httpx.MockTransport(handler)
        sync = httpx.Client(transport=transport)
        asynchronous = httpx.AsyncClient(transport=transport)
        clients.append((sync, asynchronous))
        kwargs.setdefault("max_retries", 0)
        model = MantaChatDatabricks(
            workspace_client=workspace, http_client=sync, http_async_client=asynchronous,
            **kwargs
        )
        return model, auth_calls

    yield build
    for sync, asynchronous in clients:
        sync.close()
        asyncio.run(asynchronous.aclose())


@pytest.mark.parametrize(("name", "flags", "path"), [
    ("legacy-chat", {}, "/serving-endpoints/chat/completions"),
    ("system.ai.exact-model", {}, "/ai-gateway/mlflow/v1/responses"),
    ("custom.schema.exact_model", {"use_responses_api": True}, "/ai-gateway/mlflow/v1/responses"),
    ("system.ai.exact-model", {"use_ai_gateway": True}, "/ai-gateway/mlflow/v1/responses"),
    ("system.ai.exact-model", {"target_kind": "endpoint", "use_ai_gateway": False}, "/serving-endpoints/chat/completions"),
    ("system.ai.exact-model", {"use_ai_gateway_native_api": True}, "/ai-gateway/openai/v1/responses"),
])
def test_exact_model_and_workspace_routes(transport_model, monkeypatch, name, flags, path):
    requests = []

    def handler(request):
        requests.append(request)
        body = _response([]) if path.endswith("/responses") else _chat()
        return httpx.Response(200, json=body)

    monkeypatch.setenv("OPENAI_API_BASE", "https://unrelated.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    model, auth_calls = transport_model(handler, model=name, **flags)
    assert auth_calls == []
    model.invoke("test")
    assert requests[0].url.host == "workspace.invalid"
    assert requests[0].url.path == path
    assert json.loads(requests[0].content)["model"] == name
    assert requests[0].headers["authorization"] == "Bearer fixture-1"
    assert model._get_ls_params()["ls_provider"] == "databricks"


def test_auth_refreshes_for_sync_and_async_requests(transport_model):
    authorizations = []
    auxiliary = []

    def handler(request):
        authorizations.append(request.headers["authorization"])
        auxiliary.append(request.headers["x-databricks-azure-sp-management-token"])
        assert request.headers["x-databricks-azure-workspace-resource-id"] == "fixture-resource"
        return httpx.Response(200, json=_chat())

    model, auth_calls = transport_model(handler, model="legacy-chat", timeout=17)
    model.invoke("one")
    model.invoke("two")

    async def run():
        await model.ainvoke("three")
        await model.ainvoke("four")

    asyncio.run(run())
    assert authorizations == [f"Bearer fixture-{i}" for i in range(1, 5)]
    assert auxiliary == [f"auxiliary-{i}" for i in range(1, 5)]
    assert auth_calls[:2] == [threading.get_ident()] * 2
    assert all(ident != threading.get_ident() for ident in auth_calls[2:])
    assert model._root(False).timeout == 17
    assert model._root(False).max_retries == 0
    assert model._root(True).max_retries == 0


@pytest.mark.parametrize("asynchronous", [False, True])
def test_owned_clients_preserve_openai_sdk_defaults(monkeypatch, asynchronous):
    import openai

    created_limits = []
    limits_class = httpx.Limits

    def record_limits(**kwargs):
        limits = limits_class(**kwargs)
        created_limits.append(limits)
        return limits

    monkeypatch.setattr(httpx, "Limits", record_limits)
    workspace = SimpleNamespace(config=SimpleNamespace(host="https://workspace.invalid"))
    model = MantaChatDatabricks(model="legacy-chat", workspace_client=workspace)
    root = model._root(asynchronous)
    client = root._client
    assert client.timeout.as_dict() == openai.DEFAULT_TIMEOUT.as_dict()
    assert client.follow_redirects is True
    assert created_limits[0].max_connections == openai.DEFAULT_CONNECTION_LIMITS.max_connections
    assert (created_limits[0].max_keepalive_connections
            == openai.DEFAULT_CONNECTION_LIMITS.max_keepalive_connections)
    assert created_limits[0].keepalive_expiry == openai.DEFAULT_CONNECTION_LIMITS.keepalive_expiry
    if asynchronous:
        asyncio.run(root.close())
    else:
        root.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_custom_http_clients_keep_settings_hooks_and_ownership(transport_model, asynchronous):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_chat())

    model, _ = transport_model(handler, model="legacy-chat")
    client = model.http_async_client if asynchronous else model.http_client
    original_auth = httpx.BasicAuth("caller", "not-for-databricks")
    client.auth = original_auth
    client.headers["x-caller-header"] = "retained"
    client.timeout = httpx.Timeout(23)
    hooks = []
    if asynchronous:
        async def hook(request):
            hooks.append(request)
    else:
        def hook(request):
            hooks.append(request)
    client.event_hooks["request"].append(hook)
    original_headers = dict(client.headers)
    original_hooks = {name: list(handlers) for name, handlers in client.event_hooks.items()}

    async def run():
        await model.ainvoke("test")
        root = model._root(True)
        assert root.timeout.read == 23
        await root.close()

    if asynchronous:
        asyncio.run(run())
    else:
        model.invoke("test")
        root = model._root(False)
        assert root.timeout.read == 23
        root.close()

    assert requests[0].headers["authorization"] == "Bearer fixture-1"
    assert requests[0].headers["x-caller-header"] == "retained"
    assert hooks == requests
    assert client.auth is original_auth
    assert dict(client.headers) == original_headers
    assert client.event_hooks == original_hooks
    assert not client.is_closed
    assert model._root(asynchronous).is_closed()


@pytest.mark.parametrize("streaming", [False, True])
def test_first_async_workspace_initialization_is_off_loop(transport_model, monkeypatch, streaming):
    from manta_code import auth

    def handler(request):
        if streaming:
            chunk = {"id": "chunk_1", "object": "chat.completion.chunk", "created": 0,
                     "model": "fixture", "choices": [{"index": 0, "delta": {
                         "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_chat())

    model, _ = transport_model(handler, model="legacy-chat")
    workspace = model.workspace_client
    model.workspace_client = None
    construction_threads = []

    def resolve():
        construction_threads.append(threading.get_ident())
        return workspace

    monkeypatch.setattr(auth, "resolve_workspace_client", resolve)

    async def run():
        if streaming:
            chunks = [chunk async for chunk in model.astream("test")]
            assert "".join(chunk.content for chunk in chunks) == "OK"
        else:
            await model.ainvoke("test")

    asyncio.run(run())
    assert len(construction_threads) == 1
    assert construction_threads[0] != threading.get_ident()


@pytest.mark.parametrize("responses", [False, True])
def test_legacy_extra_params_reach_the_wire(transport_model, responses):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_response([]) if responses else _chat())

    model, _ = transport_model(
        handler, model="system.ai.example", use_responses_api=responses,
        extra_params={
            "max_tokens": 32, "top_k": 20, "custom_inputs": {"legacy": True},
            "tool_choice": "auto", "parallel_tool_calls": False,
        },
    )
    model.invoke("test")
    assert bodies[0]["max_output_tokens" if responses else "max_tokens"] == 32
    assert bodies[0]["top_k"] == 20
    assert bodies[0]["custom_inputs"] == {"legacy": True}
    assert bodies[0]["tool_choice"] == "auto"
    assert bodies[0]["parallel_tool_calls"] is False
    model.invoke("request options", custom_inputs={"request": True})
    assert bodies[1]["custom_inputs"] == {"request": True}
    assert bodies[1]["top_k"] == 20


def test_explicit_options_override_legacy_extra_params(transport_model):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_chat())

    extra = {"max_tokens": 32, "top_k": 20, "custom_inputs": {"legacy": True},
             "tool_choice": "none", "parallel_tool_calls": False}
    model_kwargs = {"tool_choice": "auto", "top_k": 30}
    model, _ = transport_model(
        handler, model="legacy-chat", extra_params=extra, max_completion_tokens=64,
        model_kwargs=model_kwargs, extra_body={"custom_inputs": {"explicit": True}},
        parallel_tool_calls=True,
    )
    model.invoke("test")
    assert bodies[0]["max_tokens"] == 64
    assert bodies[0]["top_k"] == 30
    assert bodies[0]["custom_inputs"] == {"explicit": True}
    assert bodies[0]["tool_choice"] == "auto"
    assert bodies[0]["parallel_tool_calls"] is True
    assert extra["max_tokens"] == 32
    assert model_kwargs == {"tool_choice": "auto", "top_k": 30}


def test_construction_does_not_require_workspace_auth(monkeypatch):
    from manta_code import auth, providers

    def unavailable(*args, **kwargs):
        raise AssertionError("model registration must not authenticate")

    monkeypatch.setattr(auth, "resolve_workspace_client", unavailable)
    model = providers.resolve_model_ref("databricks:system.ai.example")
    assert isinstance(model, MantaChatDatabricks)
    assert model.model == "system.ai.example"


def test_explicit_model_profile_survives_adapter_construction():
    profile = {"max_input_tokens": 8192, "tool_calling": True}
    model = MantaChatDatabricks(model="legacy-chat", profile=profile)
    assert model.profile == profile
    assert "profile" not in model._get_request_payload("test")


def test_conflicting_gateway_flags_are_rejected():
    with pytest.raises(ValueError, match="either unified or native"):
        MantaChatDatabricks(model="system.ai.example", use_ai_gateway=True,
                            use_ai_gateway_native_api=True)


def test_custom_base_url_cannot_receive_credentials_on_another_origin(transport_model):
    model, auth_calls = transport_model(
        lambda request: pytest.fail("unexpected request"), model="legacy-chat",
        base_url="https://unrelated.invalid/serving-endpoints"
    )
    with pytest.raises(ValueError, match="workspace origin"):
        model.invoke("test")
    assert auth_calls == []


def test_chat_tool_roundtrip_preserves_claude_compatibility(transport_model):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, json=_chat({"role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "lookup", "arguments": '{"key":"value"}'
                }}]}))
        return httpx.Response(200, json=_chat())

    model, _ = transport_model(handler, model="databricks-claude-test", max_tokens=32)
    bound = model.bind_tools([{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object"}, "strict": True
    }}])
    first = bound.invoke("look up")
    final = bound.invoke([HumanMessage(content="look up"), first,
                          ToolMessage(content="found", tool_call_id="call_1")])
    assert first.tool_calls[0]["args"] == {"key": "value"}
    assert final.content == "OK"
    assert final.usage_metadata["total_tokens"] == 12
    assert bodies[0]["max_tokens"] == 32
    assert "strict" not in bodies[0]["tools"][0]["function"]
    assert bodies[1]["messages"][1]["content"] == " "
    assert bodies[1]["messages"][2]["tool_call_id"] == "call_1"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_responses_replay_keeps_opaque_state_and_one_tool_call(transport_model, asynchronous):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        output = [{"type": "reasoning", "id": "rs_1", "summary": [],
                   "encrypted_content": "opaque-state"},
                  {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                   "name": "lookup", "arguments": "{}", "status": "completed"}]
        return httpx.Response(200, json=_response(output if len(bodies) == 1 else []))

    model, _ = transport_model(handler, model="system.ai.example", use_responses_api=True,
                              max_tokens=32)
    bound = model.bind_tools([{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object"}
    }}], tool_choice="lookup")
    async def run():
        first = await bound.ainvoke("look up")
        original = copy.deepcopy(first.model_dump())
        await bound.ainvoke([HumanMessage(content="look up"), first,
                            ToolMessage(content="found", tool_call_id="call_1")])
        return first, original

    if asynchronous:
        first, original = asyncio.run(run())
    else:
        first = bound.invoke("look up")
        original = copy.deepcopy(first.model_dump())
        bound.invoke([HumanMessage(content="look up"), first,
                      ToolMessage(content="found", tool_call_id="call_1")])
    assert first.model_dump() == original
    assert bodies[0]["max_output_tokens"] == 32
    assert bodies[0]["tool_choice"] == {"type": "function", "name": "lookup"}
    calls = [item for item in bodies[1]["input"] if item.get("type") == "function_call"]
    assert len(calls) == 1
    assert calls[0]["call_id"] == "call_1"
    reasoning = next(item for item in bodies[1]["input"] if item.get("type") == "reasoning")
    assert reasoning["id"] == "rs_1"
    assert reasoning["encrypted_content"] == "opaque-state"
    assert first.usage_metadata["input_token_details"]["cache_read"] == 2
    assert first.usage_metadata["output_token_details"]["reasoning"] == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("service", [False, True])
def test_chat_stream_usage_and_text(transport_model, asynchronous, service):
    def handler(request):
        events = []
        for index, text in enumerate(["Hel", "lo"]):
            chunk = {"id": "chunk_1", "object": "chat.completion.chunk", "created": 0,
                     "model": "fixture", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
            if not service:
                chunk["usage"] = {"prompt_tokens": 10, "completion_tokens": index + 1,
                                  "total_tokens": 11 + index}
            events.append(chunk)
        events.append({"id": "chunk_1", "object": "chat.completion.chunk", "created": 0,
                       "model": "fixture", "choices": [], "usage": {
                           "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    model, _ = transport_model(handler, model="system.ai.example" if service else "legacy-chat", api="chat")

    async def collect():
        return [chunk async for chunk in model.astream("test")]

    chunks = asyncio.run(collect()) if asynchronous else list(model.stream("test"))
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    assert merged.content == "Hello"
    assert merged.usage_metadata == {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                                     "input_token_details": {}, "output_token_details": {}}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_responses_stream_keeps_opaque_reasoning_and_final_usage(transport_model, asynchronous):
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [],
                 "encrypted_content": "opaque-state"}
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "Hello", "annotations": []}]}

    def handler(request):
        events = [
            {"type": "response.created", "response": _response([])},
            {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0,
             "item_id": "msg_1", "delta": "Hel", "logprobs": []},
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0,
             "item_id": "msg_1", "delta": "lo", "logprobs": []},
            {"type": "response.output_text.done", "output_index": 1, "content_index": 0,
             "item_id": "msg_1", "text": "Hello", "logprobs": []},
            {"type": "response.completed", "response": _response([reasoning, message])},
        ]
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                       for event in events)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    model, _ = transport_model(handler, model="system.ai.example", use_responses_api=True)

    async def collect():
        return [chunk async for chunk in model.astream("test")]

    chunks = asyncio.run(collect()) if asynchronous else list(model.stream("test"))
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    assert merged.text == "Hello"
    opaque = next(block for block in merged.content if block.get("type") == "reasoning")
    assert opaque["encrypted_content"] == "opaque-state"
    assert merged.usage_metadata["total_tokens"] == 13
    assert merged.usage_metadata["input_token_details"]["cache_read"] == 2
