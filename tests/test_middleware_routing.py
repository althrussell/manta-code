"""Tests for cost-aware model routing (Phase 4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("langchain.agents.middleware.types")

from langchain_core.messages import HumanMessage  # noqa: E402

from manta_code.middleware import routing as R  # noqa: E402


def test_should_escalate_keywords():
    assert R.should_escalate("Help me design the architecture")
    assert R.should_escalate("debug this failing job")
    assert R.should_escalate("plan the migration")
    assert not R.should_escalate("rename this variable")
    assert not R.should_escalate("")


@dataclass
class _Request:
    messages: list = field(default_factory=list)
    model: Any = "cheap"
    _overridden: Any = None

    def override(self, **kwargs):
        self._overridden = kwargs
        return self


def test_no_resolver_is_noop():
    mw = R.ModelRoutingMiddleware(resolve_model=None)
    req = _Request(messages=[HumanMessage(content="design a big architecture")])
    seen = {}

    def handler(r):
        seen["req"] = r
        return "resp"

    assert mw.wrap_model_call(req, handler) == "resp"
    assert seen["req"] is req
    assert req._overridden is None  # not escalated


def test_escalates_on_hard_step():
    resolved = []

    def resolver(name):
        resolved.append(name)
        return f"model::{name}"

    mw = R.ModelRoutingMiddleware(resolve_model=resolver, premium_endpoint="premium-x")
    req = _Request(messages=[HumanMessage(content="please architect the new pipeline")])

    captured = {}

    def handler(r):
        captured["overridden"] = getattr(r, "_overridden", None)
        return "ok"

    mw.wrap_model_call(req, handler)
    assert resolved == ["premium-x"]
    assert captured["overridden"] == {"model": "model::premium-x"}


def test_does_not_escalate_easy_step():
    def resolver(name):  # pragma: no cover - should not be called
        raise AssertionError("resolver should not run for an easy step")

    mw = R.ModelRoutingMiddleware(resolve_model=resolver)
    req = _Request(messages=[HumanMessage(content="add a docstring")])
    mw.wrap_model_call(req, lambda r: "ok")
    assert req._overridden is None


def test_default_routing_middleware_builds_or_skips():
    # In this env the Databricks chat class imports, so we expect one middleware;
    # the contract is simply "a list of AgentMiddleware, never raises".
    mws = R.default_routing_middleware()
    assert isinstance(mws, list)
    for mw in mws:
        assert isinstance(mw, R.ModelRoutingMiddleware)


def test_databricks_resolver_is_callable_or_none():
    resolver = R.databricks_model_resolver()
    assert resolver is None or callable(resolver)


def test_resolver_failure_falls_back():
    def resolver(name):
        raise RuntimeError("cannot build model")

    mw = R.ModelRoutingMiddleware(resolve_model=resolver)
    req = _Request(messages=[HumanMessage(content="debug the crash")])
    # Must not raise; falls back to the original request.
    assert mw.wrap_model_call(req, lambda r: "ok") == "ok"
