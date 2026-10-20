"""Persist budget pauses before the unattended client can auto-approve them."""

from __future__ import annotations

import json
import os
from typing import Any


class BudgetPaused(ValueError):
    """The graph is checkpointed and requires explicit user continuation."""


def _pending_budget_interrupt_ids(snapshot: Any) -> set[str]:
    """Read checkpointed budget interrupts without approving unrelated work."""
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    pending = set()
    for interrupt in interrupts:
        value = getattr(interrupt, "value", None)
        actions = value.get("action_requests", ()) if isinstance(value, dict) else ()
        if actions and all(action.get("name") == "manta_budget_continue" for action in actions):
            interrupt_id = getattr(interrupt, "id", None)
            if isinstance(interrupt_id, str) and interrupt_id:
                pending.add(interrupt_id)
        else:
            # A saved budget decision must never approve a later tool or hook
            # interrupt, even if an old decision happens to share its ID.
            return set()
    return pending


def install_budget_runtime() -> bool:
    from deepagents_code.client import non_interactive as client
    from langgraph.types import Command

    if getattr(client._process_hitl_interrupts, "__manta_budget__", False):
        return True
    original_process = client._process_hitl_interrupts
    original_stream = client._stream_agent
    original_generate = client.generate_thread_id
    resume_used = False

    def generate_thread_id() -> str:
        return os.environ.get("MANTA_RESUME_THREAD_ID") or original_generate()

    async def stream(agent: Any, stream_input: Any, config: dict, state: Any,
                     console: Any, tracker: Any, context: Any) -> Any:
        nonlocal resume_used
        thread_id = config.get("configurable", {}).get("thread_id", "")
        task_id = os.environ.get("MANTA_TASK_ID")
        if task_id:
            from .tasks import store
            if store.get_task(task_id) is not None:
                store.update_task(task_id, thread_id=thread_id)
        if os.environ.get("MANTA_RESUME_THREAD_ID") and not resume_used:
            raw = os.environ.get("MANTA_RESUME_VALUE")
            if not raw:
                raise ValueError("Missing explicit budget continuation decision")
            decisions = json.loads(raw)
            if not isinstance(decisions, dict) or not decisions:
                raise ValueError("Invalid budget continuation decision")
            # HTTP thread registration is ephemeral on the dev server while
            # SQLite checkpoints survive. Register before reading after a
            # detached runner restart; this does not replay or mutate inputs.
            await agent.aensure_thread(config)
            snapshot = await agent.aget_state(config)
            if not getattr(snapshot, "values", None) or not _pending_budget_interrupt_ids(snapshot):
                raise ValueError("Saved task checkpoint is unavailable or already complete; nothing resumed")
            if set(decisions) != _pending_budget_interrupt_ids(snapshot):
                raise ValueError("Saved budget continuation does not match the pending checkpoint interrupts; nothing resumed")
            stream_input = Command(resume=decisions)
            resume_used = True
        return await original_stream(agent, stream_input, config, state, console, tracker, context)

    async def process(state: Any, console: Any) -> None:
        budget_requests = {}
        descriptions = []
        for interrupt_id, request in state.pending_interrupts.items():
            actions = request.get("action_requests", [])
            if any(a.get("name") == "manta_budget_continue" for a in actions):
                if not all(a.get("name") == "manta_budget_continue" for a in actions):
                    raise ValueError("Mixed budget/tool approval requires interactive review")
                budget_requests[interrupt_id] = {"decisions": [{"type": "approve"} for _ in actions]}
                descriptions.extend(a.get("description", "Budget reached") for a in actions)
        if budget_requests:
            task_id = os.environ.get("MANTA_TASK_ID")
            reason = "\n".join(descriptions)
            from .tasks import store
            if task_id and store.get_task(task_id) is not None:
                store.pause_task(task_id, reason, thread_id=state.thread_id,
                                 checkpoint={"resume_value": budget_requests})
                hint = f"manta task resume {task_id}"
            else:
                hint = f"manta -r {state.thread_id}"
            console.print(f"Budget paused; checkpoint retained for thread {state.thread_id}. Continue explicitly: {hint}")
            raise BudgetPaused(reason)
        await original_process(state, console)

    process.__manta_budget__ = True
    client.generate_thread_id = generate_thread_id
    client._stream_agent = stream
    client._process_hitl_interrupts = process
    return True
