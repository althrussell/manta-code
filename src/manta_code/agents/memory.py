"""Durable per-agent memory with privacy guardrails (ADR 0008, Phase 3).

Real agents "sharpen over time" only if they can remember across sessions — but
for a Databricks user the top fear is data leakage. So memory here is built
privacy-first:

- **Durability:** a persistent LangGraph ``SqliteStore`` at
  ``~/.manta/.state/memory.db`` backs the memory. The recall middleware reads it
  *directly* (a per-process cached handle), not via the agent graph's ``store=``.
  This is deliberate: the ``langgraph dev`` API server that ``deepagents-code``
  runs the agent in **rejects** graphs that carry a custom ``BaseStore`` (it
  manages persistence itself), so injecting our store there crashes the server
  at startup. Reading our own store from the middleware keeps memory durable
  without fighting the platform's persistence.
- **Redaction is mandatory and structural:** the store is wrapped in a
  :class:`RedactingStore` that scrubs secrets/PII from *every* value before it is
  written. Because all writes funnel through ``BaseStore.batch``, there is no
  write path that bypasses redaction — it is enforced, not advisory.
- **Namespacing:** each agent's memory lives under ``("memories", <namespace>)``
  so agents don't read each other's context unless they share a namespace.
- **Recall, not pollution:** :class:`AgentMemoryMiddleware` injects an agent's
  remembered notes into the system prompt only when present, and never writes —
  writing is explicit (the ``manta agents memory --add`` path / future tools),
  keeping curation under control.

Everything is guarded so a missing store or an upstream API change degrades to
"no memory" rather than blocking a launch.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..config import user_manta_dir

#: Where the durable memory store lives.
MEMORY_DB_PATH = Path(".state") / "memory.db"

# --- redaction ------------------------------------------------------------

#: Ordered (pattern, replacement) redaction rules. Conservative and broad: we
#: would rather over-redact a memory than leak a credential into it.
_REDACTORS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Databricks personal access tokens.
    (re.compile(r"dapi[0-9a-fA-F]{32,}"), "[redacted-databricks-token]"),
    # AWS access key ids / secret hints.
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[redacted-aws-key]"),
    # Slack tokens.
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "[redacted-slack-token]"),
    # GitHub tokens.
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"), "[redacted-github-token]"),
    # Bearer / authorization headers.
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer [redacted-token]"),
    # key = value / key: value secrets.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|"
            r"client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{4,}"
        ),
        r"\1=[redacted]",
    ),
    # Emails.
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    # US SSNs.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[redacted-ssn]"),
    # 13-16 digit card-like numbers.
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[redacted-number]"),
)


def redact_text(text: str) -> str:
    """Return ``text`` with secrets/PII replaced by ``[redacted-*]`` markers."""
    out = text
    for pattern, replacement in _REDACTORS:
        out = pattern.sub(replacement, out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside a JSON-like value (dict/list/str)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def _redacting_store_class() -> type:
    """Build the ``RedactingStore`` class (lazy import of langgraph base)."""
    from langgraph.store.base import BaseStore, PutOp

    class RedactingStore(BaseStore):
        """A ``BaseStore`` wrapper that redacts secrets/PII from every write.

        All higher-level writes (``put``) route through ``batch``/``abatch`` on
        ``BaseStore``, so intercepting those two methods covers every write path.
        Reads and all other operations delegate to the inner store unchanged.
        """

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def _redact_op(self, op: Any) -> Any:
            if isinstance(op, PutOp) and op.value is not None:
                return op._replace(value=redact_value(op.value))
            return op

        def batch(self, ops: Any) -> Any:
            return self._inner.batch([self._redact_op(op) for op in ops])

        async def abatch(self, ops: Any) -> Any:
            return await self._inner.abatch([self._redact_op(op) for op in ops])

        @property
        def supports_ttl(self) -> bool:  # pragma: no cover - trivial delegation
            return getattr(self._inner, "supports_ttl", False)

        @property
        def ttl_config(self) -> Any:  # pragma: no cover - trivial delegation
            return getattr(self._inner, "ttl_config", None)

    return RedactingStore


def open_store(path: Path | None = None) -> Any:
    """Open a durable, redaction-wrapped ``SqliteStore`` at ``path``.

    The caller owns the lifetime; the underlying sqlite connection is opened with
    ``check_same_thread=False`` so the langgraph server's threads can share it.
    """
    from langgraph.store.sqlite import SqliteStore

    db_path = path or (user_manta_dir() / MEMORY_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None puts the connection in autocommit mode so SqliteStore
    # manages its own BEGIN/COMMIT (the default sqlite3 implicit transaction
    # would otherwise collide with the store's explicit BEGIN).
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    inner = SqliteStore(conn)
    inner.setup()
    return _redacting_store_class()(inner)


#: Per-process cached durable store, opened lazily on first recall. We do NOT
#: inject this into the agent graph (``langgraph dev`` rejects custom stores);
#: the recall middleware reads it directly instead.
_SHARED_STORE: Any | None = None
_SHARED_STORE_TRIED = False


def shared_memory_store() -> Any | None:
    """Return a process-wide durable redacting store, or ``None``.

    Opened once and cached. Guarded: if langgraph's sqlite store is unavailable
    (or opening fails) this returns ``None`` so recall degrades to "no memory"
    rather than raising. The underlying connection uses
    ``check_same_thread=False`` so the langgraph server's worker threads share it.
    """
    global _SHARED_STORE, _SHARED_STORE_TRIED  # noqa: PLW0603
    if _SHARED_STORE is not None:
        return _SHARED_STORE
    if _SHARED_STORE_TRIED:
        return None
    _SHARED_STORE_TRIED = True
    try:
        _SHARED_STORE = open_store()
    except Exception:  # noqa: BLE001
        _SHARED_STORE = None
    return _SHARED_STORE


# --- namespacing + access helpers -----------------------------------------


def memory_namespace(defn: Any) -> tuple[str, str]:
    """Return the store namespace tuple for an agent definition."""
    ns = defn.effective_namespace() if hasattr(defn, "effective_namespace") else str(defn)
    return ("memories", ns)


def write_memory(store: Any, namespace: tuple[str, str], key: str, text: str) -> None:
    """Write a memory note (redaction is enforced by the store wrapper)."""
    store.put(namespace, key, {"text": text})


def read_memories(store: Any, namespace: tuple[str, str], *, limit: int = 50) -> list[str]:
    """Return remembered note texts for a namespace (most-relevant first)."""
    try:
        items = store.search(namespace, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    texts: list[str] = []
    for item in items:
        value = getattr(item, "value", None) or {}
        text = value.get("text") if isinstance(value, dict) else None
        if text:
            texts.append(str(text))
    return texts


def clear_memories(store: Any, namespace: tuple[str, str]) -> int:
    """Delete every memory in a namespace. Returns the count removed."""
    try:
        items = store.search(namespace, limit=1000)
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    for item in items:
        key = getattr(item, "key", None)
        if key is not None:
            store.delete(namespace, key)
            count += 1
    return count


# --- recall middleware ------------------------------------------------------


def _agent_memory_middleware_class() -> type:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import SystemMessage

    class AgentMemoryMiddleware(AgentMiddleware):
        """Inject an agent's remembered notes into the system prompt (recall-only).

        The durable store is sourced from Manta directly (an explicit handle or
        the per-process :func:`shared_memory_store`), never from the agent graph's
        runtime store — the ``langgraph dev`` server forbids custom graph stores.
        """

        def __init__(self, namespace: tuple[str, str], store: Any | None = None) -> None:
            super().__init__()
            self._namespace = namespace
            self._store = store

        @property
        def name(self) -> str:
            return f"Manta.Memory.{self._namespace[-1]}"

        def _resolve_store(self) -> Any | None:
            return self._store if self._store is not None else shared_memory_store()

        def _recall_block(self, store: Any) -> str | None:
            notes = read_memories(store, self._namespace, limit=20)
            if not notes:
                return None
            bullets = "\n".join(f"- {n}" for n in notes)
            return f"\n\n## Remembered context (private to this agent)\n{bullets}"

        def _with_recall(self, request: Any) -> Any:
            """Return ``request`` with remembered notes appended, or unchanged.

            Never raises: recall is best-effort, so any failure falls back to the
            original request.
            """
            try:
                store = self._resolve_store()
                if store is None:
                    return request
                block = self._recall_block(store)
                if not block:
                    return request
                base = getattr(request, "system_message", None)
                base_text = base.content if base is not None else ""
                return request.override(
                    system_message=SystemMessage(content=str(base_text) + block)
                )
            except Exception:  # noqa: BLE001 - recall is best-effort
                return request

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._with_recall(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            # deepagents runs the agent in an async context (astream/ainvoke), so
            # the async variant must exist or it raises NotImplementedError.
            return await handler(self._with_recall(request))

    return AgentMemoryMiddleware


def agent_memory_middleware(defn: Any, store: Any | None = None) -> Any | None:
    """Return a recall middleware for ``defn``, or ``None`` if memory is off.

    Called by the build hook when compiling each agent. ``store`` is an optional
    explicit store (used in tests); when omitted the middleware reads the
    per-process :func:`shared_memory_store` at call time. Guarded so a langchain
    API change degrades to "no memory middleware" rather than breaking the agent.
    """
    if not getattr(defn, "memory", False):
        return None
    try:
        cls = _agent_memory_middleware_class()
        return cls(memory_namespace(defn), store=store)
    except Exception:  # noqa: BLE001
        return None
