"""Bounded, profile-scoped Unity/legacy metadata discovery.

Metadata access and execution authorization are independent. A denied or failed
lookup never establishes absence and never prevents explicitly configured calls.
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

from .targets import CapabilityEvidence, DatabricksTarget, TargetKind, _policy_inputs, curated_capabilities, parse_target


@dataclass(frozen=True)
class DiscoveryOutcome:
    source: str
    status: str
    schema: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class TargetInfo:
    target: DatabricksTarget
    supported_api_types: tuple[str, ...] = ()
    capabilities: CapabilityEvidence = field(default_factory=CapabilityEvidence)
    metadata_status: str = "unverified"
    source: str = "configured"

    @property
    def name(self) -> str:
        return self.target.name

    @property
    def coding_capable(self) -> bool:
        # Empty metadata is unknown, rather than known embeddings-only.
        return not self.supported_api_types or any("responses" in a or "chat/completions" in a or a == "llm/v1/chat" or "messages" in a or "generateContent" in a for a in self.supported_api_types)


@dataclass(frozen=True)
class DiscoveryResult:
    targets: tuple[TargetInfo, ...]
    outcomes: tuple[DiscoveryOutcome, ...]
    workspace: str | None = None
    profile: str | None = None
    cached: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.targets if item.coding_capable)

    def find(self, name: str | DatabricksTarget, kind: str | None = None) -> TargetInfo | None:
        parsed = _configured_target(name, kind)
        return next((item for item in self.targets if item.target == parsed), None)


@dataclass(frozen=True)
class TargetLookup:
    target: TargetInfo | None
    outcome: DiscoveryOutcome
    exists: bool | None
    detail: Any = field(default=None, repr=False, compare=False)


_CACHE: OrderedDict[tuple[Any, ...], tuple[float, DiscoveryResult]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 32
# A semaphore bounds abandoned background calls after a discovery deadline.
_WORKERS = threading.BoundedSemaphore(8)


def clear_discovery_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _status(exc: Exception) -> str:
    code = str(getattr(exc, "error_code", "") or "").upper()
    http_status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if http_status in {401, 403} or code in {"PERMISSION_DENIED", "UNAUTHENTICATED", "UNAUTHORIZED"} or "permissiondenied" in name or "unauthenticated" in name:
        return "denied"
    if http_status == 404 or code in {"RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"} or "notfound" in name or "resourcedoesnotexist" in name:
        return "absent"
    if isinstance(exc, (AttributeError, NotImplementedError)) or http_status in {405, 501} or code in {"NOT_IMPLEMENTED", "UNSUPPORTED_OPERATION", "FEATURE_DISABLED", "FEATURE_NOT_ENABLED", "NOT_SUPPORTED"}:
        return "unsupported"
    return "unavailable"


classify_discovery_error = _status


def _message(exc: Exception) -> str:
    # Exception bodies may include authorization headers, URLs or credentials.
    # Only classification/code and class names leave the discovery boundary.
    code = getattr(exc, "error_code", None)
    return f"{type(exc).__name__}" + (f" ({code})" if code else "")


def _service_info(detail: Any, *, source: str = "unity") -> TargetInfo:
    target = parse_target(detail.name, kind="service")
    apis = tuple(getattr(detail, "supported_api_types", None) or ())
    curated = curated_capabilities(target)
    evidence = CapabilityEvidence(values=curated.values, sources={**curated.sources, "supported_api_types": "Unity Catalog supported_api_types"} if apis else curated.sources, supported_api_types=apis or curated.supported_api_types)
    return TargetInfo(target, apis, evidence, "verified", source)


def _endpoint_info(detail: Any, *, source: str = "legacy") -> TargetInfo:
    target = parse_target(detail.name, kind="endpoint")
    task = getattr(detail, "task", None)
    apis = ("llm/v1/chat",) if task == "llm/v1/chat" else ((str(task),) if task else ())
    return TargetInfo(target, apis, CapabilityEvidence(supported_api_types=apis), "verified", source)


def _get_client(profile: str | None, client: Any) -> Any:
    if client is not None:
        return client
    from ..auth import resolve_workspace_client
    return resolve_workspace_client(profile)


def _configured_target(name: str | DatabricksTarget, kind: str | None = None) -> DatabricksTarget:
    if isinstance(name, DatabricksTarget):
        return parse_target(name, kind)
    settings, _, _ = _policy_inputs()
    kind = kind or settings.get("targets", {}).get(name.removeprefix("databricks:").removeprefix("model-services/"), {}).get("kind")
    return parse_target(name, kind)


def _lookup_unbounded(target: DatabricksTarget, profile: str | None, client: Any) -> TargetLookup:
    source = "unity" if target.kind == TargetKind.SERVICE else "legacy"
    try:
        client = _get_client(profile, client)
        if target.kind == TargetKind.SERVICE:
            detail = client.ai_gateway.get_model_service(name=target.resource_name)
            info = _service_info(detail)
        else:
            detail = client.serving_endpoints.get(name=target.name)
            info = _endpoint_info(detail)
        return TargetLookup(info, DiscoveryOutcome(source, "success"), True, detail)
    except Exception as exc:  # noqa: BLE001 - metadata failure is an outcome
        status = _status(exc)
        return TargetLookup(None, DiscoveryOutcome(source, status, message=_message(exc)), False if status == "absent" else None)


def lookup_databricks_target(name: str | DatabricksTarget, profile: str | None = None, *, client: Any = None, kind: str | None = None, timeout: float | None = None) -> TargetLookup:
    """Fetch FULL metadata within a bounded deadline, including SDK auth setup.

    An expired deadline is unverified rather than absent. Shared worker capacity
    also bounds abandoned SDK calls, whose internal retries cannot be cancelled.
    """
    from ..config import load_config

    target = _configured_target(name, kind)
    source = "unity" if target.kind == TargetKind.SERVICE else "legacy"
    seconds = load_config().databricks.discovery_timeout_seconds if timeout is None else timeout
    if not _WORKERS.acquire(blocking=False):
        return TargetLookup(None, DiscoveryOutcome(source, "unavailable", message="Metadata concurrency limit reached; retry after existing lookups finish."), None)
    results: queue.Queue[TargetLookup] = queue.Queue(maxsize=1)

    def lookup() -> None:
        try:
            results.put(_lookup_unbounded(target, profile, client))
        finally:
            _WORKERS.release()

    threading.Thread(target=lookup, daemon=True, name="manta-target-lookup").start()
    try:
        return results.get(timeout=max(0, min(seconds, 30)))
    except queue.Empty:
        return TargetLookup(None, DiscoveryOutcome(source, "unavailable", message="Metadata lookup deadline exceeded; execution access remains unverified."), None)


def _auth_fingerprint(client: Any, profile: str | None) -> tuple[str, ...]:
    config = getattr(client, "config", None)
    # Never mint credentials/current_user just to scope metadata. Credential
    # material only contributes a one-way hash, never a cache/log key itself.
    identifiers = ("host", "auth_type", "profile", "client_id", "azure_client_id", "username", "config_file", "token")
    pieces = [str(getattr(config, key, "") or "") for key in identifiers]
    pieces += [str(os.environ.get(key, "")) for key in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_FILE", "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET")]
    from ..auth import config_file_path
    path = config_file_path()
    try:
        stat = path.stat()
        pieces.extend((str(path), str(stat.st_mtime_ns), str(stat.st_size)))
    except OSError:
        pieces.append(str(path))
    return (profile or "", hashlib.sha256("\0".join(pieces).encode()).hexdigest())


def _bounded_client(profile: str | None, client: Any, deadline: float) -> Any:
    if client is not None:
        return client
    if not _WORKERS.acquire(blocking=False):
        raise TimeoutError("Discovery concurrency limit reached")
    results: queue.Queue[Any] = queue.Queue(maxsize=1)
    def build() -> None:
        try:
            results.put((True, _get_client(profile, None)))
        except Exception as exc:  # noqa: BLE001 - return failure to caller
            results.put((False, exc))
        finally:
            _WORKERS.release()
    threading.Thread(target=build, daemon=True, name="manta-discovery-auth").start()
    try:
        ok, result = results.get(timeout=max(0, deadline-time.monotonic()))
    except queue.Empty as exc:
        raise TimeoutError("Workspace authentication exceeded discovery deadline") from exc
    if not ok:
        raise result
    return result


def _collect(source: str, schema: str | None, client: Any, output: list[TargetInfo], lock: threading.Lock) -> DiscoveryOutcome:
    try:
        iterator = client.ai_gateway.list_model_services(parent=f"schemas/{schema}", page_size=100) if source == "unity" else client.serving_endpoints.list()
        for detail in iterator:
            if source == "legacy" and getattr(detail, "task", None) != "llm/v1/chat":
                continue
            item = _service_info(detail) if source == "unity" else _endpoint_info(detail)
            with lock:
                output.append(item)
        return DiscoveryOutcome(source, "success", schema)
    except Exception as exc:  # noqa: BLE001 - covers lazy iteration/later pages
        with lock:
            partial = bool(output)
        status = "partial" if partial else _status(exc)
        # A missing schema or unsupported Unity route does not establish absence
        # of individually configured model services.
        if status == "absent":
            status = "unsupported"
        return DiscoveryOutcome(source, status, schema, _message(exc))


def discover_databricks_targets(profile: str | None = None, schemas: list[str] | tuple[str, ...] | None = None, refresh: bool = False, configured: Any = (), *, client: Any = None, timeout: float | None = None) -> DiscoveryResult:
    from ..auth import resolve_profile
    from ..config import load_config
    profile = resolve_profile(profile)
    settings = load_config().databricks
    schemas = tuple(dict.fromkeys(schemas if schemas is not None else settings.discovery_schemas))
    for schema in schemas:
        parse_target(f"{schema}.validation", kind="service")
    configured_targets = tuple(_configured_target(name) for name in configured if name)
    timeout = timeout if timeout is not None else settings.discovery_timeout_seconds
    deadline = time.monotonic() + max(0, min(timeout, 30))
    try:
        client = _bounded_client(profile, client, deadline)
    except Exception as exc:  # noqa: BLE001
        items = tuple(TargetInfo(t, capabilities=curated_capabilities(t)) for t in configured_targets)
        return DiscoveryResult(items, (DiscoveryOutcome("authentication", _status(exc), message=_message(exc)),), profile=profile)
    workspace = getattr(getattr(client, "config", None), "host", None)
    key = (*_auth_fingerprint(client, profile), schemas, tuple((t.name, t.kind) for t in configured_targets))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < settings.discovery_cache_seconds:
            _CACHE.move_to_end(key)
            return replace(cached[1], cached=True)
    jobs: list[tuple[str, str | None, list[TargetInfo], threading.Lock, queue.Queue[DiscoveryOutcome]]] = []
    for source, schema in [("legacy", None), *(("unity", schema) for schema in schemas)]:
        output: list[TargetInfo] = []
        lock = threading.Lock()
        result_queue: queue.Queue[DiscoveryOutcome] = queue.Queue(maxsize=1)
        jobs.append((source, schema, output, lock, result_queue))
        if not _WORKERS.acquire(blocking=False):
            result_queue.put(DiscoveryOutcome(source, "unavailable", schema, "Discovery concurrency limit reached; retry with refresh."))
            continue
        def run(source: str = source, schema: str | None = schema, output: list[TargetInfo] = output, lock: threading.Lock = lock, result_queue: queue.Queue[DiscoveryOutcome] = result_queue) -> None:
            try:
                result_queue.put(_collect(source, schema, client, output, lock))
            finally:
                _WORKERS.release()
        threading.Thread(target=run, daemon=True, name="manta-discovery").start()
    items: dict[DatabricksTarget, TargetInfo] = {}
    outcomes: list[DiscoveryOutcome] = []
    for source, schema, output, lock, result_queue in jobs:
        try:
            outcome = result_queue.get(timeout=max(0, deadline-time.monotonic()))
        except queue.Empty:
            with lock:
                outcome = DiscoveryOutcome(source, "partial" if output else "unavailable", schema, "Discovery deadline exceeded; configured targets remain usable.")
        with lock:
            for item in output:
                items[item.target] = item
        outcomes.append(outcome)
    # Direct configured pins are independent of schema enumeration. Run them
    # under the same launch deadline, with bounded workers; no startup N+1 FULL
    # fetches for targets already present in BASIC inventory.
    lookups: queue.Queue[tuple[DatabricksTarget, TargetLookup]] = queue.Queue()
    pending = 0
    for target in configured_targets:
        if target in items:
            continue
        items[target] = TargetInfo(target, capabilities=curated_capabilities(target))
        if time.monotonic() >= deadline or not _WORKERS.acquire(blocking=False):
            outcomes.append(DiscoveryOutcome("configured", "unavailable", message=f"{target.name}: direct metadata lookup deferred by discovery deadline."))
            continue
        pending += 1
        def direct(target: DatabricksTarget = target) -> None:
            try:
                lookups.put((target, _lookup_unbounded(target, profile, client)))
            finally:
                _WORKERS.release()
        threading.Thread(target=direct, daemon=True, name="manta-discovery-pin").start()
    for _ in range(pending):
        try:
            target, lookup = lookups.get(timeout=max(0, deadline-time.monotonic()))
        except queue.Empty:
            outcomes.append(DiscoveryOutcome("configured", "unavailable", message="Direct metadata lookup deadline exceeded."))
            break
        outcomes.append(replace(lookup.outcome, source=f"configured:{target.name}"))
        if lookup.target:
            items[target] = lookup.target
        elif lookup.exists is False:
            # Retain explicitly configured names with a precise absence label.
            items[target] = replace(items[target], metadata_status="absent")
    result = DiscoveryResult(tuple(items.values()), tuple(outcomes), workspace, profile)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return result
