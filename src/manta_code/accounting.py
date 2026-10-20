"""One request collector for agent models and direct runtime helper calls.

LangChain's public configure hook attaches the recorder to each model request.
Callbacks only enqueue work: SQLite writes run on a dedicated worker, avoiding
blocking I/O on the server event loop. Invocation IDs reconcile callback records
with the middleware fallback without counting a response twice.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import ensure_config

logger = logging.getLogger("manta.accounting")


@dataclass(frozen=True)
class CallScope:
    agent: str = "orchestrator"
    thread_id: str = ""
    model: Any = None
    scaffold_tokens: int = 0
    net_new_tokens: int = 0
    pricing: Any = None
    ledger_path: Any = None
    task: str = ""
    budget_guard: Any = None


_SCOPE: ContextVar[CallScope | None] = ContextVar("manta_call_scope", default=None)


@contextmanager
def accounting_scope(**kwargs: Any) -> Iterator[None]:
    token = _SCOPE.set(CallScope(**kwargs))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _purpose(metadata: dict[str, Any], tags: list[str]) -> str:
    """Use explicit helper metadata where available, retaining unknown roles."""
    values = " ".join(str(metadata.get(k, "")) for k in (
        "manta_call_kind", "lc_source", "langgraph_node", "lc_agent_name", "ls_run_name",
    )) + " " + " ".join(tags)
    for label, terms in (
        ("classifier", ("classifier", "auto_mode")),
        ("summarizer", ("summar", "offload")),
        ("grader", ("rubric", "grader", "criteria")),
    ):
        if any(term in values.lower() for term in terms):
            return label
    return "agent" if _SCOPE.get() else "helper"


class RequestCollector(BaseCallbackHandler):
    # LangChain runs synchronous non-inline handlers in a context-preserving
    # executor for async requests. Budget/ledger I/O never blocks the event loop.
    run_inline = False
    raise_error = True

    def __init__(self) -> None:
        self._contexts: OrderedDict[str, tuple[CallScope, str, float]] = OrderedDict()
        self._chain_purposes: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manta-usage")
        self.last_error: str | None = None
        self.failed_ledgers: dict[str, str] = {}
        self._helper_guards: dict[tuple, Any] = {}

    def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: Any,
                       parent_run_id: Any = None, name: str | None = None,
                       metadata: dict | None = None, tags: list | None = None,
                       **kwargs: Any) -> None:
        # Compiled helper graph names are public callback metadata; their model
        # nodes do not inherit the graph name as lc_agent_name. Retain only the
        # purpose, never chain inputs, prompts or serialized configuration.
        kind = _purpose({**(metadata or {}), "ls_run_name": name or ""}, tags or [])
        with self._lock:
            if kind in {"agent", "helper"}:
                kind = self._chain_purposes.get(str(parent_run_id), kind)
            self._chain_purposes[str(run_id)] = kind
            while len(self._chain_purposes) > 4096:
                self._chain_purposes.popitem(last=False)

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        with self._lock:
            self._chain_purposes.pop(str(run_id), None)

    def on_chain_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self.on_chain_end(None, run_id=run_id)

    def _guard_request(self, scope: CallScope, kind: str) -> None:
        if kind == "agent" and scope.budget_guard is not None:
            return  # The normal agent middleware checked this request already.
        guard = scope.budget_guard
        if guard is None:
            from .agents.defaults import merged_agents
            from .agents.registry import list_agents
            from .config import load_config
            from .middleware.economy import TokenEconomyMiddleware
            budget = load_config().budget
            defn = next((a for a in merged_agents(list_agents()) if a.name == scope.agent), None)
            max_tokens = getattr(defn, "budget_max_tokens", None)
            max_usd = getattr(defn, "budget_max_usd", None)
            key = (str(scope.ledger_path), scope.agent, max_tokens, max_usd, budget.daily_max_usd, budget.mode)
            with self._lock:
                guard = self._helper_guards.get(key)
                if guard is None:
                    guard = TokenEconomyMiddleware(agent=scope.agent, max_tokens=max_tokens, max_usd=max_usd,
                        daily_max_usd=budget.daily_max_usd, budget_mode=budget.mode, ledger_path=scope.ledger_path)
                    self._helper_guards[key] = guard
        self.flush()
        if guard._maybe_pause(scope.thread_id) == "stop":
            raise RuntimeError("Stopped at the budget cap as requested; helper request was not sent")

    def on_chat_model_start(self, serialized: dict, messages: list, *, run_id: Any,
                            metadata: dict | None = None, tags: list | None = None,
                            parent_run_id: Any = None,
                            **kwargs: Any) -> None:
        metadata = metadata or {}
        config = ensure_config().get("configurable") or {}
        scope = _SCOPE.get()
        requested = metadata.get("ls_model_name") or serialized.get("kwargs", {}).get("model") or ""
        if scope is None:
            from types import SimpleNamespace
            requested_model = SimpleNamespace(model_name=metadata.get("manta_target") or requested,
                transport_surface=metadata.get("manta_surface", ""),
                transport_api=metadata.get("manta_protocol", ""))
            scope = CallScope(
                agent=str(metadata.get("manta_agent") or os.environ.get("DEEPAGENTS_CODE_SERVER_ASSISTANT_ID") or "orchestrator"),
                thread_id=str(metadata.get("thread_id") or config.get("thread_id") or ""),
                model=requested_model,
            )
        elif requested and requested.removeprefix("databricks:") != str(
            getattr(scope.model, "model_name", getattr(scope.model, "model", scope.model))
        ).removeprefix("databricks:"):
            # A direct side invoke within an agent call can use a different model.
            from dataclasses import replace
            scope = replace(scope, model=requested, scaffold_tokens=0, net_new_tokens=0)
        from dataclasses import replace
        from .agents.usage import effective_pricing, usage_db_path
        scope = replace(scope, ledger_path=scope.ledger_path or usage_db_path(),
                        pricing=scope.pricing if scope.pricing is not None else effective_pricing(),
                        task=scope.task or os.environ.get("MANTA_TASK_ID") or scope.agent)
        kind = _purpose(metadata, tags or [])
        if kind in {"agent", "helper"}:
            with self._lock:
                inherited_kind = self._chain_purposes.get(str(parent_run_id))
            if inherited_kind in {"classifier", "summarizer", "grader"}:
                kind = inherited_kind
        self._guard_request(scope, kind)
        with self._lock:
            self._contexts[str(run_id)] = (scope, kind, time.monotonic())
            while len(self._contexts) > 4096:
                self._contexts.popitem(last=False)
                logger.error("Manta usage start context evicted; accounting coverage incomplete")

    def on_llm_start(self, serialized: dict, prompts: list, **kwargs: Any) -> None:
        self.on_chat_model_start(serialized, [], **kwargs)

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        with self._lock:
            entry = self._contexts.pop(str(run_id), None)
        if entry is None:
            return
        scope, kind, started = entry
        messages = [getattr(g, "message", None) for batch in response.generations for g in batch]
        message = next((m for m in messages if getattr(m, "usage_metadata", None)), None)
        if message is None:
            message = next((m for m in messages if m is not None), None)
        if message is None:
            return
        # A callback and the outer middleware see the same message instance.
        message.response_metadata["manta_invocation_id"] = str(run_id)
        message.response_metadata["manta_call_kind"] = kind
        message.response_metadata["manta_duration_ms"] = (time.monotonic() - started) * 1000
        self._worker.submit(self._record, scope, message.model_copy(deep=True))

    def _record(self, scope: CallScope, message: Any) -> None:
        try:
            from .agents.usage import record_model_response
            record_model_response(
                scope.model, message, agent=scope.agent,
                task=scope.task,
                thread_id=scope.thread_id, scaffold_tokens=scope.scaffold_tokens,
                net_new_tokens=scope.net_new_tokens, pricing=scope.pricing, path=scope.ledger_path,
                strict=True,
            )
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.failed_ledgers[str(scope.ledger_path)] = self.last_error
            logger.exception("Manta request accounting failed; coverage incomplete")

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        with self._lock:
            entry = self._contexts.pop(str(run_id), None)
        if entry is None:
            return
        from langchain_core.messages import AIMessage
        scope, kind, started = entry
        # An interrupted/failed attempt may have incurred provider charges.
        # Persist an unknown event rather than making completeness look better.
        # Error messages and request contents are deliberately not logged.
        message = AIMessage(content="", response_metadata={
            "manta_invocation_id": str(run_id), "manta_call_kind": kind,
            "manta_duration_ms": (time.monotonic() - started) * 1000,
            "manta_outcome": "cancelled" if type(error).__name__ == "CancelledError" else "error",
        })
        self._worker.submit(self._record, scope, message)

    def flush(self) -> None:
        self._worker.submit(lambda: None).result(timeout=30)


_RECORDER = RequestCollector()
_RECORDER_VAR: ContextVar[RequestCollector | None] = ContextVar(
    "manta_request_collector", default=_RECORDER,
)
_installed = False


def install_usage_collector() -> bool:
    global _installed
    if not _installed:
        from langchain_core.tracers.context import register_configure_hook
        # Graph/node callbacks must inherit the same collector so helper graph
        # purpose survives through intermediate nodes to model requests. The
        # configure hook deduplicates an existing handler by object identity.
        register_configure_hook(_RECORDER_VAR, inheritable=True)
        _installed = True
    return True


def flush_usage() -> None:
    if _installed:
        _RECORDER.flush()


def collector_health() -> tuple[bool, str]:
    return _installed and not _RECORDER.last_error, _RECORDER.last_error or "request callback collector"


def accounting_error(path: Any = None) -> str | None:
    from .agents.usage import usage_db_path
    return _RECORDER.failed_ledgers.get(str(usage_db_path(path)))
