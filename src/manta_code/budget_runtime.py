"""Persist budget pauses before the unattended client can auto-approve them."""

from __future__ import annotations

import json
import os
from typing import Any


class BudgetPaused(ValueError):
    """The graph is checkpointed and requires explicit user continuation."""


class TaskPaused(BudgetPaused):
    """A managed task retained a human decision in its durable checkpoint."""


def pending_requests(snapshot: Any) -> dict[str, dict]:
    """Read exact interrupt content, rejecting ambiguous checkpoint identities."""
    from .tasks.approvals import automatic_request, native_input_request, per_call_request
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    pending = {}
    for item in interrupts:
        identity, value = getattr(item, "id", None), getattr(item, "value", None)
        if not isinstance(identity, str) or not identity or not isinstance(value, dict):
            raise ValueError("Unsupported checkpoint interrupt; nothing resumed")
        actions = value.get("action_requests")
        if not (native_input_request(value) or per_call_request(value) or automatic_request(value)) and (not isinstance(actions, list) or not actions or not all(isinstance(a, dict) and a.get("name") for a in actions)):
            raise ValueError("Unsupported checkpoint decision; nothing resumed")
        if identity in pending and pending[identity] != value:
            raise ValueError("Ambiguous checkpoint decision; nothing resumed")
        pending[identity] = value
    return pending


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

    if not getattr(client._resolve_shell_allow_list, "__manta_task_shell__", False):
        original_shell = client._resolve_shell_allow_list

        def task_shell_allow_list():
            task_id = os.environ.get("MANTA_TASK_ID")
            if not task_id:
                return original_shell()
            from deepagents_code.config import parse_shell_allow_list_items
            from .agents.registry import AgentDef
            from .tasks import store

            record = store.get_task(task_id)
            if record is None or record.execution_kind != "detached":
                raise ValueError("A managed task requires its stored shell policy before launch")
            entries = AgentDef(name="task-shell", task_shell_allow_list=
                               record.manifest.get("task_shell_allow_list", [])).task_shell_allow_list
            # The upstream resolver treats empty environment strings as unset
            # and may otherwise inherit an unrestricted user/managed setting.
            # Resolve the reviewed task list before the client chooses shell
            # availability, approval mode and restrictive server middleware.
            return parse_shell_allow_list_items(entries)

        task_shell_allow_list.__manta_task_shell__ = True
        client._resolve_shell_allow_list = task_shell_allow_list

    if (getattr(client._process_hitl_interrupts, "__manta_budget__", False)
            and getattr(client._process_interrupts, "__manta_task_pending__", False)):
        return True
    original_process = client._process_hitl_interrupts
    original_interrupts = client._process_interrupts
    original_stream = client._stream_agent
    original_generate = client.generate_thread_id
    resume_used = False

    def extract(data: dict, state: Any, console: Any) -> None:
        from .tasks.approvals import automatic_request, managed_task, native_input_request, per_call_request

        if not managed_task():
            original_interrupts(data, state, console)
            return
        remaining = []
        for item in data.get("__interrupt__", ()):
            value = getattr(item, "value", None)
            if per_call_request(value) or native_input_request(value):
                identity = getattr(item, "id", None)
                if not isinstance(identity, str) or not identity:
                    raise ValueError("A task pause requires exact interrupt identities")
                existing = state.pending_interrupts.get(identity)
                if existing is not None and existing != value:
                    raise ValueError("Ambiguous checkpoint decision; nothing resumed")
                # Preserve the raw schema. Converting a per-call request into
                # a batch would change both its consent scope and resume type.
                state.pending_interrupts[identity] = value
                state.interrupt_occurred = True
            else:
                if automatic_request(value):
                    identity = getattr(item, "id", None)
                    if not isinstance(identity, str) or not identity:
                        raise ValueError("An automatic task pause requires exact interrupt identities")
                    automatic = getattr(state, "_manta_automatic_requests", {})
                    if identity in automatic and automatic[identity] != value:
                        raise ValueError("Ambiguous automatic checkpoint decision; nothing resumed")
                    automatic[identity] = json.loads(json.dumps(value))
                    state._manta_automatic_requests = automatic
                remaining.append(item)
        if remaining:
            original_interrupts({**data, "__interrupt__": remaining}, state, console)

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
                raise ValueError("Missing explicit task continuation decision")
            decisions = json.loads(raw)
            if not isinstance(decisions, dict) or not decisions:
                raise ValueError("Invalid budget continuation decision")
            # HTTP thread registration is ephemeral on the dev server while
            # SQLite checkpoints survive. Register before reading after a
            # detached runner restart; this does not replay or mutate inputs.
            await agent.aensure_thread(config)
            snapshot = await agent.aget_state(config)
            if not getattr(snapshot, "values", None):
                raise ValueError("Saved task checkpoint is unavailable or already complete; nothing resumed")
            expected_raw = os.environ.get("MANTA_RESUME_PENDING")
            if expected_raw:
                expected = json.loads(expected_raw)
                actual = pending_requests(snapshot)
                if not isinstance(expected, dict) or not expected or actual != expected or set(decisions) != set(actual):
                    raise ValueError("Saved task decision does not match the pending checkpoint content; nothing resumed")
            elif set(decisions) != _pending_budget_interrupt_ids(snapshot):
                raise ValueError("Saved budget continuation does not match the pending checkpoint interrupts; nothing resumed")
            stream_input = Command(resume=decisions)
            resume_used = True
        if state is not None:
            state._manta_automatic_requests = {}
        return await original_stream(agent, stream_input, config, state, console, tracker, context)

    async def process(state: Any, console: Any) -> None:
        from .tasks.approvals import checkpoint_requests, managed_task
        if managed_task() and state.pending_interrupts:
            task_id = os.environ["MANTA_TASK_ID"]
            automatic_requests = getattr(state, "_manta_automatic_requests", {})
            computed = {**getattr(state, "hook_response", {}), **getattr(state, "skill_trust_response", {})}
            checkpoint_requests(task_id, state.thread_id, state.pending_interrupts,
                                automatic_requests=automatic_requests,
                                automatic_responses={key: computed[key] for key in automatic_requests if key in computed})
            console.print(f"Task {task_id} is waiting for a human decision. Inspect it in /tasks or manta task status {task_id}.")
            raise TaskPaused("Task checkpoint retained for explicit human continuation")
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
    extract.__manta_task_pending__ = True
    client.generate_thread_id = generate_thread_id
    client._stream_agent = stream
    client._process_hitl_interrupts = process
    client._process_interrupts = extract
    return True
