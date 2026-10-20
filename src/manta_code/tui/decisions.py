"""Version-qualified presentation bridge for native and per-call interrupts.

The graph and task store retain their original interrupt schema. Only this
invocation's UI stream sees the older batched presentation expected by the
locked upstream Textual adapter. Widget futures are bound to exact graph IDs.
"""
from __future__ import annotations

import functools
import inspect
import json
from dataclasses import replace
from typing import Any

from ..tasks.approvals import native_input_request, per_call_request


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _actions(request: dict) -> list[dict]:
    if per_call_request(request):
        return [{key: request[key] for key in ("name", "args", "description") if key in request}]
    return request.get("action_requests", [])


def _widget_future(widget: Any) -> Any:
    return (getattr(widget, "_future", None)
            or getattr(getattr(widget, "_completion", None), "_future", None))


class DecisionStreamBridge:
    """Delegate graph operations and translate only astream's UI boundary."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.pending: dict[str, dict] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.agent, name)

    async def astream(self, value: Any, *args: Any, **kwargs: Any):
        from langgraph.types import Command

        if isinstance(value, Command) and isinstance(value.resume, dict):
            converted = dict(value.resume)
            for identity, response in converted.items():
                if identity not in self.pending or not per_call_request(self.pending[identity]):
                    continue
                decisions = response.get("decisions", []) if isinstance(response, dict) else []
                if len(decisions) != 1 or not isinstance(decisions[0], dict):
                    raise ValueError("A per-call approval requires one exact decision")
                converted[identity] = decisions[0]
            value = replace(value, resume=converted)
        self.pending = {}
        async for namespace, mode, data in self.agent.astream(value, *args, **kwargs):
            if mode == "updates" and isinstance(data, dict) and data.get("__interrupt__"):
                presented = []
                for interrupt in data["__interrupt__"]:
                    request = interrupt.value
                    if (isinstance(request, dict) and (per_call_request(request)
                            or native_input_request(request) or request.get("action_requests"))):
                        # Detach the sidecar from stream/client mutable values.
                        self.pending[interrupt.id] = json.loads(json.dumps(request))
                    if per_call_request(request):
                        envelope = {
                            "action_requests": _actions(request),
                            "review_configs": [{"action_name": request["name"],
                                                "allowed_decisions": ["approve", "reject"]}],
                        }
                        presented.append(replace(interrupt, value=envelope))
                    else:
                        presented.append(interrupt)
                data = {**data, "__interrupt__": presented}
            yield namespace, mode, data

    def _matching(self, payload: Any, *, kind: str) -> dict[str, dict]:
        matches = {}
        for identity, request in self.pending.items():
            expected = request.get("questions") if kind == "input" else _actions(request)
            if (request.get("type") == "ask_user") != (kind == "input"):
                continue
            if _canonical(expected) == _canonical(payload):
                matches[identity] = request
        # Identical sibling prompts cannot be assigned a graph ID by content.
        return matches if len(matches) == 1 else {}

    def bind_callback(self, callback: Any, *, kind: str):
        if callback is None:
            return None

        @functools.wraps(callback)
        async def request(payload: Any, *args: Any, **kwargs: Any):
            future = await callback(payload, *args, **kwargs)
            owner = getattr(callback, "__self__", None)
            widget_name = "_pending_ask_user_widget" if kind == "input" else "_pending_approval_widget"
            widget = getattr(owner, widget_name, None)
            if widget is not None and _widget_future(widget) is future:
                identities = self._matching(payload, kind=kind)
                widget._manta_interrupts = identities
                # The board checks both references and refuses a completed
                # future, including a stale widget awaiting unmount.
                widget._manta_decision_future = future
            return future

        return request


def install_decision_stream_bridge() -> bool:
    """Install against the exact execute/callback seams exercised in tests."""
    from deepagents_code.tui import textual_adapter

    original = textual_adapter.execute_task_textual
    if getattr(original, "__manta_decisions__", False):
        return True
    signature = inspect.signature(original)
    if not {"agent", "adapter"} <= set(signature.parameters):
        return False

    @functools.wraps(original)
    async def execute(*args: Any, **kwargs: Any):
        bound = signature.bind(*args, **kwargs)
        adapter = bound.arguments["adapter"]
        bridge = DecisionStreamBridge(bound.arguments["agent"])
        bound.arguments["agent"] = bridge
        callbacks = {name: getattr(adapter, name, None)
                     for name in ("_request_approval", "_request_ask_user")}
        adapter._request_approval = bridge.bind_callback(callbacks["_request_approval"], kind="approval")
        adapter._request_ask_user = bridge.bind_callback(callbacks["_request_ask_user"], kind="input")
        try:
            return await original(*bound.args, **bound.kwargs)
        finally:
            for name, callback in callbacks.items():
                setattr(adapter, name, callback)

    execute.__manta_decisions__ = True
    textual_adapter.execute_task_textual = execute
    return True
