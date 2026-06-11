"""A ``ChatDatabricks`` that unpacks reasoning-model content blocks.

Some Databricks serving endpoints (e.g. the Qwen "thinking" models) return the
assistant turn as a *list* of OpenAI-style content blocks — a ``reasoning``
block (the model's private chain-of-thought) followed by a ``text`` block (the
visible answer)::

    [
      {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]},
      {"type": "text", "text": "Hi! How can I help you today?"}
    ]

``databricks-langchain`` does not understand that shape on the chat-completions
path: :func:`databricks_langchain.chat_models._convert_dict_to_message` (and its
streaming sibling) ``json.dumps`` any non-string content into a string to keep
output parsers happy. The agent — and the ``deepagents-code`` TUI, which renders
``message.content_blocks`` — then sees one text block whose text is the raw JSON
array, so the user sees ``[{"type": "reasoning", ...}]`` instead of the answer.

This subclass post-processes every assistant message (streamed and
non-streamed): it parses that serialized block list, drops the private
``reasoning`` blocks, and keeps only the visible ``text``. Tool calls, usage
metadata, ids, and ordinary string content all pass through untouched. It is
wired in as the Databricks provider's ``class_path`` (see
:data:`manta_code.dcode.DATABRICKS_CLASS_PATH`) so it runs inside the LangGraph
server subprocess where the model actually executes — fixing the agent's own
message history, not merely the display.

Importing this module also installs a small resolver shim (see
:func:`_install_subagent_databricks_resolver`) so Manta's per-subagent
``databricks:<endpoint>`` model pins resolve to ``MantaChatDatabricks`` too —
deepagents resolves subagent models through langchain's ``init_chat_model``,
which has no ``databricks`` provider, so without this they fail with
"Unable to infer model provider". Because the server subprocess imports this
module to build the main agent's model (the provider ``class_path``), the shim
is in place before any subagent is resolved.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from databricks_langchain import ChatDatabricks
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

#: Content-block ``type`` values this shim recognizes. A serialized list is only
#: treated as reasoning-model content when *every* element is one of these, so
#: a model that legitimately answers with some other JSON array is left alone.
_KNOWN_BLOCK_TYPES = frozenset({"text", "reasoning"})


def _coerce_block_list(content: object) -> list[dict[str, Any]] | None:
    """Return ``content`` as a content-block list, or ``None`` if it isn't one.

    Accepts either an already-parsed list or the ``json.dumps`` string that
    ``databricks-langchain`` produces. Returns ``None`` (caller leaves content
    untouched) unless the value is a non-empty list whose every element is a
    dict with a recognized block ``type`` — a deliberately strict check so
    ordinary string answers, or a model emitting unrelated JSON, are not
    mangled.
    """
    if isinstance(content, list):
        blocks = content
    elif isinstance(content, str):
        stripped = content.strip()
        if not stripped.startswith("[") or '"type"' not in stripped:
            return None
        try:
            blocks = json.loads(stripped)
        except (ValueError, TypeError):
            return None
    else:
        return None

    if not isinstance(blocks, list) or not blocks:
        return None
    if not all(
        isinstance(block, dict) and block.get("type") in _KNOWN_BLOCK_TYPES
        for block in blocks
    ):
        return None
    return blocks


def _visible_text(content: object) -> str | None:
    """Extract the user-visible text from reasoning/text content blocks.

    Returns ``None`` when ``content`` is not a recognized block list (so the
    caller leaves the message unchanged); otherwise returns the concatenated
    text of the ``text`` blocks, discarding ``reasoning`` blocks (the model's
    private chain-of-thought, which the TUI never renders).
    """
    blocks = _coerce_block_list(content)
    if blocks is None:
        return None
    return "".join(
        str(block.get("text", "")) for block in blocks if block.get("type") == "text"
    )


def _normalize_message(message: BaseMessage) -> BaseMessage:
    """Return ``message`` with serialized reasoning content reduced to its text.

    A no-op (returns the same instance) for ordinary content. When rewriting,
    ``model_copy`` preserves tool calls, usage metadata, and ids — only
    ``content`` changes.
    """
    text = _visible_text(message.content)
    if text is None:
        return message
    return message.model_copy(update={"content": text})


def _normalize_chat_result(result: ChatResult) -> ChatResult:
    for generation in result.generations:
        generation.message = _normalize_message(generation.message)
    return result


def _normalize_chunk(chunk: ChatGenerationChunk) -> ChatGenerationChunk:
    message = _normalize_message(chunk.message)
    if message is chunk.message:
        return chunk
    return ChatGenerationChunk(
        message=message,  # type: ignore[arg-type]
        generation_info=chunk.generation_info,
    )


def _usage_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Convert one chunk's *cumulative* usage into a delta against ``previous``.

    ``databricks-langchain`` attaches the cumulative usage totals to **every**
    streamed chunk, but langchain's chunk merge *adds* ``usage_metadata``
    across chunks — so a 15-chunk stream records ~15x the real input tokens
    (and a near-quadratic output count) on the merged message. That poisoned
    Manta's ledger, budget governor, and advice signals (ADR 0010 Phase C).

    Emitting per-chunk deltas makes the additive merge come out exactly equal
    to the provider's final cumulative totals. Handles nested int dicts (e.g.
    ``input_token_details``) recursively; negative deltas clamp to 0 (a
    provider restating a lower total is noise, not a refund).
    """
    delta: dict[str, Any] = {}
    for key, value in current.items():
        prev = previous.get(key)
        if isinstance(value, int):
            delta[key] = max(0, value - (prev if isinstance(prev, int) else 0))
        elif isinstance(value, dict):
            delta[key] = _usage_delta(prev if isinstance(prev, dict) else {}, value)
        else:
            delta[key] = value
    return delta


class _StreamUsageDeduplicator:
    """Rewrite cumulative per-chunk usage as deltas across one stream."""

    def __init__(self) -> None:
        self._cumulative: dict[str, Any] = {}

    def rewrite(self, chunk: ChatGenerationChunk) -> ChatGenerationChunk:
        usage = getattr(chunk.message, "usage_metadata", None)
        if not isinstance(usage, dict) or not usage:
            return chunk
        delta = _usage_delta(self._cumulative, usage)
        self._cumulative = usage
        message = chunk.message.model_copy(update={"usage_metadata": delta})
        return ChatGenerationChunk(
            message=message,  # type: ignore[arg-type]
            generation_info=chunk.generation_info,
        )


class MantaChatDatabricks(ChatDatabricks):
    """``ChatDatabricks`` that strips reasoning blocks from assistant turns.

    Overrides only the four generation entry points to normalize content after
    delegating to the upstream implementation; everything else (auth, request
    shaping, tool calling, streaming transport) is inherited unchanged.
    """

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        return _normalize_chat_result(super()._generate(*args, **kwargs))

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        return _normalize_chat_result(await super()._agenerate(*args, **kwargs))

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        dedup = _StreamUsageDeduplicator()
        for chunk in super()._stream(*args, **kwargs):
            yield dedup.rewrite(_normalize_chunk(chunk))

    async def _astream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        dedup = _StreamUsageDeduplicator()
        async for chunk in super()._astream(*args, **kwargs):
            yield dedup.rewrite(_normalize_chunk(chunk))


#: Guards :func:`_install_subagent_databricks_resolver` against re-patching.
_resolver_installed = False


def _install_subagent_databricks_resolver() -> bool:
    """Route subagent model specs for Manta-registered providers through Manta.

    deepagents resolves a subagent's ``model`` string with
    ``deepagents._models.resolve_model`` -> langchain ``init_chat_model``, which
    has no ``databricks`` provider; a markdown subagent pinned to
    ``databricks:<endpoint>`` (Manta's planning/swe/review agents) would raise
    "Unable to infer model provider". The main agent avoids this because
    deepagents-code's ``create_model`` honors the provider ``class_path``.

    This wraps ``resolve_model`` so any spec whose provider is registered in
    :mod:`manta_code.providers` (databricks today; the AI Gateway provider in
    Phase D) resolves through Manta's registry — mirroring how deepagents-code's
    ``_create_model_from_class`` builds the main agent (``cls(model=endpoint)``)
    — while every other spec (``anthropic:…``, ``openai:…``, bare names) defers
    to the original resolver, which handles them natively.

    Patching is applied in **two** places because some deepagents modules bind
    the function by name at import time. ``deepagents.graph`` does a top-level
    ``from deepagents._models import resolve_model`` and is imported *before*
    this shim runs (via ``create_cli_agent``'s import chain), so rebinding only
    the ``_models`` attribute would miss it. We therefore (1) patch
    ``_models.resolve_model`` — picked up by the function-local importers
    (``middleware.subagents``/``summarization``/``rubric``) and any module
    imported later — and (2) rebind the name on every already-imported
    ``deepagents`` module that still points at the original. Idempotent, and a
    no-op when deepagents is unavailable (returns ``False``).
    """
    global _resolver_installed
    if _resolver_installed:
        return True
    try:
        from deepagents import _models
    except Exception:
        return False

    from . import providers

    original_resolve_model = _models.resolve_model

    def resolve_model(model: Any) -> Any:
        if isinstance(model, str):
            ref = providers.parse_model_ref(model)
            if ref is not None and providers.resolver_for(ref.provider) is not None:
                resolved = providers.resolve_model_ref(model, fallback=False)
                if resolved is not None:
                    return resolved
        return original_resolve_model(model)

    _models.resolve_model = resolve_model
    _rebind_imported_resolvers(original_resolve_model, resolve_model)
    _resolver_installed = True
    return True


def _rebind_imported_resolvers(original: Any, replacement: Any) -> None:
    """Rebind ``resolve_model`` on deepagents modules that imported it by name.

    A module-level ``from deepagents._models import resolve_model`` (as in
    ``deepagents.graph``) binds the original function into that module's
    namespace, so patching ``_models.resolve_model`` alone does not reach it.
    This swaps any such binding that still references ``original`` for
    ``replacement``.
    """
    import sys

    for module in list(sys.modules.values()):
        if module is None:
            continue
        if not getattr(module, "__name__", "").startswith("deepagents"):
            continue
        if getattr(module, "resolve_model", None) is original:
            module.resolve_model = replacement


_install_subagent_databricks_resolver()

# Install Manta's build hook here too: this module is imported by
# ``create_model`` (via the provider ``class_path``) in the langgraph server
# subprocess, which runs *before* ``create_cli_agent`` builds the graph. That
# makes this the reliable seam to wrap ``create_deep_agent`` so Manta's compiled
# agents / middleware / store / tools are injected. Best-effort and idempotent
# (see :func:`manta_code.hook.install_build_hook`); a failure here never blocks
# the resolver shim above or the launch.
try:  # pragma: no cover - exercised via the live server subprocess
    from .hook import install_build_hook as _install_build_hook

    _install_build_hook()
except Exception:  # noqa: BLE001
    pass
