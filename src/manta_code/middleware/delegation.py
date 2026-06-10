"""Deterministic plan-request delegation (ADR 0008).

Manta ships a dedicated ``planning`` agent (read-only, pinned to a strong
reasoner). The problem: deepagents gives the *orchestrator* its own
``write_todos`` planning tool and its default prompt strongly prefers planning
inline, so empirically **no** orchestrator model — cheap or premium — reliably
delegates "make a plan" to the planning agent on its own. A prompt nudge can't
beat a built-in tool the model was trained to reach for.

This middleware closes that gap deterministically. When the *base orchestrator*
(no specific Manta profile selected as primary) receives a fresh, plan-intent
human turn, it **short-circuits the model call** and returns a synthesized
``task(subagent_type="planning", ...)`` tool call. The agent loop then runs the
real planning agent — Opus-grade, read-only, with its own memory — instead of
the orchestrator planning inline. No reliance on the model choosing to comply.

Safety / scope:

- **Base orchestrator only.** It is wired in :func:`manta_code.hook.build_orchestrator_middleware`
  *only* when no profile is the primary loop (if the user picked ``planning`` it
  already *is* the planner; if they picked ``swe`` we don't hijack it).
- **Fires once per request.** It only triggers when the *last* message is the
  human plan request (the first model call of the turn). After the ``task`` tool
  returns, the last message is a ``ToolMessage`` so it never re-fires — no loop.
- **Conservative intent match.** :func:`plan_intent` requires an explicit
  *request to produce a plan* and skips execution asks ("implement the plan…")
  and references to an existing plan, to avoid hijacking normal work.
- **Guarded + toggleable.** Any error falls back to a normal model call, and the
  whole behavior can be disabled with ``MANTA_AUTODELEGATE_PLANNING=0``.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage

#: Default subagent a plan request is routed to.
DEFAULT_TARGET = "planning"

#: Env var to disable auto-delegation of plan requests.
_ENV_TOGGLE = "MANTA_AUTODELEGATE_PLANNING"

# A request to *produce* a plan: a planning verb near the word "plan", or an
# imperative "plan …", or a "design/architect a …" ask. Kept deliberately tight.
_PLAN_REQUEST: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(create|make|draft|write|compose|come up with|give me|need|want|"
        r"propose|put together|draw up|prepare|build me|sketch)\b"
        r"[^.?!\n]{0,30}\bplan\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(please\s+)?plan\b(?!\.)", re.IGNORECASE),
    re.compile(r"\bplan\s+(this|it|that|these|the\s+\w+)\s+out\b", re.IGNORECASE),
    re.compile(r"\b(design|architect)\s+(a|an|the)\s+\w+", re.IGNORECASE),
)

# Leads that mean "do the work", not "plan it" — never treat as plan intent.
_EXECUTE_LEAD = re.compile(
    r"^\s*(please\s+)?(implement|execute|build out|code|run|apply|do|carry out|follow)\b",
    re.IGNORECASE,
)

# A reference to an *existing* plan ("implement the plan"), as opposed to a
# request for a new one ("create a plan").
_PLAN_REFERENCE = re.compile(
    r"\b(the|this|that|existing|approved|above|attached)\s+plan\b", re.IGNORECASE
)
_NEW_PLAN = re.compile(r"\b(a|an|another|new)\s+plan\b", re.IGNORECASE)


def plan_intent(text: str) -> bool:
    """Return ``True`` when ``text`` is a request to *produce a plan*.

    Pure and case-insensitive so the policy is unit-testable without a model.
    Skips execution asks and references to a pre-existing plan to keep
    false-positives low (we'd rather miss an odd phrasing than hijack a normal
    request).
    """
    if not text or not text.strip():
        return False
    t = text.strip()
    if _EXECUTE_LEAD.match(t):
        return False
    if not any(p.search(t) for p in _PLAN_REQUEST):
        return False
    # "implement the plan" style: a reference to an existing plan, not a new ask.
    if _PLAN_REFERENCE.search(t) and not _NEW_PLAN.search(t):
        return False
    return True


def _messages(request: Any) -> list[Any]:
    return list(getattr(request, "messages", None) or [])


def _is_human(msg: Any) -> bool:
    return getattr(msg, "type", None) == "human" or msg.__class__.__name__ == "HumanMessage"


def _content_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    return content if isinstance(content, str) else str(content)


def _tool_name(tool: Any) -> str | None:
    name = getattr(tool, "name", None)
    if name is None and isinstance(tool, dict):
        name = tool.get("name") or (tool.get("function") or {}).get("name")
    return name if isinstance(name, str) else None


def _task_tool(tools: Any) -> Any | None:
    for tool in tools or []:
        if _tool_name(tool) == "task":
            return tool
    return None


def _tool_description(tool: Any) -> str:
    desc = getattr(tool, "description", None)
    if desc is None and isinstance(tool, dict):
        desc = tool.get("description") or (tool.get("function") or {}).get("description")
    return desc if isinstance(desc, str) else ""


class PlanDelegationMiddleware(AgentMiddleware):
    """Force plan requests to the planning agent by synthesizing a ``task`` call.

    On the first model call of a plan-intent human turn, returns an
    :class:`~langchain_core.messages.AIMessage` carrying a ``task`` tool call
    (``subagent_type=<target>``) *instead of* invoking the model. The agent loop
    executes the tool, running the real planning agent. Every other call passes
    straight through to the model.
    """

    def __init__(self, *, target: str = DEFAULT_TARGET) -> None:
        super().__init__()
        self._target = target

    @property
    def name(self) -> str:
        return "Manta.PlanDelegation"

    def _delegated_message(self, request: Any) -> AIMessage | None:
        """Synthesized ``task`` AIMessage to short-circuit with, or ``None``."""
        messages = _messages(request)
        if not messages:
            return None
        last = messages[-1]
        if not _is_human(last):
            return None
        text = _content_text(last)
        if not plan_intent(text):
            return None
        task = _task_tool(getattr(request, "tools", None))
        if task is None:
            return None
        # If we can read the task tool's allowed-agents description, only fire
        # when the target is actually available; otherwise proceed (the tool
        # itself returns a graceful error for an unknown subagent_type).
        desc = _tool_description(task)
        if self._target and desc and self._target not in desc:
            return None
        tool_call = {
            "name": "task",
            "args": {"description": text, "subagent_type": self._target},
            "id": f"manta_plan_{uuid.uuid4().hex[:8]}",
            "type": "tool_call",
        }
        return AIMessage(
            content=f"Routing this planning request to the `{self._target}` agent.",
            tool_calls=[tool_call],
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            message = self._delegated_message(request)
        except Exception:  # noqa: BLE001 - delegation must never break a call
            message = None
        if message is not None:
            return message
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            message = self._delegated_message(request)
        except Exception:  # noqa: BLE001 - delegation must never break a call
            message = None
        if message is not None:
            return message
        return await handler(request)


def _env_enabled() -> bool:
    value = os.getenv(_ENV_TOGGLE)
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def plan_delegation_middleware(target: str = DEFAULT_TARGET) -> AgentMiddleware | None:
    """Plan-delegation middleware, or ``None`` if disabled via env.

    Safe to call and append unconditionally for the base orchestrator.
    """
    if not _env_enabled():
        return None
    return PlanDelegationMiddleware(target=target)
