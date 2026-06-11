from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from manta_code.agents.usage import Price
from manta_code.middleware import advice as ADV


@pytest.fixture(autouse=True)
def _manta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path))


PRICING = {
    "opus": Price(input=15.0, output=75.0),
    "mini": Price(input=0.15, output=0.60),
    "mid": Price(input=1.25, output=10.0),
}


# --- model_tier -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "tier"),
    [
        ("databricks-opus-x", "premium"),
        ("databricks-mini-x", "cheap"),
        ("databricks-mid-x", "standard"),
        ("totally-unknown", "unknown"),
        (None, "unknown"),
    ],
)
def test_model_tier(model, tier):
    assert ADV.model_tier(model, PRICING) == tier


# --- harness ----------------------------------------------------------------------


@dataclass
class _Runtime:
    thread_id: str = "t1"


@dataclass
class _Request:
    model: Any = "databricks-mini-x"
    runtime: _Runtime = field(default_factory=_Runtime)
    tool_call: dict | None = None


def _response(content: str = "ok", output_tokens: int = 50, tool_calls: list | None = None):
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "total_tokens": 100 + output_tokens,
        },
    )


def _mw(**kw) -> ADV.AdviceMiddleware:
    kw.setdefault("pricing", PRICING)
    return ADV.AdviceMiddleware(agent="tester", **kw)


def _tool_error(mw, n: int) -> None:
    req = _Request(tool_call={"name": "execute", "args": {}, "id": "1"})
    for _ in range(n):
        mw.wrap_tool_call(req, lambda r: ToolMessage(content="boom", tool_call_id="1", status="error"))


# --- escalate-on-failures -----------------------------------------------------------


def test_escalation_note_after_repeated_failures():
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    result = mw.wrap_model_call(_Request(model="databricks-mini-x"), lambda r: _response())
    assert "Manta advice" in result.content
    assert "escalating to a stronger reasoner" in result.content


def test_no_escalation_below_threshold():
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD - 1)
    result = mw.wrap_model_call(_Request(model="databricks-mini-x"), lambda r: _response())
    assert "Manta advice" not in result.content


def test_no_escalation_advice_on_premium_model():
    # Already on the strong model — nothing to escalate to.
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    result = mw.wrap_model_call(_Request(model="databricks-opus-x"), lambda r: _response())
    assert "escalating" not in str(result.content)


# --- downgrade-on-streak -------------------------------------------------------------


def test_downgrade_note_after_short_premium_streak():
    mw = _mw()
    req = _Request(model="databricks-opus-x")
    last = None
    for _ in range(ADV.STREAK_THRESHOLD):
        last = mw.wrap_model_call(req, lambda r: _response(output_tokens=80))
    assert "Manta advice" in last.content
    assert "cheaper model" in last.content


def test_long_output_resets_streak():
    mw = _mw()
    req = _Request(model="databricks-opus-x")
    for _ in range(ADV.STREAK_THRESHOLD - 1):
        mw.wrap_model_call(req, lambda r: _response(output_tokens=80))
    # One substantive output resets the boilerplate streak.
    mw.wrap_model_call(req, lambda r: _response(output_tokens=2000))
    result = mw.wrap_model_call(req, lambda r: _response(output_tokens=80))
    assert "Manta advice" not in result.content


def test_note_cooldown_prevents_nagging():
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    req = _Request(model="databricks-mini-x")
    first = mw.wrap_model_call(req, lambda r: _response())
    assert "Manta advice" in first.content
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    second = mw.wrap_model_call(req, lambda r: _response())
    assert "Manta advice" not in second.content  # cooldown active


# --- annotation safety ----------------------------------------------------------------


def test_mid_loop_tool_call_message_never_annotated():
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    tool_response = _response(
        tool_calls=[{"name": "read_file", "args": {}, "id": "x", "type": "tool_call"}]
    )
    result = mw.wrap_model_call(_Request(model="databricks-mini-x"), lambda r: tool_response)
    assert "Manta advice" not in result.content


def test_advice_recorded_to_ledger_and_events():
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    mw.wrap_model_call(_Request(model="databricks-mini-x"), lambda r: _response())
    from manta_code.agents.usage import recent_advice
    from manta_code.tasks.store import recent_events

    (record,) = recent_advice()
    assert record.kind == "escalate"
    assert record.agent == "tester"
    kinds = {e.kind for e in recent_events(limit=10)}
    assert "advice" in kinds


# --- budget trade-off interrupt --------------------------------------------------------


def test_budget_tradeoff_interrupts_once(monkeypatch):
    mw = _mw(max_usd=0.001)  # tiny budget so one premium call crosses 60%
    req = _Request(model="databricks-opus-x")
    interrupts: list[dict] = []

    import langgraph.types as lg_types

    monkeypatch.setattr(lg_types, "interrupt", lambda payload: interrupts.append(payload))
    mw.wrap_model_call(req, lambda r: _response(output_tokens=500))  # accrue spend
    mw.wrap_model_call(req, lambda r: _response(output_tokens=500))  # should pause now
    assert len(interrupts) == 1
    assert interrupts[0]["type"] == "manta_advice"
    assert interrupts[0]["kind"] == "budget_tradeoff"
    # Decided once: never re-asks on this thread.
    mw.wrap_model_call(req, lambda r: _response(output_tokens=500))
    assert len(interrupts) == 1


def test_no_interrupt_without_budget(monkeypatch):
    mw = _mw()
    import langgraph.types as lg_types

    interrupts: list = []
    monkeypatch.setattr(lg_types, "interrupt", lambda payload: interrupts.append(payload))
    req = _Request(model="databricks-opus-x")
    for _ in range(10):
        mw.wrap_model_call(req, lambda r: _response(output_tokens=4000))
    assert interrupts == []


# --- factories / toggles -----------------------------------------------------------------


def test_factories_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MANTA_ADVICE", "0")
    assert ADV.orchestrator_advice_middleware() is None
    assert ADV.agent_advice_middleware(object()) is None
    monkeypatch.setenv("MANTA_ADVICE", "1")
    assert ADV.orchestrator_advice_middleware() is not None


def test_rule_failure_never_breaks_the_call(monkeypatch):
    mw = _mw()
    monkeypatch.setattr(
        mw, "_evaluate_notes", lambda *a: (_ for _ in ()).throw(RuntimeError)
    )
    result = mw.wrap_model_call(_Request(), lambda r: _response(content="fine"))
    assert result.content == "fine"


def test_budget_tradeoff_graph_interrupt_propagates(monkeypatch):
    # interrupt() pauses by RAISING GraphInterrupt; swallowing it would make
    # the approve-to-continue tier a silent no-op (review finding).
    from langgraph.errors import GraphInterrupt

    mw = _mw(max_usd=0.001)
    req = _Request(model="databricks-opus-x")
    mw.wrap_model_call(req, lambda r: _response(output_tokens=500))  # accrue spend

    import langgraph.types as lg_types

    def _raising_interrupt(payload):
        raise GraphInterrupt()

    monkeypatch.setattr(lg_types, "interrupt", _raising_interrupt)
    with pytest.raises(GraphInterrupt):
        mw.wrap_model_call(req, lambda r: _response(output_tokens=500))
    # Not yet decided: the resume re-execution asks again (and only then
    # records), so no advice row exists from the paused attempt.
    from manta_code.agents.usage import recent_advice

    assert all(r.kind != "budget_tradeoff" for r in recent_advice())


def test_note_not_consumed_by_mid_loop_turn():
    # Escalation advice typically fires mid tool-error loop, where the model
    # response carries tool calls and cannot be annotated. The cooldown and
    # ledger row must NOT be consumed there — the note lands on the turn's
    # final user-facing answer instead (review finding).
    mw = _mw()
    _tool_error(mw, ADV.FAILURE_THRESHOLD)
    req = _Request(model="databricks-mini-x")
    tool_turn = _response(
        tool_calls=[{"name": "read_file", "args": {}, "id": "x", "type": "tool_call"}]
    )
    result = mw.wrap_model_call(req, lambda r: tool_turn)
    assert "Manta advice" not in result.content
    from manta_code.agents.usage import recent_advice

    assert recent_advice() == []  # nothing recorded as delivered yet

    final_turn = _response(content="here is the answer")
    result = mw.wrap_model_call(req, lambda r: final_turn)
    assert "Manta advice" in result.content  # delivered on the final answer
    (record,) = recent_advice()
    assert record.delivered == "note"
