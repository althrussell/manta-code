"""Preserve opaque provider signatures on unified Chat tool conversations.

Unity's Gemini Chat format puts thoughtSignature on tool calls and text blocks.
LangChain normalizes tool calls into its common schema and drops that extension
on replay. Keep the opaque values in checkpointable metadata, and reattach them
by call identity/content index only for the same governed Chat conversation.
"""
from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

_FIELDS = ("thoughtSignature", "thought_signature", "encrypted_content", "extra_content")
_TOOLS_KEY = "manta_chat_tool_state"
_CONTENT_KEY = "manta_chat_content_state"


@dataclass
class _StreamState:
    tool_ids: dict[tuple[int, int], str] = field(default_factory=dict)
    tool_fields: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)


_ACTIVE_STREAM: ContextVar[_StreamState | None] = ContextVar("manta_chat_opaque_stream", default=None)


def _opaque(block: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(block[key]) for key in _FIELDS if key in block}


def _prepare_stream_tool_state(delta: dict[str, Any], choice_index: int) -> dict[str, Any]:
    """Resolve fragmented identities and capture complete opaque fields once."""
    state = _ACTIVE_STREAM.get()
    calls = delta.get("tool_calls")
    if state is None or not isinstance(calls, list):
        return delta
    resolved = []
    changed = False
    for call in calls:
        if isinstance(call, dict):
            index = call.get("index")
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                key = (choice_index, index)
                identity = call.get("id")
                if isinstance(identity, str) and identity:
                    state.tool_ids[key] = identity
                elif not identity and _opaque(call) and key in state.tool_ids:
                    call = {**call, "id": state.tool_ids[key]}
                    changed = True
            if isinstance(call.get("id"), str) and (fields := _opaque(call)):
                seen = state.tool_fields.setdefault((choice_index, call["id"]), {})
                # Opaque fields are complete values, not text tokens. Emitting
                # the same value again makes LangChain concatenate its strings.
                duplicates = {key for key, value in fields.items() if key in seen and seen[key] == value}
                seen.update(fields)
                if duplicates:
                    call = {key: value for key, value in call.items() if key not in duplicates}
                    changed = True
        resolved.append(call)
    return {**delta, "tool_calls": resolved} if changed else delta


def prepare_chat_stream_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Let upstream parse opaque-only deltas without assuming a function body."""
    choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
    if not any(
        isinstance(call, dict) and call.get("function") is None and _opaque(call)
        for choice in choices for call in (choice.get("delta") or {}).get("tool_calls") or []
    ):
        return chunk
    result = copy.deepcopy(chunk)
    choices = result.get("choices") or result.get("chunk", {}).get("choices") or []
    for choice in choices:
        for call in (choice.get("delta") or {}).get("tool_calls") or []:
            if isinstance(call, dict) and call.get("function") is None and _opaque(call):
                call["function"] = {}
    return result


def _capture_wire_state(message: BaseMessage, wire: dict[str, Any]) -> BaseMessage:
    """Capture extensions before the upstream common tool-call parser drops them."""
    if not isinstance(message, AIMessage):
        return message
    tools = copy.deepcopy(message.additional_kwargs.get(_TOOLS_KEY, {}))
    for call in wire.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id") and (state := _opaque(call)):
            tools[call["id"]] = {**tools.get(call["id"], {}), **state}
    content = copy.deepcopy(message.additional_kwargs.get(_CONTENT_KEY, {}))
    blocks = wire.get("content")
    if isinstance(blocks, list):
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("type") in {"text", "reasoning"} and (state := _opaque(block)):
                content[str(index)] = {**content.get(str(index), {}), **state}
    if not tools and not content:
        return message
    additional = {**message.additional_kwargs}
    if tools:
        additional[_TOOLS_KEY] = tools
    if content:
        additional[_CONTENT_KEY] = content
    return message.model_copy(update={"additional_kwargs": additional})


def capture_chat_result_state(result: ChatResult, response: Any) -> ChatResult:
    """Retain opaque Chat extensions from dict and SDK model responses."""
    wire = response if isinstance(response, dict) else response.model_dump(
        exclude={"choices": {"__all__": {"message": {"parsed"}}}}, warnings=False,
    )
    for generation, choice in zip(result.generations, wire.get("choices") or [], strict=True):
        generation.message = _capture_wire_state(generation.message, choice.get("message") or {})
    return result


def capture_chat_chunk_state(
    result: ChatGenerationChunk | None, chunk: dict[str, Any],
) -> ChatGenerationChunk | None:
    """Retain stream delta extensions before LangChain normalizes tool calls."""
    if result is None:
        return None
    choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
    if not choices:
        return result
    choice = choices[0]
    delta = _prepare_stream_tool_state(choice.get("delta") or {}, choice.get("index", 0))
    message = _capture_wire_state(result.message, delta)
    if message is result.message:
        return result
    return ChatGenerationChunk(message=message, generation_info=result.generation_info)


def preserve_sync_chat_stream(iterator: Iterator[ChatGenerationChunk]) -> Iterator[ChatGenerationChunk]:
    """Keep tool-index identities local to one stream, including nested streams."""
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


async def preserve_async_chat_stream(
    iterator: AsyncIterator[ChatGenerationChunk],
) -> AsyncIterator[ChatGenerationChunk]:
    """Async counterpart; reset context before yielding to callers or callbacks."""
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


def capture_chat_state(message: BaseMessage) -> BaseMessage:
    if not isinstance(message, AIMessage):
        return message
    tools = copy.deepcopy(message.additional_kwargs.get(_TOOLS_KEY, {}))
    for call in message.additional_kwargs.get("tool_calls", []):
        if isinstance(call, dict) and call.get("id") and (state := _opaque(call)):
            tools[call["id"]] = state
    content = copy.deepcopy(message.additional_kwargs.get(_CONTENT_KEY, {}))
    if isinstance(message.content, list):
        for index, block in enumerate(message.content):
            if isinstance(block, dict) and block.get("type") in {"text", "reasoning"} and (state := _opaque(block)):
                content[str(index)] = state
    if not tools and not content:
        return message
    additional = {**message.additional_kwargs}
    if tools:
        additional[_TOOLS_KEY] = tools
    if content:
        additional[_CONTENT_KEY] = content
    return message.model_copy(update={"additional_kwargs": additional})


def restore_chat_state(payload: dict[str, Any], messages: Sequence[BaseMessage]) -> dict[str, Any]:
    if "messages" not in payload:
        return payload
    result = payload
    for index, (source, _) in enumerate(zip(messages, payload["messages"], strict=True)):
        if not isinstance(source, AIMessage):
            continue
        captured = capture_chat_state(source)
        tool_states = captured.additional_kwargs.get(_TOOLS_KEY, {})
        content_states = captured.additional_kwargs.get(_CONTENT_KEY, {})
        if not tool_states and not content_states:
            continue
        if result is payload:
            result = copy.deepcopy(payload)
        output = result["messages"][index]
        for call in output.get("tool_calls", []):
            if state := tool_states.get(call.get("id")):
                call.update(copy.deepcopy(state))
        content = output.get("content")
        if isinstance(content, list):
            for content_index, block in enumerate(content):
                if isinstance(block, dict) and (state := content_states.get(str(content_index))):
                    block.update(copy.deepcopy(state))
    return result
