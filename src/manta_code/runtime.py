"""Runtime composition, guarded programmatic dispatch, and effective manifests.

The QuickJS bridge normally invokes raw tools outside ToolNode. Its proxy here
re-enters the *actual compiled* tool middleware chain, preserving hook, policy,
MCP timeout and audit behavior. It uses a per-call HITL gate for programmatic
calls because there is no separate model message for batched approval to inspect.
"""
from __future__ import annotations

import dataclasses
import inspect
import uuid
from contextvars import ContextVar
from collections.abc import Callable
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode, ToolRuntime

from .middleware.policy import ToolPolicyMiddleware, tool_effect

_RUNTIME_KEY = "__manta_programmatic_runtime"
_LAST_MANIFEST: dict[str, Any] | None = None
_LAUNCH_POLICY: ContextVar[dict[str, Any] | None] = ContextVar("manta_launch_policy", default=None)


def current_launch_policy() -> dict[str, Any] | None:
    policy = _LAUNCH_POLICY.get()
    return dict(policy) if policy is not None else None


def launch_memory_enabled() -> bool:
    import os
    policy = _LAUNCH_POLICY.get()
    if policy is not None:
        return policy.get("enable_memory", True)
    return os.environ.get("DEEPAGENTS_CODE_SERVER_ENABLE_MEMORY", "true").strip().lower() not in {"0", "false", "no", "off"}


def model_for_agent(model: Any, agent: str) -> Any:
    """Shallow-copy model identity without copying clients or changing a peer."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables.base import RunnableBindingBase
    if isinstance(model, BaseChatModel):
        if (model.metadata or {}).get("manta_agent") == agent:
            return model
        return model.model_copy(update={"metadata": {**(model.metadata or {}), "manta_agent": agent}})
    if isinstance(model, RunnableBindingBase):
        return model.model_copy(update={"bound": model_for_agent(model.bound, agent)})
    return model


def middleware_for_agent(middleware: list[Any], agent: str) -> list[Any]:
    """Rebind known helper models on isolated middleware copies.

    Helper objects may be shared with the main agent. Copy their containers and
    model bindings, retaining transports, locks and other runtime resources.
    Models constructed later still carry LangChain's actual agent metadata.
    """
    import copy
    result = []
    for original in middleware:
        clone = copy.copy(original)
        for attribute in ("_approval", "_guarded_dispatch_names"):
            value = vars(original).get(attribute)
            if isinstance(value, (dict, set)):
                setattr(clone, attribute, value.copy())
        for attribute in ("model", "_model", "_summary_model", "_summary_chat_model", "_configured_classifier_model"):
            # Properties such as Deep Agents' summary `model` are backed by
            # _lc_helper and deliberately have no setter.
            value = vars(original).get(attribute)
            if value is not None:
                setattr(clone, attribute, model_for_agent(value, agent))
        helper = vars(original).get("_lc_helper")
        if helper is not None:
            clone._lc_helper = middleware_for_agent([helper], agent)[0]
        resolved = vars(original).get("_model_result")
        if dataclasses.is_dataclass(resolved) and hasattr(resolved, "model"):
            clone._model_result = dataclasses.replace(resolved, model=model_for_agent(resolved.model, agent))
        result.append(clone)
    return result


def install_launch_policy_capture(dc_agent: Any) -> None:
    import functools
    original = dc_agent.create_cli_agent
    if getattr(original, "__manta_launch_policy__", False):
        return
    signature = inspect.signature(original)

    @functools.wraps(original)
    def create(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        # Never retain tools, credentials, environment or prompt content.
        policy = {k: bound.arguments[k] for k in ("enable_memory", "interactive", "auto_approve", "auto_mode_enabled", "assistant_id", "enforce_model_policy", "cli_max_retries")}
        token = _LAUNCH_POLICY.set(policy)
        try:
            return original(*args, **kwargs)
        finally:
            _LAUNCH_POLICY.reset(token)

    create.__manta_launch_policy__ = True
    dc_agent.create_cli_agent = create



def _is_interpreter(middleware: Any) -> bool:
    return any(base.__name__ == "CodeInterpreterMiddleware" and base.__module__ == "langchain_quickjs.middleware"
               for base in type(middleware).__mro__)


class RuntimeBuildError(RuntimeError):
    """Required controls could not be assembled or validated."""


def tool_node(graph: Any) -> ToolNode:
    node = getattr(graph, "nodes", {}).get("tools")
    bound = getattr(node, "bound", None)
    if not isinstance(bound, ToolNode):
        raise RuntimeBuildError("Compiled graph has no inspectable ToolNode; required dispatch controls cannot be verified. Run manta doctor.")
    return bound


def child_graphs(node: ToolNode) -> dict[str, Any]:
    """Read SDK task closure's compiled graphs, without executing a tool."""
    task = node.tools_by_name.get("task")
    if task is None:
        return {}
    seen: set[int] = set()

    def visit(fn: Any, depth: int = 0) -> dict[str, Any]:
        if not inspect.isfunction(fn) or id(fn) in seen or depth > 6:
            return {}
        seen.add(id(fn))
        values = inspect.getclosurevars(fn).nonlocals
        found = values.get("subagent_graphs")
        if isinstance(found, dict):
            return found
        for value in values.values():
            result = visit(value, depth + 1)
            if result:
                return result
        return {}

    return visit(getattr(task, "coroutine", None)) or visit(getattr(task, "func", None))


class RuntimeDispatchMiddleware(AgentMiddleware):
    """Route JS tools and JS task() through the graph's normal tool controls."""

    def __init__(self, *, agent: str, middleware: list[Any] | None = None,
                 interrupt_on: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.agent = agent
        self.databricks_selection: list[str] | None = None
        self._node: ToolNode | None = None
        self._middleware = list(middleware or ())
        self._interrupt_on = dict(interrupt_on or {})
        self._interpreter_names: set[str] = set()
        self._policies: list[ToolPolicyMiddleware] = []
        self._approval: Any = None
        self._explicit_ptc_names: set[str] = set()

    @property
    def name(self) -> str:
        return "Manta.Dispatch"

    def configure(self, middleware: list[Any], interrupt_on: dict[str, Any] | None = None) -> None:
        from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
        self._middleware = middleware
        gates = dict(interrupt_on or {})
        self._policies = [mw for mw in middleware if isinstance(mw, ToolPolicyMiddleware)]
        for mw in middleware:
            if isinstance(mw, HumanInTheLoopMiddleware):
                gates.update(mw.interrupt_on)
            if _is_interpreter(mw):
                self._interpreter_names.update(t.name for t in getattr(mw, "tools", ()))
        for policy in self._policies:
            policy._guarded_dispatch_names.update(self._interpreter_names)
        # Predicates are evaluated against the live request; explicit Manta ASK
        # entries intentionally override permissive upstream automatic modes.
        gates.update(self._interrupt_on)
        self._approval = HumanInTheLoopMiddleware(gates, interrupt_mode="per_call")
        for mw in middleware:
            if type(mw).__name__ == "EventLogMiddleware":
                mw._approval.update(gates)
        for mw in middleware:
            if _is_interpreter(mw):
                ptc = getattr(mw, "_ptc", None)
                if isinstance(ptc, (list, tuple)):
                    # Resolve explicit objects through the authoritative graph
                    # toolset too. A hidden tool cannot escape the manifest.
                    self._explicit_ptc_names.update(t.name for t in ptc if isinstance(t, BaseTool))
                    mw._ptc = [t.name if isinstance(t, BaseTool) else t for t in ptc]

    def bind(self, graph: Any) -> None:
        self._node = tool_node(graph)
        if not self._explicit_ptc_names <= self._node.tools_by_name.keys():
            raise RuntimeBuildError(f"{self.agent}: explicit programmatic tools are absent from actual ToolNode")
        if self._interpreter_names and not self._interpreter_names <= self._node.tools_by_name.keys():
            raise RuntimeBuildError(f"{self.agent}: configured interpreter is absent from actual ToolNode")

    def _proxy(self, tool: BaseTool) -> BaseTool:
        if tool.name in self._interpreter_names:
            return tool

        def invoke(*args: Any, runtime: ToolRuntime = None, config: RunnableConfig = None, **kwargs: Any) -> Any:
            if runtime is not None:
                kwargs["runtime"] = runtime
            return self._dispatch(tool, args, kwargs, config or {}, asynchronous=False)

        async def ainvoke(*args: Any, runtime: ToolRuntime = None, config: RunnableConfig = None, **kwargs: Any) -> Any:
            if runtime is not None:
                kwargs["runtime"] = runtime
            return await self._dispatch(tool, args, kwargs, config or {}, asynchronous=True)

        if isinstance(tool, StructuredTool):
            schema = tool.args_schema
            if isinstance(schema, type) and not getattr(schema, "model_fields", None):
                from pydantic import create_model
                # BaseTool short-circuits every input, including injected
                # runtime, when its Pydantic schema has no fields. Keep one
                # hidden field so zero-argument PTC tools receive the runtime.
                schema = create_model(f"{tool.name}DispatchArgs", __base__=schema,
                                      runtime=(Annotated[Any, InjectedToolArg], None))
            proxy = tool.model_copy(update={"func": invoke, "coroutine": ainvoke, "args_schema": schema})
            # model_copy retains cached properties from the original callable;
            # the proxy adds an injected runtime parameter to every bridge.
            proxy.__dict__.pop("_injected_args_keys", None)
            proxy.__dict__.pop("_tool_call_schema_memo", None)
            return proxy
        return StructuredTool(name=tool.name, description=tool.description,
                              args_schema=tool.args_schema or tool.get_input_schema(),
                              func=invoke, coroutine=ainvoke, metadata=tool.metadata)

    def _prepare_model(self, request: Any) -> Any:
        if not self._interpreter_names:
            return request
        # Ordinary model tools retain their schemas; only the interpreter's raw
        # bindings call these proxies. ToolNode still owns the original tools.
        return request.override(tools=[self._proxy(t) if isinstance(t, BaseTool) else t for t in request.tools])

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        return handler(self._prepare_model(request))

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        return await handler(self._prepare_model(request))

    def _prepare_tool(self, request: Any) -> Any:
        if request.tool_call.get("name") not in self._interpreter_names:
            return request
        runtime = request.runtime
        tools = [self._proxy(t) for t in runtime.tools]
        return request.override(runtime=dataclasses.replace(runtime, tools=tools))

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return handler(self._prepare_tool(request))

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return await handler(self._prepare_tool(request))

    def _dispatch(self, tool: BaseTool, positional: tuple, args: dict, config: dict,
                  *, asynchronous: bool) -> Any:
        runtime = args.get("runtime") or config.get(_RUNTIME_KEY) or config.get("configurable", {}).get(_RUNTIME_KEY)
        if runtime is None or self._node is None:
            raise RuntimeBuildError("Programmatic tool dispatch has no validated graph runtime; execution refused.")
        if positional:
            raise RuntimeBuildError("Programmatic dispatch requires named, schema-validated arguments.")
        clean_config = {k: v for k, v in runtime.config.items() if k != _RUNTIME_KEY}
        clean_config["configurable"] = {k: v for k, v in clean_config.get("configurable", {}).items() if k != _RUNTIME_KEY}
        call_id = f"manta-js-{uuid.uuid4().hex}"
        runtime = dataclasses.replace(runtime, config=clean_config, tool_call_id=call_id)
        call = {"name": tool.name, "id": call_id, "args": {k: v for k, v in args.items() if k != "runtime"}, "type": "tool_call"}
        request = ToolCallRequest(tool_call=call, tool=tool, state=runtime.state, runtime=runtime)
        # A blocked call re-enters the chain to produce the same audit result;
        # it never reaches the approval prompt or the real tool.
        blocked = any(policy._blocked(request) is not None for policy in self._policies)
        if asynchronous:
            return self._durable_adispatch(request, blocked)
        if request.runtime.config.get("configurable", {}).get("__pregel_scratchpad") is not None:
            from langgraph.func import task
            @task(name=f"manta_programmatic_{self.agent}_{tool.name}")
            def durable() -> Any:
                return self._sdispatch(request, blocked)
            return durable().result()
        return self._sdispatch(request, blocked)

    async def _durable_adispatch(self, request: Any, blocked: bool) -> Any:
        if request.runtime.config.get("configurable", {}).get("__pregel_scratchpad") is None:
            return await self._adispatch(request, blocked)
        from langgraph.func import task
        @task(name=f"manta_programmatic_{self.agent}_{request.tool_call['name']}")
        async def durable() -> Any:
            return await self._adispatch(request, blocked)
        return await durable()

    def _gate_event(self, kind: str, request: Any) -> None:
        from .tasks.events import _current_task_id
        from .tasks.store import EventRecord, record_event
        record_event(EventRecord(agent=self.agent, kind=kind, detail=request.tool_call["name"], task_id=_current_task_id()))

    def _per_call_gate(self, request: Any) -> tuple[Any, list[bool]]:
        from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
        required = [False]
        name = request.tool_call["name"]
        config = self._approval.interrupt_on.get(name)
        if config is None:
            return self._approval, required
        predicate = config.get("when")
        def when(current: Any) -> bool:
            required[0] = True if predicate is None else bool(predicate(current))
            return required[0]
        return HumanInTheLoopMiddleware({name: {**config, "when": when}}, interrupt_mode="per_call"), required

    def _builtin_approval(self, request: Any) -> bool:
        config = self._approval.interrupt_on.get(request.tool_call["name"], {})
        predicate = config.get("when")
        return getattr(predicate, "__module__", "") == "deepagents_code.agent" and getattr(predicate, "__qualname__", "") in {
            "_should_interrupt_tool_call", "_interrupt_predicate.<locals>.should_interrupt",
        }

    @staticmethod
    def _route_programmatic_approval(request: Any, mode: Any) -> Any:
        from deepagents_code.agent import _ASYNC_APPROVAL_ROUTING_KEY, _RoutingDecision
        from deepagents_code.approval_mode import ApprovalMode
        # Auto classifies model-visible tool proposals. Internal JS calls have
        # no proposal/decision plan, so use the same human fallback as an
        # unavailable classifier rather than treating Auto as blanket consent.
        if mode is ApprovalMode.AUTO:
            mode = ApprovalMode.MANUAL
        return request.override(state={**request.state, _ASYNC_APPROVAL_ROUTING_KEY: _RoutingDecision(mode)})

    def _sync_approval_request(self, request: Any) -> Any:
        if not self._builtin_approval(request):
            return request
        from deepagents_code.agent import _resolve_approval_mode
        return self._route_programmatic_approval(request, _resolve_approval_mode(request.runtime.context, request.runtime.store))

    async def _async_approval_request(self, request: Any) -> Any:
        """Use dcode's async live-mode routing for its built-in predicates."""
        if not self._builtin_approval(request):
            return request
        from deepagents_code.agent import _aresolve_approval_mode
        mode = await _aresolve_approval_mode(request.runtime.context, request.runtime.store)
        return self._route_programmatic_approval(request, mode)

    def _sdispatch(self, request: Any, blocked: bool) -> Any:
        def execute(req: Any) -> Any:
            def terminal(r: Any) -> Any:
                return self._node._execute_tool_sync(r, "dict", r.runtime.config)
            chain = self._node._wrap_tool_call
            return chain(req, terminal) if chain is not None else terminal(req)
        if blocked or self._approval is None:
            return execute(request)
        request = self._sync_approval_request(request)
        approval, required = self._per_call_gate(request)
        executed = False
        def approved(req: Any) -> Any:
            nonlocal executed
            executed = True
            if required[0]:
                from .tasks.events import confirmed_tool_approval
                with confirmed_tool_approval(req):
                    return execute(req)
            return execute(req)
        try:
            result = approval.wrap_tool_call(request, approved)
        except BaseException as exc:
            if not executed:
                self._gate_event("interrupted" if type(exc).__name__ == "GraphInterrupt" else "cancelled" if type(exc).__name__ == "CancelledError" else "tool_error", request)
            raise
        if not executed:
            self._gate_event("denied", request)
        return result

    async def _adispatch(self, request: Any, blocked: bool) -> Any:
        async def execute(req: Any) -> Any:
            async def terminal(r: Any) -> Any:
                return await self._node._execute_tool_async(r, "dict", r.runtime.config)
            chain = self._node._awrap_tool_call
            return await chain(req, terminal) if chain is not None else await terminal(req)
        if blocked or self._approval is None:
            return await execute(request)
        request = await self._async_approval_request(request)
        approval, required = self._per_call_gate(request)
        executed = False
        async def approved(req: Any) -> Any:
            nonlocal executed
            executed = True
            if required[0]:
                from .tasks.events import confirmed_tool_approval
                with confirmed_tool_approval(req):
                    return await execute(req)
            return await execute(req)
        try:
            result = await approval.awrap_tool_call(request, approved)
        except BaseException as exc:
            if not executed:
                self._gate_event("interrupted" if type(exc).__name__ == "GraphInterrupt" else "cancelled" if type(exc).__name__ == "CancelledError" else "tool_error", request)
            raise
        if not executed:
            self._gate_event("denied", request)
        return result


def compiled_middleware(graph: Any) -> list[Any]:
    """Inspect bound graph callbacks, rather than trusting the supplied list."""
    seen: set[int] = set()
    owners: list[Any] = []

    def visit(value: Any, depth: int = 0) -> None:
        if value is None or id(value) in seen or depth > 32:
            return
        seen.add(id(value))
        if inspect.ismethod(value):
            owner = value.__self__
            if isinstance(owner, AgentMiddleware) and all(owner is not item for item in owners):
                owners.append(owner)
            visit(value.__func__, depth + 1)
        elif inspect.isfunction(value):
            for item in inspect.getclosurevars(value).nonlocals.values():
                visit(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item, depth + 1)
        else:
            for attribute in ("bound", "func", "afunc", "steps"):
                item = getattr(value, attribute, None)
                if item is not value:
                    visit(item, depth + 1)
    for node in getattr(graph, "nodes", {}).values():
        visit(getattr(node, "bound", None))
    return owners


def effective_manifest(graph: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Validate declared Manta controls against each actual compiled graph."""
    node = tool_node(graph)
    children = child_graphs(node)
    specs = {s["name"]: s for s in kwargs.get("subagents", ()) if isinstance(s, dict)}
    missing = [name for name, spec in specs.items() if any(isinstance(m, RuntimeDispatchMiddleware) for m in spec.get("middleware", ())) and name not in children]
    if missing:
        raise RuntimeBuildError(f"Cannot inspect required compiled subagents: {', '.join(missing)}")

    def entry(name: str, current: Any, mws: list[Any], selection: list[str] | None) -> dict[str, Any]:
        current_node = tool_node(current)
        attached = compiled_middleware(current)
        required = [m for m in mws if m.name.startswith("Manta.") or type(m).__name__ == "SessionPolicyMiddleware"]
        absent = [m.name for m in required if all(m is not found for found in attached)]
        if absent:
            raise RuntimeBuildError(f"{name}: required controls absent from compiled graph: {', '.join(absent)}")
        dispatches = [mw for mw in mws if isinstance(mw, RuntimeDispatchMiddleware)]
        if len(dispatches) != 1:
            raise RuntimeBuildError(f"{name}: expected exactly one guarded dispatch middleware")
        dispatches[0].bind(current)
        policies = [mw for mw in mws if isinstance(mw, ToolPolicyMiddleware)]
        tools = []
        from .agents.factory import DATABRICKS_TOOL_NAMES
        for tool_name, tool in sorted(current_node.tools_by_name.items()):
            effect = tool_effect(tool_name, tool)
            read_allowed = all(not p._read_only or effect in {"read", "dispatch"} or (p._allow_delegation and effect == "delegate") for p in policies)
            denied = not read_allowed or any(tool_name in p._deny or (p._allow is not None and tool_name not in p._allow)
                                            or (p._filesystem and effect in {"execute", "unknown", "delegate"}) for p in policies)
            conditional = any(p._filesystem or tool_name in p._ask for p in policies)
            tools.append({"name": tool_name, "effect": effect, "read_only_allowed": read_allowed,
                          "policy_status": "denied" if denied else "conditional" if conditional else "allowed"})
        return {"name": name, "tools": tools, "middleware": [mw.name for mw in attached],
                "graph_nodes": sorted(current.nodes), "profile": dispatches[0].agent,
                "databricks_tools": selection if selection is not None else dispatches[0].databricks_selection,
                "effective_databricks_tools": sorted(set(current_node.tools_by_name) & DATABRICKS_TOOL_NAMES),
                "programmatic_dispatch": "guarded", "validated": True}

    manifest = {"schema_version": 1, "validated": True,
                "validation_scope": "primary and inspectable inline tool controls",
                "unvalidated_subagents": [name for name, spec in specs.items() if not any(isinstance(m, RuntimeDispatchMiddleware) for m in spec.get("middleware", ()))],
                "primary": entry("primary", graph, kwargs.get("middleware", []), kwargs.get("_manta_primary_databricks_tools")), "agents": []}
    for name, spec in specs.items():
        if name in children and any(isinstance(m, RuntimeDispatchMiddleware) for m in spec.get("middleware", ())):
            middleware = spec["middleware"]
            if spec.get("mode") == "fork":
                middleware = list({m.name: m for m in [*kwargs.get("middleware", []), *middleware]}.values())
            manifest["agents"].append(entry(name, children[name], middleware, spec.get("_manta_databricks_tools")))
    return manifest


def publish_manifest(manifest: dict[str, Any] | None) -> None:
    global _LAST_MANIFEST
    _LAST_MANIFEST = manifest


def last_effective_manifest() -> dict[str, Any] | None:
    return _LAST_MANIFEST
