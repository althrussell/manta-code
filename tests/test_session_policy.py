from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from manta_code.databricks_chat import MantaChatDatabricks
from manta_code.middleware.session_policy import SessionPolicyMiddleware


@dataclass
class Request:
    model: object
    state: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    runtime: object = None
    model_settings: dict = field(default_factory=dict)

    def override(self, **kwargs):
        return replace(self, **kwargs)


def test_resume_keeps_checkpoint_protocol_after_default_changes(monkeypatch):
    monkeypatch.setenv("MANTA_DATABRICKS_POLICY", '{"settings":{"service_api":"responses"}}')
    model = MantaChatDatabricks(model="system.ai.test", api="responses")
    state = {"_manta_databricks_protocols": {
        "databricks:system.ai.test": {"api": "chat", "surface": "unified"},
    }, "_manta_databricks_last_target": "databricks:system.ai.test"}
    request, update = SessionPolicyMiddleware()._prepare(Request(model, state))
    assert request.model.transport_api == "chat"
    assert request.model.transport_surface == "unified"
    assert update["_manta_databricks_protocols"]["databricks:system.ai.test"]["api"] == "chat"
    assert "profile" in update["_manta_databricks_protocols"]["databricks:system.ai.test"]
    assert model.transport_api == "responses"  # shared constructor never mutated


@pytest.mark.parametrize("opaque", [
    [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}],
    [{"type": "function_call", "id": "fc_1", "call_id": "call_1"}],
    [{"type": "reasoning", "extras": {"signature": "opaque"}}],
    [{"type": "text", "text": "Signed text", "thoughtSignature": "opaque"}],
])
def test_switch_rejects_opaque_history_without_loss(opaque):
    state = {"_manta_databricks_last_target": "databricks:system.ai.previous"}
    messages = [AIMessage(content=opaque)]
    request = Request(MantaChatDatabricks(model="system.ai.next", api="chat"), state, messages)
    with pytest.raises(ValueError, match="opaque reasoning or tool state"):
        SessionPolicyMiddleware()._prepare(request)
    assert messages[0].content == opaque


@pytest.mark.parametrize("state_key", [
    "manta_chat_tool_state", "manta_chat_content_state", "manta_responses_tool_state",
])
def test_switch_rejects_checkpointed_gateway_state(state_key):
    state = {"_manta_databricks_last_target": "databricks:system.ai.previous"}
    message = AIMessage(content="", additional_kwargs={state_key: {"call-1": "opaque"}})
    request = Request(MantaChatDatabricks(model="system.ai.next", api="chat"), state, [message])
    with pytest.raises(ValueError, match="opaque reasoning or tool state"):
        SessionPolicyMiddleware()._prepare(request)


def test_deliberate_plain_history_protocol_switch_and_async_parity():
    state = {"_manta_databricks_protocols": {
        "databricks:system.ai.test": {"api": "chat", "surface": "unified"},
    }, "_manta_databricks_last_target": "databricks:system.ai.test"}
    req = Request(
        MantaChatDatabricks(model="system.ai.test", api="responses"), state,
        [HumanMessage(content="Continue"), AIMessage(content="Plain text")],
        SimpleNamespace(context={"model_params": {"api": "responses"}}),
    )
    mw = SessionPolicyMiddleware()
    response = SimpleNamespace(result=[])
    sync = mw.wrap_model_call(req, lambda r: response)

    async def handler(request):
        assert request.model.transport_api == "responses"
        return response

    asynchronous = asyncio.run(mw.awrap_model_call(req, handler))
    assert sync.command.update == asynchronous.command.update
    assert sync.command.update["_manta_databricks_protocols"]["databricks:system.ai.test"]["api"] == "responses"


def test_runtime_auth_profile_does_not_reuse_previous_workspace_client(monkeypatch):
    from manta_code import auth

    original = SimpleNamespace(config=SimpleNamespace(host="https://old.invalid"))
    replacement = SimpleNamespace(config=SimpleNamespace(host="https://new.invalid"))
    selected = []

    def resolve(profile=None):
        selected.append(profile)
        return replacement

    monkeypatch.setattr(auth, "resolve_workspace_client", resolve)
    model = MantaChatDatabricks(
        model="system.ai.test", api="chat", databricks_profile="old", workspace_client=original,
    )
    request, _ = SessionPolicyMiddleware()._prepare(Request(
        model, model_settings={"databricks_profile": "new"},
    ))
    assert request.model.target_policy.profile == "new"
    assert request.model.workspace_client is None
    assert request.model._workspace() is replacement
    assert selected == ["new"]
    assert request.model_settings == {}
    assert model.workspace_client is original


def test_opaque_history_cannot_cross_profile_for_same_service(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://same.invalid")
    original = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="first")
    _, state = SessionPolicyMiddleware()._prepare(Request(original))
    next_model = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="second")
    message = AIMessage(content=[{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}])
    with pytest.raises(ValueError, match="profile/workspace"):
        SessionPolicyMiddleware()._prepare(Request(next_model, state, [message]))
    assert next_model.workspace_client is None  # boundary is enforced before authentication


def test_opaque_history_cannot_cross_workspace_in_same_profile():
    def model(host):
        return MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="team", workspace_client=SimpleNamespace(config=SimpleNamespace(host=host)))

    _, state = SessionPolicyMiddleware()._prepare(Request(model("https://first.invalid/")))
    checkpoint = state["_manta_databricks_protocols"]["databricks:system.ai.test"]
    assert checkpoint["profile"] == "team"
    assert "first.invalid" not in str(state)
    message = AIMessage(content="", additional_kwargs={"manta_chat_tool_state": {"call_1": "opaque"}})
    with pytest.raises(ValueError, match="profile/workspace"):
        SessionPolicyMiddleware()._prepare(Request(model("https://second.invalid"), state, [message]))
    # Canonical origin spelling does not invalidate an ordinary resume.
    SessionPolicyMiddleware()._prepare(Request(model("https://FIRST.invalid"), state, [message]))


def test_runtime_profile_switch_blocks_opaque_history_before_lazy_auth():
    original = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="first")
    req = Request(original, messages=[AIMessage(content=[{"type": "reasoning", "encrypted_content": "opaque"}])], model_settings={"databricks_profile": "second"})
    with pytest.raises(ValueError, match="profile/workspace"):
        SessionPolicyMiddleware()._prepare(req)
    assert original.workspace_client is None


def test_new_profile_does_not_restore_old_profiles_dialect():
    original = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="first")
    _, state = SessionPolicyMiddleware()._prepare(Request(original))
    new_model = MantaChatDatabricks(model="system.ai.test", api="responses", databricks_profile="second")
    request, update = SessionPolicyMiddleware()._prepare(Request(new_model, state, [HumanMessage(content="Continue")]))
    assert request.model.transport_api == "responses"
    assert update["_manta_databricks_protocols"]["databricks:system.ai.test"]["profile"] == "second"


def test_plain_tool_argument_named_signature_does_not_block_switch():
    message = AIMessage(content="", tool_calls=[{"id": "call_1", "name": "verify", "args": {"signature": "user supplied plain data"}}])
    request = Request(MantaChatDatabricks(model="system.ai.next", api="chat"), {"_manta_databricks_last_target": "databricks:system.ai.previous"}, [message])
    SessionPolicyMiddleware()._prepare(request)


def test_checkpoint_workspace_is_refreshed_after_lazy_client_resolution():
    model = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="team")

    def handler(request):
        request.model.workspace_client = SimpleNamespace(config=SimpleNamespace(host="https://resolved.invalid"))
        return SimpleNamespace(result=[])

    response = SessionPolicyMiddleware().wrap_model_call(Request(model), handler)
    import hashlib
    saved = response.command.update["_manta_databricks_protocols"]["databricks:system.ai.test"]
    assert saved["workspace"] == hashlib.sha256(b"https://resolved.invalid").hexdigest()


def test_message_provenance_restores_dialect_when_private_fork_input_is_omitted():
    original = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="team")
    reply = AIMessage(content=[{"type": "reasoning", "extras": {"signature": "opaque"}}])
    middleware = SessionPolicyMiddleware()
    middleware.wrap_model_call(Request(original), lambda req: SimpleNamespace(result=[reply]))
    changed_default = MantaChatDatabricks(model="system.ai.test", api="responses", databricks_profile="team")
    request, update = middleware._prepare(Request(changed_default, messages=[reply]))
    assert request.model.transport_api == "chat"
    assert update["_manta_databricks_last_target"] == "databricks:system.ai.test"
    other_profile = MantaChatDatabricks(model="system.ai.test", api="chat", databricks_profile="other")
    with pytest.raises(ValueError, match="profile/workspace"):
        middleware._prepare(Request(other_profile, messages=[reply]))


def test_real_graph_checkpoints_and_restores_protocol(monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "system.ai.test", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "OK",
            }}], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        })

    workspace = SimpleNamespace(config=SimpleNamespace(
        host="https://workspace.invalid", authenticate=lambda: {"Authorization": "Bearer test"},
    ))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = MantaChatDatabricks(
            model="system.ai.test", api="chat", workspace_client=workspace,
            http_client=client, max_retries=0,
        )
        saver = InMemorySaver()
        config = {"configurable": {"thread_id": "resume-contract"}}
        first = create_agent(model, middleware=[SessionPolicyMiddleware()], checkpointer=saver)
        result = first.invoke({"messages": [HumanMessage(content="one")]}, config)
        assert "_manta_databricks_protocols" not in result
        assert first.get_state(config).values["_manta_databricks_protocols"]["databricks:system.ai.test"]["api"] == "chat"
        changed = model.clone_with_policy(api="responses", surface="unified")
        second = create_agent(changed, middleware=[SessionPolicyMiddleware()], checkpointer=saver)
        result = second.invoke({"messages": [HumanMessage(content="two")]}, config)
        assert result["messages"][-1].content == "OK"
    assert paths == ["/ai-gateway/mlflow/v1/chat/completions"] * 2


def test_upstream_runtime_protocol_overrides_rebuild_before_inference():
    from deepagents_code._cli_context import CLIContextSchema
    from deepagents_code.configurable_model import ConfigurableModelMiddleware

    requests = []

    def handler(request):
        import json

        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(200, json={
            "id": "resp_fixture", "object": "response", "created_at": 0,
            "status": "completed", "model": "system.ai.test", "output": [{
                "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }], "error": None, "incomplete_details": None,
            "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
        })

    workspace = SimpleNamespace(config=SimpleNamespace(
        host="https://workspace.invalid", authenticate=lambda: {"Authorization": "Bearer test"},
    ))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = MantaChatDatabricks(model="system.ai.test", api="chat", workspace_client=workspace, http_client=client)
        graph = create_agent(
            model, middleware=[ConfigurableModelMiddleware(persist_model_state=False, openai_prompt_cache_key=False), SessionPolicyMiddleware()],
            context_schema=CLIContextSchema,
        )
        result = graph.invoke({"messages": [HumanMessage(content="test")]}, context={
            "model_params": {"api": "responses", "max_output_tokens": 32},
        })
    assert result["messages"][-1].text == "OK"
    assert requests[0][0] == "/ai-gateway/mlflow/v1/responses"
    assert requests[0][1]["max_output_tokens"] == 32
    assert "api" not in requests[0][1]


@pytest.mark.parametrize("mode", ["isolated", "fork"])
def test_private_protocol_state_isolated_from_delegated_model_and_retained_on_resume(mode):
    """Exercise actual task state transfer, with opaque parent Responses items."""
    import json

    from deepagents import create_deep_agent

    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["model"], request.url.path))
        if body["model"] == "system.ai.child":
            return httpx.Response(200, json={
                "id": "chat-child", "object": "chat.completion", "created": 0,
                "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "CHILD_OK"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            })
        returned = any(item.get("type") == "function_call_output" for item in body["input"])
        if mode == "fork" and len(calls) == 1:
            output = [
                {"type": "reasoning", "id": "rs-earlier", "encrypted_content": "opaque-earlier", "summary": []},
                {"type": "message", "id": "msg-earlier", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "PRIMED", "annotations": []}]},
            ]
        elif returned:
            output = [{"type": "message", "id": "msg-final", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "PARENT_OK", "annotations": []}]}]
        else:
            output = [
                {"type": "reasoning", "id": "rs-parent", "encrypted_content": "opaque-parent", "summary": []},
                {"type": "function_call", "id": "fc-parent", "call_id": "call-parent", "name": "task", "arguments": json.dumps({"description": "Reply CHILD_OK", "subagent_type": "child"}), "status": "completed"},
            ]
        return httpx.Response(200, json={
            "id": f"resp-parent-{len(calls)}", "object": "response", "created_at": 0, "status": "completed", "model": body["model"], "output": output,
            "error": None, "incomplete_details": None, "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
        })

    workspace = SimpleNamespace(config=SimpleNamespace(host="https://workspace.invalid", authenticate=lambda: {"Authorization": "Bearer test"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parent = MantaChatDatabricks(model="system.ai.parent", api="responses", workspace_client=workspace, http_client=client)
        child = MantaChatDatabricks(model="system.ai.child", api="chat", workspace_client=workspace, http_client=client)
        graph = create_deep_agent(parent, subagents=[{"name": "child", "description": "Answers exact phrases", "system_prompt": "Answer", "model": child, "mode": mode, "middleware": [SessionPolicyMiddleware()]}], middleware=[SessionPolicyMiddleware()], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "private-policy-delegation"}, "recursion_limit": 12}
        if mode == "fork":
            # A fork receives the parent's opaque history and protocol state;
            # it must retain the cross-model guard rather than act isolated.
            primed = graph.invoke({"messages": [HumanMessage(content="Prime")]}, config)
            assert primed["messages"][-1].text == "PRIMED"
            with pytest.raises(ValueError, match="opaque reasoning or tool state"):
                graph.invoke({"messages": [HumanMessage(content="Delegate")]}, config)
            assert [model for model, _ in calls] == ["system.ai.parent", "system.ai.parent"]
            return
        result = graph.invoke({"messages": [HumanMessage(content="Delegate")]}, config)
        assert [model for model, _ in calls] == ["system.ai.parent", "system.ai.child", "system.ai.parent"], str(calls)
        assert result["messages"][-1].text == "PARENT_OK"
        assert any(getattr(msg, "type", None) == "tool" and "CHILD_OK" in msg.text for msg in result["messages"])
        state = graph.get_state(config).values
        assert state["_manta_databricks_last_target"] == "databricks:system.ai.parent"
        assert set(state["_manta_databricks_protocols"]) == {"databricks:system.ai.parent"}
        assert "_manta_databricks_protocols" not in result
        resumed = graph.invoke({"messages": [HumanMessage(content="Continue")]}, config)
        assert resumed["messages"][-1].text == "PARENT_OK"
    assert [model for model, _ in calls] == ["system.ai.parent", "system.ai.child", "system.ai.parent", "system.ai.parent"]
