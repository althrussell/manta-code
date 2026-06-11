"""Deliver task-inbox steering messages into a running task (ADR 0011).

Background tasks were fire-and-forget; the task inbox makes them steerable:
``manta task send <id> "<message>"`` queues a note, and this middleware
delivers it inside the detached run.

Delivery is a **checkpointed state update**, not a per-call injection: the
middleware implements ``before_model``, whose returned ``{"messages": [...]}``
goes through the agent graph's ``add_messages`` reducer and is persisted to
the thread *before* the model node runs. The note therefore lands in the
thread history, survives interrupts/retries, and shapes every subsequent
model call. (Injecting via ``wrap_model_call`` was rejected in design review:
it affects exactly one call, never persists, and an inner middleware's
``GraphInterrupt`` would lose already-drained rows.)

Consumption is per-row (by id), so a message inserted between read and mark
waits for the next model call instead of being silently swallowed.

Attached **once, at the orchestrator level**, and only when ``MANTA_TASK_ID``
is set — never per-subagent, so a delegated subagent can't consume the user's
steering note into its own context. Fully guarded: a store failure delivers
nothing and breaks nothing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage

logger = logging.getLogger("manta.inbox")

#: Prefix marking injected steering notes in the thread history.
STEERING_PREFIX = "[Steering note from the user (mid-task)]"


class InboxMiddleware(AgentMiddleware):
    """Drain the task inbox into the thread before each model call."""

    def __init__(self, *, task_id: str) -> None:
        super().__init__()
        self._task_id = task_id

    @property
    def name(self) -> str:
        return f"Manta.Inbox.{self._task_id}"

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        try:
            from ..tasks.store import mark_inbox_consumed, unconsumed_inbox

            pending = unconsumed_inbox(self._task_id)
            if not pending:
                return None
            mark_inbox_consumed([m.id for m in pending])
            notes = [
                HumanMessage(content=f"{STEERING_PREFIX}: {m.message}")
                for m in pending
            ]
            logger.info(
                "Delivered %d steering note(s) into task %s",
                len(notes),
                self._task_id,
            )
            return {"messages": notes}
        except Exception:  # noqa: BLE001 - steering must never break the task
            logger.debug("task inbox delivery failed", exc_info=True)
            return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


def task_inbox_middleware() -> AgentMiddleware | None:
    """Inbox middleware for the current background task, or ``None``.

    Active only inside a task runner (``MANTA_TASK_ID`` exported by the
    executor); interactive sessions get nothing.
    """
    task_id = os.environ.get("MANTA_TASK_ID")
    if not task_id:
        return None
    try:
        return InboxMiddleware(task_id=task_id)
    except Exception:  # noqa: BLE001
        return None
