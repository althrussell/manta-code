"""Independent end-to-end policy and HTTP contracts; no workspace calls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as Obj

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler

from manta_code.config import MantaConfig
from manta_code.databricks_chat import MantaChatDatabricks
from manta_code.providers.targets import POLICY_ENV, export_target_policy

TAG = "Databricks-Ai-Gateway-Request-Tags"


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path))
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


def chat():
    return {"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "reported-backend", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}


def delta(text):
    payload = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
               "model": "reported-backend", "choices": [{"index": 0, "finish_reason": None,
                   "delta": {"role": "assistant", "content": text}}]}
    return f"data: {json.dumps(payload)}\n\n".encode()


class Events(BaseCallbackHandler):
    def __init__(self):
        self.tokens = []
        self.errors = []
        self.completed = []

    def on_llm_new_token(self, token, **kwargs):
        if token:
            self.tokens.append(token)

    def on_llm_error(self, error, **kwargs):
        self.errors.append(error)

    def on_llm_end(self, result, **kwargs):
        self.completed.append(result)


@pytest.fixture
def build_model():
    clients = []

    def build(handler, **kwargs):
        authorizations = []

        def authenticate():
            authorizations.append(f"Bearer refresh-{len(authorizations) + 1}")
            return {"Authorization": authorizations[-1]}

        workspace = Obj(config=Obj(host="https://workspace.invalid", authenticate=authenticate))
        transport = httpx.MockTransport(handler)
        sync = httpx.Client(transport=transport)
        asynchronous = httpx.AsyncClient(transport=transport)
        clients.extend([sync, asynchronous])
        options = {"model": "system.ai.integration", "api": "chat", "max_retries": 0, **kwargs}
        model = MantaChatDatabricks(workspace_client=workspace, http_client=sync,
                                    http_async_client=asynchronous, **options)
        return model, authorizations

    yield build
    for client in clients:
        if isinstance(client, httpx.AsyncClient):
            asyncio.run(client.aclose())
        else:
            client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_gateway_retry_refreshes_sdk_headers(build_model, asynchronous):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"retry-after-ms": "1"},
                                  json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(200, json=chat())

    model, authorizations = build_model(handler, max_retries=1)
    result = asyncio.run(model.ainvoke("test")) if asynchronous else model.invoke("test")
    assert result.content == "OK"
    assert len(requests) == len(authorizations) == 2
    assert [request.headers["authorization"] for request in requests] == authorizations
    assert len({request.url.path for request in requests}) == 1


@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_inference_error_does_not_switch_protocol_or_retry(build_model, asynchronous, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {
            "message": "Use Responses instead; authorization=redacted-fixture", "code": "denied"}})

    model, _ = build_model(handler, max_retries=2)
    with pytest.raises(Exception):
        if asynchronous:
            asyncio.run(model.ainvoke("test"))
        else:
            model.invoke("test")
    assert len(requests) == 1
    assert requests[0].url.path == "/ai-gateway/mlflow/v1/chat/completions"
    assert model.transport_api == "chat"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_partial_stream_error_is_not_replayed_and_callbacks_match(build_model, asynchronous):
    requests = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield delta("visible")
            raise httpx.ReadError("stream disconnected")

    class AsyncBrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield delta("visible")
            raise httpx.ReadError("stream disconnected")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=AsyncBrokenStream() if asynchronous else BrokenStream())

    model, _ = build_model(handler, max_retries=2)
    events = Events()
    visible = []

    async def collect():
        async for chunk in model.astream("test", config={"callbacks": [events]}):
            if chunk.content:
                visible.append(chunk.content)

    with pytest.raises(Exception) as raised:
        if asynchronous:
            asyncio.run(collect())
        else:
            for chunk in model.stream("test", config={"callbacks": [events]}):
                if chunk.content:
                    visible.append(chunk.content)
    failure = raised.value
    causes = []
    while failure is not None:
        causes.append(failure)
        failure = failure.__cause__
    assert any(isinstance(error, httpx.ReadError) for error in causes)
    assert len(requests) == 1
    assert visible == events.tokens == ["visible"]
    assert len(events.errors) == 1
    assert events.completed == []


def test_async_stream_cancellation_closes_http_and_preserves_callback_error(build_model):
    closed = []
    reached_wait = asyncio.Event()

    class CancellableStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield delta("once")
            reached_wait.set()
            await asyncio.Future()

        async def aclose(self):
            closed.append(True)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=CancellableStream())

    model, _ = build_model(handler)
    events = Events()

    async def run():
        async def consume():
            async for _ in model.astream("test", config={"callbacks": [events]}):
                pass
        task = asyncio.create_task(consume())
        await asyncio.wait_for(reached_wait.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]

    asyncio.run(run())
    assert events.tokens == ["once"]
    assert len(events.errors) == 1 and isinstance(events.errors[0], asyncio.CancelledError)
    assert events.completed == []


def test_per_call_headers_preserve_constructor_tags(build_model):
    observed = []

    def handler(request):
        observed.append(json.loads(request.headers[TAG]))
        return httpx.Response(200, json=chat())

    model, _ = build_model(handler, request_tags={"app": "default", "configured": "kept"},
                           default_headers={TAG: '{"app":"constructor","constructor":"kept"}'})
    model.invoke("test", extra_headers={"x-per-call": "one"})
    model.invoke("test", extra_headers={TAG: '{"app":"call","call":"kept"}'})
    assert observed == [
        {"app": "constructor", "configured": "kept", "constructor": "kept"},
        {"app": "call", "configured": "kept", "constructor": "kept", "call": "kept"},
    ]


def test_explicit_protocol_alias_overrides_lower_target_settings(monkeypatch):
    cfg = MantaConfig.model_validate({"databricks": {"targets": {
        "system.ai.integration": {"api": "responses"},
    }}})
    monkeypatch.setenv(POLICY_ENV, export_target_policy(cfg))
    model = MantaChatDatabricks(model="system.ai.integration", use_responses_api=False)
    assert model.transport_api == "chat"


def test_explicit_surface_alias_overrides_lower_provider_settings(monkeypatch):
    monkeypatch.setenv(POLICY_ENV, export_target_policy(MantaConfig(), {"use_ai_gateway": False}))
    model = MantaChatDatabricks(model="system.ai.integration", surface="unified")
    assert model.transport_surface == "unified"


def test_explicit_output_limit_overrides_lower_alias(monkeypatch):
    cfg = MantaConfig.model_validate({"databricks": {"targets": {
        "system.ai.integration": {"max_output_tokens": 32},
    }}})
    monkeypatch.setenv(POLICY_ENV, export_target_policy(cfg))
    model = MantaChatDatabricks(model="system.ai.integration", max_tokens=64)
    assert model.max_tokens == 64


@pytest.mark.parametrize(("manta_profile", "explicit", "expected"), [
    ("manta", "explicit", "explicit"), ("manta", None, "manta"), (None, None, "sdk"),
])
def test_profile_precedence_reaches_lazy_sdk_auth(build_model, monkeypatch, manta_profile, explicit, expected):
    import databricks.sdk

    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "sdk")
    if manta_profile:
        monkeypatch.setenv("MANTA_PROFILE", manta_profile)
    else:
        monkeypatch.delenv("MANTA_PROFILE", raising=False)
    model, auth_calls = build_model(lambda request: httpx.Response(200, json=chat()), databricks_profile=explicit)
    workspace = model.workspace_client
    model.workspace_client = None
    constructions = []

    def workspace_client(**kwargs):
        constructions.append(kwargs)
        return workspace

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", workspace_client)
    assert constructions == []
    model.invoke("one")
    model.invoke("two")
    assert constructions == [{"profile": expected}]
    assert len(auth_calls) == 2
