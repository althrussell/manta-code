"""Databricks unified authentication with the official OpenAI LangChain adapter.

Clients are initialized lazily and refresh their authentication headers through
the Databricks SDK on each request. Endpoint names keep the legacy serving
route; three-part service names select Unity Gateway's unified API. The shared
target policy selects Responses from verified metadata and exact compatibility
evidence, with explicit overrides. The OpenAI adapter owns tool/Responses
serialization; Manta retains its legacy serialized-content and cumulative-usage
repairs without flattening structured Responses continuation state.

Importing this stable provider class path also installs Manta's Deep Agents
resolver and agent build hook in the server subprocess.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field, PrivateAttr, model_validator
from .providers.targets import (
    TargetPolicy,
    TargetResolutionError,
    merge_request_tag_headers,
    resolve_target_policy,
    validate_request_tags,
)
from .providers.response_compat import (
    capture_response_state,
    preserve_async_stream,
    preserve_sync_stream,
    restore_responses_state,
)
from .providers.chat_compat import capture_chat_state, restore_chat_state

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
    message = capture_chat_state(capture_response_state(message))
    # Parsed Responses blocks carry ids and opaque continuation state. Only
    # repair the legacy JSON-in-a-string representation; never flatten them.
    if not isinstance(message.content, str):
        return message
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


class _LazyClient:
    """Defer workspace authentication until the first actual model request."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self._factory(), name)


class _WorkspaceAuth(httpx.Auth):
    """Refresh every SDK header, including Azure auxiliary credentials."""

    def __init__(self, headers: Any) -> None:
        self._headers = headers

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers.update(self._headers())
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncIterator[httpx.Request]:
        request.headers.update(await asyncio.to_thread(self._headers))
        yield request


class _BorrowedClient(httpx.Client):
    """Apply request auth without modifying or taking ownership of a client."""

    def __init__(self, client: httpx.Client, auth: httpx.Auth) -> None:
        super().__init__(timeout=client.timeout)
        self._borrowed = client
        self._workspace_auth = auth

    def build_request(self, *args: Any, **kwargs: Any) -> httpx.Request:
        return self._borrowed.build_request(*args, **kwargs)

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if self.is_closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return self._borrowed.send(request, **{**kwargs, "auth": self._workspace_auth})


class _BorrowedAsyncClient(httpx.AsyncClient):
    """Async counterpart of :class:`_BorrowedClient`."""

    def __init__(self, client: httpx.AsyncClient, auth: httpx.Auth) -> None:
        super().__init__(timeout=client.timeout)
        self._borrowed = client
        self._workspace_auth = auth

    def build_request(self, *args: Any, **kwargs: Any) -> httpx.Request:
        return self._borrowed.build_request(*args, **kwargs)

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if self.is_closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return await self._borrowed.send(
            request, **{**kwargs, "auth": self._workspace_auth}
        )


@cache
def _openai_request_parameters() -> frozenset[str]:
    """Use the installed SDK's public signatures instead of a stale allowlist."""
    from openai.resources.chat.completions import Completions
    from openai.resources.responses import Responses

    return frozenset(
        name
        for create in (Completions.create, Responses.create)
        for name in inspect.signature(create).parameters
        if name != "self"
    )


@cache
def _dialect_request_parameters(api: str) -> frozenset[str]:
    from openai.resources.chat.completions import Completions
    from openai.resources.responses import Responses

    resource = Responses if api == "responses" else Completions
    return frozenset(
        name for method in (resource.create, resource.parse)
        for name in inspect.signature(method).parameters if name != "self"
    )


def _merge_extra_params(kwargs: dict[str, Any], extra: dict[str, Any]) -> None:
    """Keep the old Databricks extra_params seam with OpenAI's typed methods.

    Constructor fields belong on the model, SDK request options belong in
    model_kwargs, and Databricks extensions belong in extra_body. Explicit
    options override the legacy defaults, including aliases such as max_tokens.
    """
    spellings: dict[str, set[str]] = {}
    for name, field in ChatOpenAI.model_fields.items():
        names = {name, field.alias} if field.alias else {name}
        for spelling in names:
            spellings[spelling] = names
    model_kwargs = dict(kwargs.get("model_kwargs") or {})
    body = {}
    for key, value in extra.items():
        if key in spellings:
            if not spellings[key].intersection(kwargs.keys() | model_kwargs.keys()):
                kwargs[key] = value
        elif key in _openai_request_parameters():
            if key in kwargs:
                model_kwargs[key] = kwargs.pop(key)
            else:
                model_kwargs.setdefault(key, value)
        else:
            # These vendor options were accepted by ChatDatabricks directly;
            # OpenAI's typed create methods require them in extra_body.
            body[key] = kwargs.pop(key, model_kwargs.pop(key, value))
    if model_kwargs or "model_kwargs" in kwargs:
        kwargs["model_kwargs"] = model_kwargs
    if body:
        kwargs["extra_body"] = {**body, **(kwargs.get("extra_body") or {})}


def _constructor_policy_layers(options: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Identify explicit upstream call options, including the first import.

    Deep Agents 0.1.86 passes merged provider defaults and runtime overrides
    through one kwargs dict. Its pinned create_model(extra_kwargs=...) frame
    retains the exact provenance; inspect only these immediate guarded callers.
    This avoids treating provider defaults as explicit per-call overrides.
    Direct callers continue to own every option they supply.
    """
    frame = inspect.currentframe()
    try:
        constructor = frame.f_back if frame else None
        class_factory = constructor.f_back if constructor else None
        creator = class_factory.f_back if class_factory else None
        if (
            class_factory is not None and creator is not None
            and class_factory.f_globals.get("__name__") == "deepagents_code.config"
            and class_factory.f_code.co_name == "_create_model_from_class"
            and creator.f_globals.get("__name__") == "deepagents_code.config"
            and creator.f_code.co_name == "create_model"
            and "extra_kwargs" in creator.f_locals
        ):
            explicit_keys = set(creator.f_locals["extra_kwargs"] or {}) | {"model", "model_name", "endpoint"}
            return ({k: v for k, v in options.items() if k in explicit_keys}, {k: v for k, v in options.items() if k not in explicit_keys})
        return options, {}
    finally:
        del frame


class MantaChatDatabricks(ChatOpenAI):
    """OpenAI-compatible Databricks model, with refreshable SDK credentials.

    Keep this class path stable: the launcher and Deep Agents resolver import
    it in the server process to install Manta's control-plane hooks. Clients
    stay lazy so listing models does not require working workspace credentials.
    """

    workspace_client: Any = Field(default=None, exclude=True)
    use_ai_gateway: bool | None = None
    use_ai_gateway_native_api: bool = False
    _sync_root: Any = PrivateAttr(default=None)
    _async_root: Any = PrivateAttr(default=None)
    _target_policy: Any = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def validate_temperature(cls, values: dict[str, Any]) -> Any:
        # Endpoint/service names are opaque Databricks identities, even when a
        # user names a custom endpoint gpt-5-*. Do not apply OpenAI family
        # heuristics that silently delete an explicitly configured parameter.
        return values

    def __init__(self, **kwargs: Any) -> None:
        if "endpoint" in kwargs:
            kwargs.setdefault("model", kwargs.pop("endpoint"))
        extra = kwargs.pop("extra_params", None)
        if extra:
            _merge_extra_params(kwargs, extra)
        explicit, defaults = _constructor_policy_layers(kwargs)
        policy = resolve_target_policy(kwargs.get("model", kwargs.get("model_name", "")), explicit=explicit, constructor_defaults=defaults)
        kwargs = policy.model_kwargs()
        if policy.api == "responses" and kwargs.get("output_version") == "v0":
            raise TargetResolutionError("Databricks Responses requires output_version='v1' or 'responses/v1' to preserve opaque continuation state; v0 is unsupported.")
        if policy.surface == "unified" and policy.api == "responses":
            if kwargs.get("store") is True or kwargs.get("use_previous_response_id") or kwargs.get("previous_response_id") or (kwargs.get("model_kwargs") or {}).get("previous_response_id"):
                raise TargetResolutionError("Unified Databricks Responses is stateless: store=True and previous_response_id are unsupported. Replay the complete conversation history instead.")
            kwargs["store"] = False
        # Never fall back to an unrelated OPENAI_API_KEY or OpenAI endpoint.
        kwargs["api_key"] = "databricks-sdk-auth"
        kwargs.setdefault("use_responses_api", False)
        stream_usage = policy.capabilities.values.get("stream_usage", True) if policy.api == "chat" else True
        if stream_usage is False and kwargs.get("stream_usage") is True:
            raise TargetResolutionError(f"{policy.target.name} does not support Chat stream_options (verified Gateway behavior); remove stream_usage=True or correct the explicit capability profile.")
        kwargs.setdefault("stream_usage", stream_usage)
        kwargs.setdefault("include_response_headers", True)
        kwargs.setdefault("client", _LazyClient(lambda: self._root(False).chat.completions))
        kwargs.setdefault("async_client", _LazyClient(lambda: self._root(True).chat.completions))
        kwargs.setdefault("root_client", _LazyClient(lambda: self._root(False)))
        kwargs.setdefault("root_async_client", _LazyClient(lambda: self._root(True)))
        super().__init__(**kwargs)
        self._target_policy = policy
        if "base_url" not in kwargs and "openai_api_base" not in kwargs:
            # ChatOpenAI reads OPENAI_API_BASE. It does not select Databricks'
            # workspace and must not affect this provider's destination.
            self.openai_api_base = None
        if self.use_ai_gateway and self.use_ai_gateway_native_api:
            raise ValueError("Choose either unified or native AI Gateway routing, not both.")

    @property
    def endpoint(self) -> str:
        return self.model_name

    @property
    def target_policy(self) -> TargetPolicy:
        return self._target_policy

    @property
    def transport_surface(self) -> str:
        return self.target_policy.surface

    @property
    def transport_api(self) -> str:
        return self.target_policy.api

    def clone_with_policy(self, *, api: str, surface: str) -> MantaChatDatabricks:
        """Restore a checkpoint's dialect without changing target or credentials."""
        options = dict(self.target_policy.params)
        options.update(
            model=self.model_name, target_kind=self.target_policy.target.kind,
            api=api, surface=surface,
            profile=self.target_policy.capabilities.profile,
            request_tags=self.target_policy.request_tags,
            databricks_profile=self.target_policy.profile,
            workspace_client=self.workspace_client,
        )
        return type(self)(**options)

    def _workspace(self) -> Any:
        if self.workspace_client is None:
            from .auth import resolve_workspace_client

            self.workspace_client = (
                resolve_workspace_client(self.target_policy.profile)
                if self.target_policy.profile else resolve_workspace_client()
            )
        return self.workspace_client

    def _auth_headers(self) -> dict[str, str]:
        headers = self._workspace().config.authenticate()
        if not any(key.lower() == "authorization" and value for key, value in headers.items()):
            raise ValueError("Databricks SDK did not provide authorization headers.")
        return dict(headers)

    def _gateway_enabled(self) -> bool:
        if self.use_ai_gateway is not None:
            return self.use_ai_gateway
        # UC model services use three-part FQNs. Preserve the exact identifier.
        return len(self.model_name.split(".")) == 3

    def _base_url(self) -> str:
        host = self._workspace().config.host.rstrip("/")
        if self.openai_api_base:
            supplied = urlsplit(self.openai_api_base)
            workspace = urlsplit(host)
            if (supplied.scheme, supplied.netloc) != (workspace.scheme, workspace.netloc):
                raise ValueError("Databricks base_url must use the configured workspace origin.")
            expected_path = {
                "legacy": "/serving-endpoints", "unified": "/ai-gateway/mlflow/v1",
                "openai": "/ai-gateway/openai/v1",
            }[self.transport_surface]
            if supplied.path.rstrip("/") != expected_path or supplied.query or supplied.fragment:
                raise TargetResolutionError(f"Databricks base_url contradicts selected surface {self.transport_surface!r}; expected workspace route {expected_path}.")
            return self.openai_api_base
        if self.use_ai_gateway_native_api:
            return f"{host}/ai-gateway/openai/v1"
        if self._gateway_enabled():
            return f"{host}/ai-gateway/mlflow/v1"
        return f"{host}/serving-endpoints"

    def _root(self, asynchronous: bool) -> Any:
        attr = "_async_root" if asynchronous else "_sync_root"
        root = getattr(self, attr)
        if root is None:
            import openai

            base_url = self._base_url()
            auth = _WorkspaceAuth(self._auth_headers)
            # OpenAI 3's default client uses HTTPX 2. Keep the documented SDK
            # defaults while using HTTPX 0.28's public Auth/client contract.
            timeout = openai.DEFAULT_TIMEOUT
            limits = openai.DEFAULT_CONNECTION_LIMITS
            http_options = {
                "auth": auth,
                "timeout": httpx.Timeout(
                    connect=timeout.connect, read=timeout.read,
                    write=timeout.write, pool=timeout.pool,
                ),
                "limits": httpx.Limits(
                    max_connections=limits.max_connections,
                    max_keepalive_connections=limits.max_keepalive_connections,
                    keepalive_expiry=limits.keepalive_expiry,
                ),
                "follow_redirects": True,
            }
            if asynchronous:
                http_client = (
                    _BorrowedAsyncClient(self.http_async_client, auth)
                    if self.http_async_client is not None
                    else httpx.AsyncClient(**http_options)
                )
            else:
                http_client = (
                    _BorrowedClient(self.http_client, auth)
                    if self.http_client is not None
                    else httpx.Client(**http_options)
                )
            options: dict[str, Any] = {
                "base_url": base_url,
                "api_key": "databricks-sdk-auth",
                "http_client": http_client,
            }
            for key, value in (
                ("timeout", self.request_timeout),
                ("max_retries", self.max_retries),
                ("default_headers", self.default_headers),
                ("default_query", self.default_query),
            ):
                if value is not None:
                    options[key] = value
            cls = openai.AsyncOpenAI if asynchronous else openai.OpenAI
            root = cls(**options)
            setattr(self, attr, root)
        return root

    def _get_ls_params(self, *args: Any, **kwargs: Any) -> Any:
        return {**super()._get_ls_params(*args, **kwargs), "ls_provider": "databricks"}

    @property
    def _llm_type(self) -> str:
        return "chat-databricks"

    def _validate_forced_tool_choice(self, choice: Any) -> None:
        unsupported = self.target_policy.capabilities.values.get("forced_tool_choice") is False
        if self.use_responses_api and unsupported and choice not in (None, False, "auto", "none"):
            raise TargetResolutionError(
                f"{self.model_name} does not support forced tool choice on Responses (verified service behavior). Use tool_choice='auto' for its supported tool-calling loop."
            )

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        self._validate_forced_tool_choice(tool_choice)
        return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)

    def _validate_known_capabilities(self, payload: dict[str, Any]) -> None:
        values = self.target_policy.capabilities.values
        if not self.use_responses_api and values.get("stream_usage") is False and payload.get("stream_options") is not None:
            raise TargetResolutionError(f"{self.model_name} does not support Chat stream_options (verified Gateway behavior); remove stream_options/stream_usage or correct the explicit capability profile.")
        if values.get("temperature") is False and payload.get("temperature") is not None:
            raise TargetResolutionError(f"{self.model_name} has temperature disabled in its capability profile; remove temperature or correct the explicit profile override.")
        reasoning = payload.get("reasoning") or {}
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        effort = effort or payload.get("reasoning_effort")
        levels = values.get("reasoning_effort_levels")
        if effort is not None and levels is not None and effort not in levels:
            raise TargetResolutionError(f"{self.model_name} supports reasoning effort values {levels}; requested {effort!r}. Update the option or its explicit capability profile.")
        if payload.get("tools") and values.get("tool_calling") is False:
            raise TargetResolutionError(f"{self.model_name} has tool calling disabled in its capability profile; select a tool-capable service or correct the profile override.")
        cap = payload.get("max_output_tokens", payload.get("max_completion_tokens", payload.get("max_tokens")))
        maximum = values.get("max_output_tokens")
        if isinstance(cap, int) and isinstance(maximum, int) and cap > maximum:
            raise TargetResolutionError(f"Requested output cap {cap} exceeds {self.model_name}'s verified/profile limit {maximum}; lower the cap or correct its profile override.")

    def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        transport_options = {"model", "model_name", "endpoint", "api", "surface", "use_responses_api", "use_ai_gateway", "use_ai_gateway_native_api", "target_kind", "databricks_profile"}.intersection(kwargs)
        if transport_options:
            raise TargetResolutionError(
                f"Transport options {', '.join(sorted(transport_options))} belong on the Databricks model constructor. Rebuild the model or use clone_with_policy(api=..., surface=...) before invoking it."
            )
        limits = {key: kwargs.pop(key) for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in kwargs}
        if limits:
            cap = next(iter(limits.values()))
            if any(value != cap for value in limits.values()):
                raise TargetResolutionError("Conflicting per-call max_tokens and max_output_tokens limits.")
            # The default constructor payload uses max_completion_tokens;
            # normalizing before merge prevents that default replacing a
            # higher-priority max_tokens alias during Responses conversion.
            kwargs["max_completion_tokens"] = cap
        if self.use_responses_api and (kwargs.get("stop") or getattr(self, "stop", None)):
            raise TargetResolutionError("The Responses API does not support stop sequences; remove stop or select a service supporting api='chat'.")
        if self.transport_surface == "unified" and self.use_responses_api:
            if kwargs.get("store") is True or kwargs.get("previous_response_id"):
                raise TargetResolutionError("Unified Databricks Responses is stateless; send complete history without store=True or previous_response_id.")
            kwargs["store"] = False
        call_tags = kwargs.pop("request_tags", None)
        if "extra_headers" in kwargs or call_tags is not None:
            base_tags = json.loads(merge_request_tag_headers(self.default_headers, self.target_policy.request_tags).get("Databricks-Ai-Gateway-Request-Tags", "{}"))
            kwargs["extra_headers"] = merge_request_tag_headers(
                kwargs.get("extra_headers"), {**base_tags, **validate_request_tags(call_tags or {})},
            )
        custom_inputs = kwargs.pop("custom_inputs", None)
        if custom_inputs is not None:
            kwargs["extra_body"] = {
                **(self.extra_body or {}), **(kwargs.get("extra_body") or {}),
                "custom_inputs": custom_inputs,
            }
        payload = super()._get_request_payload(*args, **kwargs)
        if "messages" in payload:
            payload = restore_chat_state(payload, self._convert_input(args[0] if args else kwargs["input_"]).to_messages())
        self._validate_forced_tool_choice(payload.get("tool_choice"))
        if "input" in payload:
            temperature = kwargs.get("temperature", self.temperature)
            if temperature is not None:
                payload["temperature"] = temperature
            payload = restore_responses_state(
                payload, self._convert_input(args[0] if args else kwargs["input_"]).to_messages(),
            )
            if "stop" in payload and payload["stop"]:
                raise TargetResolutionError("The Responses API does not support stop sequences; remove stop or select a service supporting api='chat'.")
            if "parallel_tool_calls" in payload and not isinstance(payload["parallel_tool_calls"], bool):
                raise TargetResolutionError("parallel_tool_calls must be a boolean.")
        if "messages" in payload and not self.use_ai_gateway_native_api:
            # FMAPI's chat dialect retains max_tokens and does not accept the
            # strict tool flag for non-GPT models. Keep those existing quirks.
            if "max_completion_tokens" in payload:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
            if "gpt" not in self.model_name.lower():
                tools = copy.deepcopy(payload.get("tools", []))
                if "tools" in payload:
                    payload["tools"] = tools
                for tool in tools:
                    if isinstance(tool.get("function"), dict):
                        tool["function"].pop("strict", None)
            if "claude" in self.model_name.lower():
                for message in payload["messages"]:
                    if (
                        message.get("role") == "assistant"
                        and message.get("tool_calls")
                        and not message.get("content")
                    ):
                        message["content"] = " "
        unknown = payload.keys() - _dialect_request_parameters(self.transport_api)
        if unknown:
            raise TargetResolutionError(
                f"Options {', '.join(sorted(unknown))} are unsupported by the {self.transport_api} transport. Remove them, select a compatible API, or put documented Databricks extensions in extra_body."
            )
        self._validate_known_capabilities(payload)
        return payload

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        return _normalize_chat_result(super()._generate(*args, **kwargs))

    def _create_chat_result(self, response: Any, generation_info: Any = None) -> ChatResult:
        from .providers.chat_compat import capture_chat_result_state
        return capture_chat_result_state(super()._create_chat_result(response, generation_info), response)

    def _convert_chunk_to_generation_chunk(self, chunk: Any, default_chunk_class: Any, base_generation_info: Any = None) -> Any:
        from .providers.chat_compat import capture_chat_chunk_state
        return capture_chat_chunk_state(
            super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info), chunk,
        )

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        # First SDK/client construction may consult CLI or cloud credentials.
        # Keep it off the event loop, just like per-request credential refresh.
        await asyncio.to_thread(self._root, True)
        return _normalize_chat_result(await super()._agenerate(*args, **kwargs))

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        dedup = (
            _StreamUsageDeduplicator()
            if not self._gateway_enabled() and not self.use_ai_gateway_native_api
            else None
        )
        for chunk in preserve_sync_stream(super()._stream(messages, stop=stop, run_manager=None, **kwargs)):
            normalized = _normalize_chunk(chunk)
            normalized = dedup.rewrite(normalized) if dedup is not None else normalized
            if run_manager:
                run_manager.on_llm_new_token(normalized.text, chunk=normalized, logprobs=(normalized.generation_info or {}).get("logprobs"))
            yield normalized

    async def _astream(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        await asyncio.to_thread(self._root, True)
        dedup = (
            _StreamUsageDeduplicator()
            if not self._gateway_enabled() and not self.use_ai_gateway_native_api
            else None
        )
        async for chunk in preserve_async_stream(super()._astream(messages, stop=stop, run_manager=None, **kwargs)):
            normalized = _normalize_chunk(chunk)
            normalized = dedup.rewrite(normalized) if dedup is not None else normalized
            if run_manager:
                await run_manager.on_llm_new_token(normalized.text, chunk=normalized, logprobs=(normalized.generation_info or {}).get("logprobs"))
            yield normalized


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
    except Exception:  # noqa: BLE001 - optional runtime import must not block registration
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
except Exception:  # noqa: BLE001, S110 - best-effort server hook, checked by doctor
    pass
