"""Explicit human decisions for durable task checkpoints.

This module is shared by the board, CLI and SDK. It is deliberately absent from
the model's task tool bundle: recording a pending request does not authorize it.
"""
from __future__ import annotations

import hmac
import json
import uuid
from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId

from . import store

INPUT_ACTION = "manta_request_input"
PENDING_STATES = ("waiting_approval", "waiting_input", "paused_budget")


def per_call_request(value: Any) -> bool:
    """The locked LangChain per-call HITL schema, preserved without conversion."""
    return bool(isinstance(value, dict) and value.get("type") == "tool_approval"
                and isinstance(value.get("name"), str) and value["name"]
                and isinstance(value.get("args"), dict)
                and isinstance(value.get("tool_call_id"), str))


def native_input_request(value: Any) -> bool:
    return bool(isinstance(value, dict) and value.get("type") == "ask_user"
                and isinstance(value.get("questions"), list) and value["questions"]
                and all(isinstance(question, dict) for question in value["questions"]))


def automatic_request(value: Any) -> bool:
    """Server-owned requests whose client response was already computed."""
    return isinstance(value, dict) and value.get("type") in {"hook_invocation", "skill_trust"}


def validate_automatic_responses(requests: dict[str, dict], responses: dict[str, dict]) -> dict[str, dict]:
    """Retain hook results without rerunning them or granting new skill trust."""
    if (not isinstance(requests, dict) or not isinstance(responses, dict)
            or not all(isinstance(key, str) and key and isinstance(value, dict)
                       for mapping in (requests, responses) for key, value in mapping.items())):
        raise ValueError("Automatic checkpoint responses require exact interrupt identities and objects")
    if set(requests) != set(responses):
        raise ValueError("Every automatic checkpoint request requires its exact computed response")
    for identity, request in requests.items():
        response = responses[identity]
        if request.get("type") == "hook_invocation":
            from deepagents_code.hooks.interrupt import parse_hook_interrupt_payload, parse_hook_resume_value

            invocation = parse_hook_interrupt_payload(request)
            if invocation is None:
                raise ValueError("The saved hook request is malformed")
            parsed = parse_hook_resume_value(response, invocation_id=invocation.invocation_id,
                                             snapshot_id=invocation.snapshot_id)
            if parsed.decision.event != invocation.invocation.event.event:
                raise ValueError("The saved hook response belongs to another event")
        elif request.get("type") == "skill_trust":
            if response != {"approved": False}:
                raise ValueError("A detached continuation cannot grant new skill directory trust")
        else:
            raise ValueError("Unsupported automatic checkpoint request")
    return responses


def managed_task(task_id: str | None = None) -> bool:
    """Whether this process belongs to a real, checkpointable detached task."""
    import os
    try:
        record = store.get_task(task_id or os.environ.get("MANTA_TASK_ID", ""))
        return bool(record and getattr(record, "execution_kind", "detached") == "detached"
                    and record.state in (*store.ACTIVE_STATES, *PENDING_STATES))
    except Exception:
        return False


def request_kind(requests: dict[str, dict]) -> str:
    def only_actions(request: dict, name: str) -> bool:
        actions = request.get("action_requests", [])
        return bool(actions) and all(action.get("name") == name for action in actions)

    if requests and all(only_actions(request, "manta_budget_continue") for request in requests.values()):
        return "paused_budget"
    if requests and all(native_input_request(request) or only_actions(request, INPUT_ACTION)
                        for request in requests.values()):
        return "waiting_input"
    return "waiting_approval"


def checkpoint_requests(task_id: str, thread_id: str, requests: dict[str, dict],
                        *, path=None, automatic_requests: dict[str, dict] | None = None,
                        automatic_responses: dict[str, dict] | None = None) -> dict:
    """Persist a complete displayed decision group without granting consent."""
    if not requests or not all(isinstance(key, str) and key for key in requests):
        raise ValueError("A task pause requires exact interrupt identities")
    # A serialization round trip detaches mutable client stream state and
    # rejects opaque values before marking the task resumable.
    saved = json.loads(json.dumps(requests, sort_keys=True))
    if not all(isinstance(value, dict) and (value.get("action_requests") or
               native_input_request(value) or per_call_request(value))
               for value in saved.values()):
        raise ValueError("The pending request is not a supported task decision")
    computed_requests = json.loads(json.dumps(automatic_requests or {}, sort_keys=True))
    computed_responses = json.loads(json.dumps(automatic_responses or {}, sort_keys=True))
    if set(saved) & set(computed_requests):
        raise ValueError("Human and automatic checkpoint identities must be distinct")
    validate_automatic_responses(computed_requests, computed_responses)
    checkpoint = {"pending_id": uuid.uuid4().hex, "interrupts": {**saved, **computed_requests}}
    if computed_responses:
        checkpoint["automatic_responses"] = computed_responses
    state = request_kind(saved)
    if state == "paused_budget":
        # Preserve the established explicit budget-resume CLI contract.
        checkpoint["resume_value"] = {
            **computed_responses,
            **{key: {"decisions": [{"type": "approve"} for _ in value["action_requests"]]}
               for key, value in saved.items()},
        }
    descriptions = [str(action.get("description") or action.get("name") or "Input required")
                    for request in saved.values() for action in request.get("action_requests", [])]
    descriptions.extend(str(question.get("question", "Input required")) for request in saved.values()
                        for question in request.get("questions", []))
    descriptions.extend(str(request.get("description") or request["name"])
                        for request in saved.values() if per_call_request(request))
    store.pause_task(task_id, "\n".join(descriptions), thread_id=thread_id,
                     checkpoint=checkpoint, state=state, path=path)
    return checkpoint


def _input_response(actions: list[dict], value: Any, *, cancelled: bool = False) -> dict:
    if cancelled:
        return {"status": "cancelled", "answer": "", "answers": []}
    if len(actions) != 1:
        raise ValueError("Input requests must be answered individually")
    if isinstance(value, dict):
        value = value.get("answer", value.get("answers"))
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], str):
            raise ValueError("This input request requires one text answer")
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A non-empty input answer is required")
    return {"status": "answered", "answer": value, "answers": [value]}


def decision_mapping(requests: dict[str, dict], decision: str, value: str = "") -> dict:
    """Build a complete response; mixed groups use an exact-ID JSON object."""
    if decision not in {"approve", "reject", "answer", "cancel"}:
        raise ValueError("Choose approve, reject, answer or cancel")
    per_interrupt = None
    if decision == "answer" and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError("The response object must be valid JSON") from exc
        if isinstance(parsed, dict) and set(parsed) == set(requests):
            per_interrupt = parsed
    responses = {}
    for interrupt_id, request in requests.items():
        if per_call_request(request):
            choice = decision
            if per_interrupt is not None:
                selected = per_interrupt[interrupt_id]
                choice = selected.get("decision") if isinstance(selected, dict) else selected
            if choice == "cancel":
                choice = "reject"
            if choice not in {"approve", "reject"}:
                raise ValueError("For a mixed group, respond with a JSON object keyed by every interrupt ID; use approve/reject for actions and text for input")
            responses[interrupt_id] = {"type": choice}
            continue
        if request.get("type") == "ask_user":
            if decision == "approve":
                raise ValueError("Answer this input request before continuing")
            questions = request.get("questions", [])
            selected = per_interrupt.get(interrupt_id) if per_interrupt is not None else value
            if len(questions) != 1 or questions[0].get("type") != "text":
                raise ValueError("Answer this structured question in its owning conversation")
            responses[interrupt_id] = _input_response([{}], selected, cancelled=decision in {"cancel", "reject"})
            continue
        actions = request.get("action_requests", [])
        if not actions or not all(isinstance(action, dict) for action in actions):
            raise ValueError("The saved pending request is malformed")
        input_actions = [action.get("name") == INPUT_ACTION for action in actions]
        if any(input_actions) and not all(input_actions):
            raise ValueError("A mixed input/action interrupt requires interactive review")
        selected = per_interrupt.get(interrupt_id) if per_interrupt is not None else value
        if all(input_actions):
            if decision == "approve":
                raise ValueError("Answer this input request before continuing")
            responses[interrupt_id] = _input_response(
                actions, selected, cancelled=decision in {"cancel", "reject"})
        else:
            choice = decision
            if per_interrupt is not None:
                choice = selected.get("decision") if isinstance(selected, dict) else selected
            if choice == "cancel":
                choice = "reject"
            if choice not in {"approve", "reject"}:
                raise ValueError("For a mixed group, respond with a JSON object keyed by every interrupt ID; use approve/reject for actions and text for input")
            responses[interrupt_id] = {"decisions": [{"type": choice} for _ in actions]}
    return responses


def respond_task(task_id: str, *, pending_id: str, decision: str, value: str = "",
                 db_path=None):
    """Continue only the complete pending group that the user inspected."""
    from .executor import TaskError, resume_task
    record = store.get_task(task_id, path=db_path)
    if record is None or record.state not in PENDING_STATES:
        raise TaskError("The task is no longer waiting for this decision; refresh it")
    if getattr(record, "execution_kind", "detached") == "inline":
        raise TaskError("Inline decisions must be resolved by their owning session")
    expected = record.checkpoint.get("pending_id", "")
    if not isinstance(pending_id, str) or not expected or not hmac.compare_digest(expected, pending_id):
        raise TaskError("The pending request changed; refresh and inspect it before deciding")
    try:
        requests = record.checkpoint.get("interrupts", {})
        computed = record.checkpoint.get("automatic_responses", {})
        if not isinstance(requests, dict) or not all(isinstance(request, dict) for request in requests.values()):
            raise ValueError("The saved pending request is malformed")
        automatic = {identity: request for identity, request in requests.items() if automatic_request(request)}
        validate_automatic_responses(automatic, computed)
        human = {identity: request for identity, request in requests.items() if identity not in automatic}
        response = {**computed, **decision_mapping(human, decision, value)}
    except ValueError as exc:
        raise TaskError(str(exc)) from exc
    if not response:
        raise TaskError("The task has no pending requests")
    result = resume_task(task_id, resume_value=response, db_path=db_path,
                         expected_pending_id=pending_id)
    store.record_event(store.EventRecord(agent=record.agent, kind="task_decision",
                                        detail=decision, task_id=task_id), path=db_path)
    return result


def manta_request_input(question: str, tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
    """Ask a question and checkpoint until the user answers in Manta's task board."""
    from langgraph.types import interrupt
    if not question or not question.strip():
        raise ValueError("A non-empty question is required")
    from .events import unattended_run
    if not unattended_run():
        response = interrupt({"type": "ask_user", "questions": [{"question": question, "type": "text"}],
                              "tool_call_id": tool_call_id})
        answers = response.get("answers", []) if isinstance(response, dict) else []
        return answers[0] if answers and isinstance(answers[0], str) else "The user cancelled. Stop and report the block."
    response = interrupt({
        "action_requests": [{"name": INPUT_ACTION, "args": {"question": question},
                             "description": question}],
        "review_configs": [{"action_name": INPUT_ACTION,
                            "allowed_decisions": ["approve", "reject"]}],
    })
    if not isinstance(response, dict) or response.get("status") == "cancelled":
        return "The user cancelled the input request. Stop and report the block."
    answer = response.get("answer")
    if not isinstance(answer, str):
        raise ValueError("The saved input decision has no text answer")
    return answer
