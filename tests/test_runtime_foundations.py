"""Harmless fake tools verify the effective runtime boundary, including JS."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from manta_code import hook
from manta_code.agents.registry import AgentDef, FsRule
from manta_code.middleware.policy import ToolPolicyMiddleware
from manta_code.runtime import RuntimeDispatchMiddleware


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def runtime(tools=()):
    return ToolRuntime(state={"messages": []}, context=None, config={}, stream_writer=lambda _: None,
                       tool_call_id="fixture", store=None, tools=list(tools))


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID", raising=False)
    return tmp_path


def test_failed_required_builder_is_atomic(monkeypatch, isolated):
    source = {"system_prompt": "unchanged", "tools": [object()], "middleware": []}
    original = dict(source)
    monkeypatch.setattr(hook, "build_manta_subagents", lambda: (_ for _ in ()).throw(ValueError("invalid agent policy")))
    with pytest.raises(ValueError, match="invalid agent policy"):
        hook.enrich_kwargs(source)
    assert source == original


def test_malformed_registry_stops_runtime_build(isolated):
    directory = isolated / "agents" / "broken"
    directory.mkdir(parents=True)
    (directory / "agent.toml").write_text('name = "broken"\nread_only = "not-a-boolean"\n')
    with pytest.raises(ValueError):
        hook.build_manta_subagents()


def test_effects_and_canonical_search_scopes(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    policy = ToolPolicyMiddleware(read_only=True)
    assert policy._denial_reason("delete", {"file_path": str(root / "unused")})
    assert policy._denial_reason("unclassified_remote_fixture", {})
    assert policy._denial_reason("js_eval", {})  # no unguarded interpreter exemption
    declared = SimpleNamespace(metadata={"manta_effect": "read"})
    assert policy._denial_reason("custom_fixture", {}, declared) is None
    untrusted_hint = SimpleNamespace(metadata={"annotations": {"readOnlyHint": True}})
    assert policy._denial_reason("custom_fixture", {}, untrusted_hint)
    remote = SimpleNamespace(metadata={"_deepagents_code_mcp_server": "fixture", "manta_effect": "read", "readOnlyHint": True})
    assert policy._denial_reason("read_file", {}, remote)
    scoped = ToolPolicyMiddleware(root=root, filesystem=[FsRule(operations=["read", "write"], paths=[str(root / "private") + "/**"], mode="deny")])
    assert scoped._denial_reason("grep", {"pattern": "fixture"})
    assert scoped._denial_reason("glob", {"pattern": "**/*"})
    assert scoped._denial_reason("execute", {"command": "fixture"})
    assert scoped._denial_reason("read_file", {"file_path": str(root / "private" / ".." / "private" / "note")})
    # Resolve a harmless link to an empty fixture directory; no tool runs.
    (root / "private").mkdir()
    (root / "alias").symlink_to(root / "private", target_is_directory=True)
    assert scoped._denial_reason("read_file", {"file_path": str(root / "alias" / "note")})


def test_explicit_databricks_groups_and_memory_binding(isolated):
    @tool
    def uc_list_catalogs() -> str:
        """Read fixture catalogs."""
        return "fixture"
    @tool
    def run_job() -> str:
        """Simulate a fixture job; no external call."""
        return "fixture"
    @tool
    def custom_fixture() -> str:
        """Read a fixture."""
        return "fixture"
    from manta_code.agents.factory import select_agent_tools
    selected = select_agent_tools(AgentDef(name="reader", databricks_tools=["uc_catalog"], memory=False),
                                  [uc_list_catalogs, run_job, custom_fixture])
    assert {t.name for t in selected} == {"uc_list_catalogs", "custom_fixture"}
    with pytest.raises(ValueError, match="unknown databricks_tools"):
        select_agent_tools(AgentDef(name="bad", databricks_tools=["missing"]), [])


def test_programmatic_proxy_reenters_actual_policy_and_hook_chain(isolated):
    calls = []
    @tool
    def custom_fixture() -> str:
        """Increment a harmless fixture counter."""
        calls.append("ran")
        return "fixture"
    policy = ToolPolicyMiddleware(read_only=True)
    dispatch = RuntimeDispatchMiddleware(agent="review")
    dispatch.configure([dispatch, policy])
    graph = create_agent(ScriptedModel(responses=[AIMessage(content="done")]), [custom_fixture], middleware=[dispatch, policy])
    dispatch.bind(graph)
    proxy = dispatch._proxy(custom_fixture)
    outer = runtime([custom_fixture])
    result = asyncio.run(proxy.ainvoke({}, {"__manta_programmatic_runtime": outer}))
    assert result.status == "error"
    assert "read-only" in result.content
    assert calls == []


def test_programmatic_ask_fails_closed_unattended(monkeypatch, isolated):
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    calls = []
    @tool
    def custom_fixture() -> str:
        """Increment a harmless fixture counter."""
        calls.append("ran")
        return "fixture"
    policy = ToolPolicyMiddleware(ask=[custom_fixture.name])
    dispatch = RuntimeDispatchMiddleware(agent="worker")
    dispatch.configure([dispatch, policy], {custom_fixture.name: True})
    graph = create_agent(ScriptedModel(responses=[AIMessage(content="done")]), [custom_fixture], middleware=[dispatch, policy])
    dispatch.bind(graph)
    result = asyncio.run(dispatch._proxy(custom_fixture).ainvoke({}, {"__manta_programmatic_runtime": runtime([custom_fixture])}))
    assert "unattended" in result.content
    assert calls == []


def test_real_js_bridge_uses_policy_and_preserves_safe_ptc(isolated):
    from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
    calls = []
    @tool
    def custom_fixture() -> str:
        """Increment a harmless fixture counter."""
        calls.append("ran")
        return "fixture"
    interpreter = CostAwareCodeInterpreterMiddleware(ptc=[custom_fixture], subagents=False, tool_name="js_eval")
    policy = ToolPolicyMiddleware(read_only=True)
    dispatch = RuntimeDispatchMiddleware(agent="review")
    stack = [dispatch, policy, interpreter]
    dispatch.configure(stack)
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "js_eval", "args": {"code": "await tools.customFixture({})"}, "id": "fixture-js"}]),
        AIMessage(content="done"),
    ])
    graph = create_agent(model, [custom_fixture], middleware=stack)
    dispatch.bind(graph)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Check harmless fixture")]}))
    assert calls == []
    assert any("read-only" in str(m.content) for m in result["messages"] if isinstance(m, ToolMessage))


def test_real_dcode_composition_manifest_global_fs_and_no_memory(monkeypatch, isolated):
    import deepagents_code.agent as upstream
    from manta_code.runtime import last_effective_manifest
    original = upstream.create_deep_agent
    original_cli = upstream.create_cli_agent
    monkeypatch.setattr(hook, "_installed", False)
    monkeypatch.setattr(hook, "_runtime_definitions", lambda: [AgentDef(name="helper", read_only=True)])
    monkeypatch.setattr(hook, "build_databricks_tools", lambda: [])
    try:
        assert hook.install_build_hook()
        graph, _ = upstream.create_cli_agent(
            ScriptedModel(responses=[AIMessage(content="done")]), "agent",
            enable_shell=False, enable_interpreter=False, enable_memory=False,
            enable_skills=False, interactive=False, auto_approve=True,
            fs_tools=["read_file", "glob", "grep"], enforce_model_policy=False,
        )
        manifest = last_effective_manifest()
        assert manifest["validated"]
        helper = next(a for a in manifest["agents"] if a["name"] == "helper")
        assert {t["name"] for t in helper["tools"]} == {"read_file", "glob", "grep", "manta_request_input"}
        assert helper["databricks_tools"] == []
        assert "ServerHooksMiddleware" in helper["middleware"]
        assert "CodeModelRetryMiddleware" in helper["middleware"]
        assert "manta_remember" not in {t["name"] for t in manifest["primary"]["tools"]}
    finally:
        upstream.create_deep_agent = original
        upstream.create_cli_agent = original_cli


def test_owned_server_bootstrap_keeps_offload_and_auth_services(tmp_path):
    from deepagents_code.client.launch import server
    from manta_code.server_graph import install_server_bootstrap
    install_server_bootstrap()
    path = server.generate_langgraph_json(tmp_path, checkpointer_path="./fixture.py:checkpointer")
    generated = json.loads(path.read_text())
    assert generated["graphs"]["agent"] == "manta_code.server_graph:make_graph"
    assert generated["http"]["app"] == "deepagents_code.offload_api:app"
    assert generated["http"]["enable_custom_route_auth"]
    assert generated["checkpointer"]["path"] == "./fixture.py:checkpointer"


def test_server_bootstrap_installs_controls_before_non_databricks_graph(monkeypatch):
    from manta_code import server_graph
    import deepagents_code.server_graph as upstream
    events = []
    monkeypatch.setattr(server_graph, "install_server_controls", lambda: events.append("Manta controls"))
    async def fake_graph(**kwargs):
        events.append("non-Databricks fake graph")
        return "graph"
    monkeypatch.setattr(upstream, "make_graph", fake_graph)
    assert asyncio.run(server_graph.make_graph()) == "graph"
    assert events == ["Manta controls", "non-Databricks fake graph"]


def test_js_approval_resume_does_not_repeat_completed_tool_effect(isolated):
    from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
    from langgraph.types import Command
    from langgraph.checkpoint.memory import InMemorySaver
    from manta_code.tasks.events import EventLogMiddleware
    from manta_code.tasks.store import recent_events
    calls = []
    @tool
    def first_fixture() -> str:
        """Record a harmless first fixture effect."""
        calls.append("first")
        return "first"
    @tool
    def approved_fixture() -> str:
        """Record a harmless approved fixture effect."""
        calls.append("approved")
        return "approved"
    interpreter = CostAwareCodeInterpreterMiddleware(ptc=["first_fixture", "approved_fixture"], subagents=False, tool_name="js_eval")
    dispatch = RuntimeDispatchMiddleware(agent="worker")
    policy = ToolPolicyMiddleware(ask=["approved_fixture"])
    stack = [EventLogMiddleware(agent="worker", approval={"approved_fixture"}), dispatch, policy, interpreter]
    dispatch.configure(stack, {"approved_fixture": True})
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "js_eval", "args": {"code": "await tools.firstFixture({}); await tools.approvedFixture({})"}, "id": "fixture-js"}]),
        AIMessage(content="done"),
    ])
    graph = create_agent(model, [first_fixture, approved_fixture], middleware=stack, checkpointer=InMemorySaver())
    dispatch.bind(graph)
    config = {"configurable": {"thread_id": "fixture-resume"}}
    async def exercise():
        paused = await graph.ainvoke({"messages": [HumanMessage(content="Check harmless fixtures")]}, config)
        assert paused["__interrupt__"]
        assert paused["__interrupt__"][0].value["name"] == "approved_fixture"
        assert calls == ["first"]
        resumed = await graph.ainvoke(Command(resume={"type": "approve"}), config)
        assert "__interrupt__" not in resumed
    asyncio.run(exercise())
    assert calls == ["first", "approved"]
    assert any(e.kind == "approved" and e.detail == "approved_fixture" for e in recent_events())


def test_owned_server_factory_builds_non_databricks_fake_graph_with_controls(monkeypatch, isolated):
    from manta_code import server_graph
    import deepagents_code.server_graph as upstream
    import deepagents_code.agent as upstream_agent
    from manta_code.runtime import last_effective_manifest, tool_node
    original = upstream_agent.create_deep_agent
    original_cli = upstream_agent.create_cli_agent
    monkeypatch.setattr(hook, "_installed", False)
    monkeypatch.setattr(hook, "_runtime_definitions", lambda: [AgentDef(name="helper", memory=False)])
    monkeypatch.setattr(hook, "build_databricks_tools", lambda: [])
    async def fake_graph(**kwargs):
        graph, _ = upstream_agent.create_cli_agent(
            ScriptedModel(responses=[AIMessage(content="done")]), "agent",
            enable_shell=False, enable_memory=False, enable_skills=False,
            enable_interpreter=False, interactive=False, auto_approve=True,
            enforce_model_policy=False,
        )
        return graph
    monkeypatch.setattr(upstream, "make_graph", fake_graph)
    try:
        graph = asyncio.run(server_graph.make_graph())
        assert "Manta.Dispatch" in last_effective_manifest()["primary"]["middleware"]
        assert any(a["name"] == "helper" for a in last_effective_manifest()["agents"])
        assert tool_node(graph)
    finally:
        upstream_agent.create_deep_agent = original
        upstream_agent.create_cli_agent = original_cli


def test_real_js_task_dispatch_preserves_child_policy_and_cost_wrapper(monkeypatch, isolated):
    import deepagents_code.agent as upstream
    from manta_code.tasks.store import recent_events
    from langgraph.checkpoint.memory import InMemorySaver
    calls = []
    @tool
    def custom_fixture() -> str:
        """Record a harmless fixture effect."""
        calls.append("ran")
        return "fixture"
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "js_eval", "args": {"code": "await task({description: 'Check harmless fixture', subagentType: 'helper'})"}, "id": "parent-js"}]),
        AIMessage(content="parent finished"),
    ])
    child_model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "custom_fixture", "args": {}, "id": "child-fixture"}]),
        AIMessage(content="child finished"),
    ])
    original = upstream.create_deep_agent
    original_cli = upstream.create_cli_agent
    monkeypatch.setattr(hook, "_installed", False)
    monkeypatch.setattr(hook, "_runtime_definitions", lambda: [AgentDef(name="helper", memory=False, read_only=True)])
    monkeypatch.setattr(hook, "build_databricks_tools", lambda: [])
    original_build = hook.build_manta_subagents
    monkeypatch.setattr(hook, "build_manta_subagents", lambda: [{**spec, "model": child_model} for spec in original_build()])
    try:
        hook.install_build_hook()
        graph, _ = upstream.create_cli_agent(
            model, "agent", tools=[custom_fixture], enable_interpreter=True,
            enable_shell=False, enable_memory=False, enable_skills=False,
            interactive=False, auto_approve=True, enforce_model_policy=False,
            checkpointer=InMemorySaver(),
        )
        result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Check fixture")]}, {"configurable": {"thread_id": "js-task-fixture"}}))
        assert result["messages"][-1].content == "parent finished"
        assert calls == []
        assert any(e.agent == "helper" and e.kind == "denied" and e.detail == "custom_fixture" for e in recent_events())
    finally:
        upstream.create_deep_agent = original
        upstream.create_cli_agent = original_cli


def test_helper_model_role_binding_does_not_mutate_parent():
    from langchain.agents.middleware.summarization import SummarizationMiddleware
    from manta_code.runtime import middleware_for_agent
    model = ScriptedModel(responses=[AIMessage(content="summary")], metadata={"fixture": True})
    original = SummarizationMiddleware(model=model)
    baseline = dict(model.metadata)
    child = middleware_for_agent([original], "review")[0]
    assert child is not original
    assert child.model is not original.model
    assert child.model.metadata == {**baseline, "manta_agent": "review"}
    assert child._summary_model.bound.metadata["manta_agent"] == "review"
    assert original.model.metadata == baseline


@pytest.mark.parametrize("programmatic", [False, True])
def test_bypassed_gate_never_claims_human_approval(programmatic, isolated):
    from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
    from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
    from manta_code.tasks.events import EventLogMiddleware
    from manta_code.tasks.store import recent_events
    @tool
    def constant_fixture() -> str:
        """Return a harmless constant."""
        return "ok"
    events = EventLogMiddleware(agent="worker", approval={"constant_fixture"})
    gate = HumanInTheLoopMiddleware({"constant_fixture": {"allowed_decisions": ["approve", "reject"], "when": lambda request: False}})
    dispatch = RuntimeDispatchMiddleware(agent="worker")
    interpreter = CostAwareCodeInterpreterMiddleware(ptc=["constant_fixture"], subagents=False, tool_name="js_eval")
    stack = [events, dispatch, gate, *([interpreter] if programmatic else [])]
    dispatch.configure(stack)
    call = {"name": "js_eval", "args": {"code": "await tools.constantFixture({})"}} if programmatic else {"name": "constant_fixture", "args": {}}
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{**call, "id": "fixture"}]), AIMessage(content="done")])
    graph = create_agent(model, [constant_fixture], middleware=stack)
    dispatch.bind(graph)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Check constant")]}))
    assert "__interrupt__" not in result
    assert [e.kind for e in recent_events() if e.detail == "constant_fixture"] == ["tool"]


@pytest.mark.parametrize("mode,explicit_ask,expected_interrupt", [("yolo", False, False), ("yolo", True, True), ("auto", False, True)])
def test_js_live_mode_uses_async_store_and_preserves_explicit_ask(mode, explicit_ask, expected_interrupt, isolated):
    import deepagents_code.agent as upstream
    from deepagents_code.approval_mode import approval_mode_key
    from deepagents_code._js_cost import CostAwareCodeInterpreterMiddleware
    from langgraph.store.memory import InMemoryStore
    from langgraph.checkpoint.memory import InMemorySaver
    from manta_code.agents.factory import _runtime_interrupt_on
    calls = []
    @tool
    def constant_fixture() -> str:
        """Return a harmless constant."""
        calls.append("called")
        return "ok"
    class AsyncOnlyStore(InMemoryStore):
        def get(self, *args, **kwargs):
            raise AssertionError("Synchronous live-mode lookup is unavailable")
        async def aget(self, *args, **kwargs):
            return SimpleNamespace(value={"mode": mode})
    definition = AgentDef(name="fixture", tools_ask=["constant_fixture"] if explicit_ask else [])
    own_gates = _runtime_interrupt_on(definition)
    dispatch = RuntimeDispatchMiddleware(agent="fixture", interrupt_on=own_gates)
    gate = upstream.AsyncApprovalHITLMiddleware({"constant_fixture": {
        "allowed_decisions": ["approve", "reject"], "when": upstream._should_interrupt_tool_call,
    }})
    stack = [dispatch, ToolPolicyMiddleware(ask=definition.tools_ask), gate,
             CostAwareCodeInterpreterMiddleware(ptc=["constant_fixture"], subagents=False, tool_name="js_eval")]
    dispatch.configure(stack)
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "js_eval", "args": {"code": "await tools.constantFixture({})"}, "id": "fixture"}]),
        AIMessage(content="done"),
    ])
    graph = create_agent(model, [constant_fixture], middleware=stack, store=AsyncOnlyStore(), checkpointer=InMemorySaver())
    dispatch.bind(graph)
    context = {"thread_id": "async-store", "approval_mode_key": approval_mode_key("async-store"), "approval_mode": mode}
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Check constant")]},
                                     {"configurable": {"thread_id": "async-store"}}, context=context))
    assert bool(result.get("__interrupt__")) is expected_interrupt
    assert calls == ([] if expected_interrupt else ["called"])


def test_native_gate_merge_preserves_specialized_auto_middleware():
    from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
    from manta_code.agents.factory import install_approval_gates
    class SpecializedAutoGate(HumanInTheLoopMiddleware):
        classifier_fixture = True
    original = SpecializedAutoGate({"fixture": True})
    merged = install_approval_gates([original], {"run_job": True})[0]
    assert isinstance(merged, SpecializedAutoGate)
    assert merged.classifier_fixture
    assert "run_job" in merged.interrupt_on
    assert "run_job" not in original.interrupt_on


@pytest.mark.parametrize("surface", ["primary", "primary-js", "child-js"])
def test_native_job_mutation_requires_manual_approval_and_rejects_without_effect(surface, monkeypatch, isolated):
    import deepagents_code.agent as upstream
    from deepagents_code.configuration.interpreter import InterpreterConfig
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    calls = []
    @tool
    def run_job(job_id: int) -> str:
        """Record a harmless fake job request; no external job is called."""
        calls.append(job_id)
        return "fixture job"
    first = {"name": "run_job", "args": {"job_id": 1}}
    if surface == "primary-js":
        first = {"name": "js_eval", "args": {"code": "await tools.runJob({job_id: 1})"}}
    if surface == "child-js":
        first = {"name": "js_eval", "args": {"code": "await task({description: 'Check harmless job fixture', subagentType: 'helper'})"}}
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{**first, "id": "fixture"}]), AIMessage(content="done")])
    child_model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "run_job", "args": {"job_id": 1}, "id": "child-fixture"}]), AIMessage(content="child done")])
    original = upstream.create_deep_agent
    original_cli = upstream.create_cli_agent
    monkeypatch.setattr(hook, "_installed", False)
    monkeypatch.setattr(hook, "_runtime_definitions", lambda: [AgentDef(name="helper", memory=False, databricks_tools=["jobs"])])
    monkeypatch.setattr(hook, "build_databricks_tools", lambda: [run_job])
    original_build = hook.build_manta_subagents
    monkeypatch.setattr(hook, "build_manta_subagents", lambda: [{**spec, "model": child_model} for spec in original_build()])
    try:
        hook.install_build_hook()
        graph, _ = upstream.create_cli_agent(
            model, "agent", tools=[run_job], enable_interpreter=surface != "primary",
            interpreter_config=InterpreterConfig(timeout_seconds=30, memory_limit_mb=64,
                                                  max_ptc_calls=10, max_result_chars=2000,
                                                  ptc=["run_job"], ptc_acknowledge_unsafe=False),
            enable_ask_user=False,
            enable_shell=False, enable_memory=False, enable_skills=False,
            interactive=True, auto_approve=False, enforce_model_policy=False,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": f"native-{surface}"}}
        async def exercise():
            result = await graph.ainvoke({"messages": [HumanMessage(content="Check fake job")]}, config)
            assert result["__interrupt__"]
            assert calls == []
            value = result["__interrupt__"][0].value
            decision = {"type": "reject"} if value.get("type") == "tool_approval" else {"decisions": [{"type": "reject"}]}
            resumed = await graph.ainvoke(Command(resume=decision), config)
            assert "__interrupt__" not in resumed
        asyncio.run(exercise())
        assert calls == []
    finally:
        upstream.create_deep_agent = original
        upstream.create_cli_agent = original_cli


def test_native_job_explicit_ask_is_denied_unattended(monkeypatch, isolated):
    from manta_code.agents.factory import compose_subagent, compile_subagent
    from manta_code.tasks.events import EventLogMiddleware
    calls = []
    @tool
    def run_job(job_id: int) -> str:
        """Record a harmless fake job request."""
        calls.append(job_id)
        return "fixture job"
    monkeypatch.setenv("MANTA_UNATTENDED", "1")
    definition = AgentDef(name="dataops", databricks_tools=["jobs"], tools_ask=["run_job"], memory=False)
    spec = compose_subagent(compile_subagent(definition, extra_middleware=[EventLogMiddleware(agent="dataops")]), template=None, inherited_tools=[run_job])
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "run_job", "args": {"job_id": 1}, "id": "fixture"}]), AIMessage(content="done")])
    graph = create_agent(model, spec["tools"], middleware=spec["middleware"])
    for mw in spec["middleware"]:
        if isinstance(mw, RuntimeDispatchMiddleware):
            mw.bind(graph)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Check fake job")]}))
    assert "__interrupt__" not in result
    assert calls == []
    assert any("unattended" in str(message.content) for message in result["messages"] if isinstance(message, ToolMessage))
