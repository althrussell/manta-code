"""Preserve Unity Responses function-call continuation state.

LangChain 1.7.2 retains reasoning state but its standard-content conversion drops
function-call ``encrypted_content``. Gemini needs that opaque field on replay.
The stream converter also ignores function-call done events. Repairs here are
scoped to a Databricks stream and never decode, display, or reconstruct state.
"""
from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk

_OPAQUE_FIELDS = ("encrypted_content", "thought_signature")
_STATE_KEY = "manta_responses_tool_state"


def capture_response_state(message: BaseMessage) -> BaseMessage:
    """Keep opaque state through LangChain's optional standard-content output.

    Its v1 content translator strips provider extensions. A namespaced metadata
    map survives that translation as well as checkpoints; it is never rendered.
    """
    if not isinstance(message, AIMessage) or not isinstance(message.content, list):
        return message
    states = copy.deepcopy(message.additional_kwargs.get(_STATE_KEY, {}))
    for block in message.content:
        if not isinstance(block, dict) or block.get("type") != "function_call" or not block.get("call_id"):
            continue
        opaque = {key: copy.deepcopy(block[key]) for key in _OPAQUE_FIELDS if key in block}
        if opaque:
            states[block["call_id"]] = {**states.get(block["call_id"], {}), **opaque}
    if not states:
        return message
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, _STATE_KEY: states}})


def restore_responses_state(payload: dict[str, Any], messages: Sequence[BaseMessage]) -> dict[str, Any]:
    """Restore opaque tool state by exact call identity without mutating history."""
    if "input" not in payload:
        return payload
    states: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        states.update(copy.deepcopy(message.additional_kwargs.get(_STATE_KEY, {})))
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind not in {"function_call", "tool_call"}:
                continue
            call_id = block.get("call_id") if kind == "function_call" else block.get("id")
            if not isinstance(call_id, str):
                continue
            extras = block.get("extras") or {}
            opaque = {
                key: copy.deepcopy(block[key] if key in block else extras[key])
                for key in _OPAQUE_FIELDS if key in block or key in extras
            }
            if opaque:
                states[call_id] = {**states.get(call_id, {}), **opaque}
    if not states:
        return payload
    result = copy.deepcopy(payload)
    for item in result["input"]:
        if isinstance(item, dict) and item.get("type") == "function_call":
            state = states.get(item.get("call_id"))
            if state:
                item.update(state)
    return result


@dataclass
class _StreamState:
    indices: dict[str, int] = field(default_factory=dict)


_ACTIVE_STREAM: ContextVar[_StreamState | None] = ContextVar("manta_responses_opaque_stream", default=None)


def install_stream_converter() -> None:
    """Install a context-gated extension of the pinned upstream converter."""
    from langchain_openai.chat_models import base

    original = base._convert_responses_chunk_to_generation_chunk
    if getattr(original, "__manta_opaque_state__", False):
        return

    @wraps(original)
    def convert(event: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(event, *args, **kwargs)
        state = _ACTIVE_STREAM.get()
        item = getattr(event, "item", None)
        if state is None or getattr(item, "type", None) != "function_call":
            return result
        current_index, output_index, sub_index, generation = result
        identity = getattr(item, "id", None) or getattr(item, "call_id", None)
        if event.type == "response.output_item.added" and generation:
            for block in generation.message.content:
                if isinstance(block, dict) and block.get("type") == "function_call":
                    state.indices[identity] = block.get("index", current_index)
        if event.type != "response.output_item.done":
            return result
        opaque = {
            key: copy.deepcopy(getattr(item, key))
            for key in _OPAQUE_FIELDS if getattr(item, key, None) is not None
        }
        if not opaque:
            return result
        index = state.indices.get(identity, current_index)
        if generation is None:
            # Standard v1 output translates a state-only block to a separate
            # nonstandard item. Keep its state only in metadata in that mode.
            content = [] if kwargs.get("output_version") == "v1" else [{
                "type": "function_call", "index": index, **opaque,
            }]
            generation = ChatGenerationChunk(message=AIMessageChunk(
                content=content, additional_kwargs={_STATE_KEY: {item.call_id: opaque}},
            ))
        else:
            # Avoid duplicating fields if a later upstream converter starts
            # preserving them. Copy before extending shared content objects.
            generation = copy.deepcopy(generation)
            for block in generation.message.content:
                if isinstance(block, dict) and block.get("type") == "function_call":
                    for key, value in opaque.items():
                        block.setdefault(key, value)
                    break
            else:
                generation.message.content.append({"type": "function_call", "index": index, **opaque})
            generation.message.additional_kwargs = {
                **generation.message.additional_kwargs,
                _STATE_KEY: {**generation.message.additional_kwargs.get(_STATE_KEY, {}), item.call_id: opaque},
            }
        return current_index, output_index, sub_index, generation

    convert.__manta_opaque_state__ = True  # type: ignore[attr-defined]
    base._convert_responses_chunk_to_generation_chunk = convert


def preserve_sync_stream(iterator: Iterator[ChatGenerationChunk]) -> Iterator[ChatGenerationChunk]:
    """Activate compatibility only while advancing this Databricks stream."""
    install_stream_converter()
    state = _StreamState()
    try:
        while True:
            token = _ACTIVE_STREAM.set(state)
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            finally:
                _ACTIVE_STREAM.reset(token)
            yield chunk
    finally:
        if close := getattr(iterator, "close", None):
            close()


async def preserve_async_stream(iterator: AsyncIterator[ChatGenerationChunk]) -> AsyncIterator[ChatGenerationChunk]:
    install_stream_converter()
    state = _StreamState()
    try:
        while True:
            token = _ACTIVE_STREAM.set(state)
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                _ACTIVE_STREAM.reset(token)
            yield chunk
    finally:
        if close := getattr(iterator, "aclose", None):
            await close()
