"""``@{agent}`` addressing: hand a turn (or a background task) to a named agent.

The vision's pillar 4 surface (ADR 0010, Phase B). Two forms, parsed from the
start of a human message:

- ``@swe land this refactor`` — **inline** delegation: the model call is
  short-circuited with a synthesized ``task`` tool call (the proven
  :class:`~manta_code.middleware.delegation.PlanDelegationMiddleware` pattern),
  so the named agent runs deterministically with its enforced permissions, and
  you watch it work.
- ``@swe land this refactor &`` — **background**: the work is submitted as a
  detached task (:mod:`manta_code.tasks.executor`) and the turn returns
  immediately with the task id; you keep working while it runs. Collect with
  ``manta task output <id>`` or the in-session task tools.

Safety / scope mirrors plan delegation: fires only when the *last* message is
the human ``@agent`` turn (once per request, never re-fires after the tool
runs), only for names that resolve to real Manta agents, falls back to a
normal model call on any error, and can be disabled with
``MANTA_AGENT_ADDRESSING=0``.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage

logger = logging.getLogger("manta.address")

#: Env var to disable @agent addressing.
_ENV_TOGGLE = "MANTA_AGENT_ADDRESSING"

#: ``@name`` at the start of the message; name must look like an agent slug.
_ADDRESS = re.compile(r"^\s*@([a-z0-9][a-z0-9-]*)\b[,:]?\s*(.*)$", re.DOTALL)

#: A trailing ``&`` (its own token) requests background execution.
_BACKGROUND = re.compile(r"(?:^|\s)&\s*$")


def parse_address(text: str) -> tuple[str, str, bool] | None:
    """Parse ``@agent task text [&]`` → ``(agent, task_text, background)``.

    Returns ``None`` when the message is not an @-address or carries no task
    text (a bare ``@swe`` falls through to the model, which can ask what the
    user wants). Pure, so the grammar is unit-testable.
    """
    if not text:
        return None
    match = _ADDRESS.match(text)
    if not match:
        return None
    agent, rest = match.group(1), match.group(2).strip()
    background = False
    if _BACKGROUND.search(rest):
        rest = _BACKGROUND.sub("", rest).strip()
        background = True
    if not rest:
        return None
    return agent, rest, background


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


def _has_task_tool(request: Any) -> bool:
    for tool in getattr(request, "tools", None) or []:
        if _tool_name(tool) == "task":
            return True
    return False


class AgentAddressMiddleware(AgentMiddleware):
    """Route ``@agent`` turns to the named agent — inline or as a background task.

    Args:
        known_agents: zero-arg callable returning the addressable agent names
            (resolved lazily per turn so newly created agents work without a
            restart).
        submit: callable ``(agent, prompt) -> str`` returning a human-readable
            submission receipt for the background (``&``) form.
    """

    def __init__(
        self,
        *,
        known_agents: Callable[[], set[str]],
        submit: Callable[[str, str], str],
    ) -> None:
        super().__init__()
        self._known_agents = known_agents
        self._submit = submit

    @property
    def name(self) -> str:
        return "Manta.AgentAddress"

    def _short_circuit(self, request: Any) -> AIMessage | None:
        messages = _messages(request)
        if not messages or not _is_human(messages[-1]):
            return None
        parsed = parse_address(_content_text(messages[-1]))
        if parsed is None:
            return None
        agent, task_text, background = parsed
        if agent not in self._known_agents():
            return None  # not an agent — could be a handle in prose; don't hijack

        if background:
            receipt = self._submit(agent, task_text)
            # No tool calls: this ends the turn immediately with the receipt.
            return AIMessage(content=receipt)

        if not _has_task_tool(request):
            return None  # inline delegation needs the task tool; let the model handle it
        tool_call = {
            "name": "task",
            "args": {"description": task_text, "subagent_type": agent},
            "id": f"manta_addr_{uuid.uuid4().hex[:8]}",
            "type": "tool_call",
        }
        return AIMessage(
            content=f"Handing this to the `{agent}` agent as requested.",
            tool_calls=[tool_call],
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            message = self._short_circuit(request)
        except Exception:  # noqa: BLE001 - addressing must never break a call
            logger.debug("@agent addressing failed", exc_info=True)
            message = None
        if message is not None:
            return message
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            message = self._short_circuit(request)
        except Exception:  # noqa: BLE001 - addressing must never break a call
            logger.debug("@agent addressing failed", exc_info=True)
            message = None
        if message is not None:
            return message
        return await handler(request)


def _env_enabled() -> bool:
    value = os.getenv(_ENV_TOGGLE)
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _default_known_agents() -> set[str]:
    try:
        from ..tasks.executor import known_agent_names

        return known_agent_names()
    except Exception:  # noqa: BLE001
        return set()


def _default_submit(agent: str, prompt: str) -> str:
    from ..tasks.tools import manta_task_submit

    return manta_task_submit(agent, prompt)


def agent_address_middleware() -> AgentMiddleware | None:
    """@agent addressing middleware, or ``None`` if disabled via env."""
    if not _env_enabled():
        return None
    return AgentAddressMiddleware(
        known_agents=_default_known_agents,
        submit=_default_submit,
    )
