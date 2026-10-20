"""No-inference setup checks and explicitly requested model qualification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeResult:
    name: str
    protocol: str
    surface: str
    ok: bool
    detail: str
    calls: int = 0


def credential_context(profile: str | None = None, *, timeout_seconds: float | None = None) -> str:
    """Resolve SDK auth without requiring the identity or inference APIs.

    Authentication headers remain local and are never included in diagnostics.
    A successful refresh proves credential construction, not EXECUTE privileges.
    """
    import queue
    import threading

    from .auth import resolve_profile, resolve_workspace_client
    from .config import load_config
    from .providers.discovery import _WORKERS

    seconds = load_config().databricks.discovery_timeout_seconds if timeout_seconds is None else timeout_seconds
    if not _WORKERS.acquire(blocking=False):
        raise TimeoutError("Credential setup concurrency limit reached")
    results: queue.Queue[tuple[bool, str]] = queue.Queue(maxsize=1)

    def check() -> None:
        try:
            client = resolve_workspace_client(profile)
            headers = client.config.authenticate()
            if not headers:
                raise ValueError("SDK did not produce authentication headers")
            selected = resolve_profile(profile) or "default profile"
            results.put((True, f"{selected}; SDK credentials ready; inference access unverified"))
        except Exception as exc:  # noqa: BLE001 - no credential/provider bodies cross thread boundary
            results.put((False, type(exc).__name__))
        finally:
            _WORKERS.release()

    threading.Thread(target=check, daemon=True, name="manta-credential-check").start()
    try:
        ok, detail = results.get(timeout=max(0, min(seconds, 30)))
    except queue.Empty as exc:
        raise TimeoutError("Credential setup deadline exceeded") from exc
    if not ok:
        raise ValueError(f"SDK credential construction failed ({detail})")
    return detail


def upstream_default_diagnostic(path: Path | None = None) -> tuple[str, str] | None:
    """Report an upstream Databricks default without changing user-owned state."""
    import os
    import tomllib

    from .providers.targets import parse_target

    if path is None:
        path = Path(os.environ.get("DEEPAGENTS_HOME", Path.home() / ".deepagents")) / "config.toml"
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, ValueError):
        return None
    # Deep Agents' current config keeps the default under [models]. Older
    # versions used a top-level default_model; report either without rewriting.
    model_config = config.get("models", {})
    model = model_config.get("default") or model_config.get("default_model") or config.get("default_model")
    if not isinstance(model, str) or not model.startswith("databricks:"):
        return None
    try:
        target = parse_target(model.split(":", 1)[1])
    except ValueError:
        return model, "invalid Databricks default; select an exact endpoint or service name"
    if target.kind.value == "endpoint":
        return model, "legacy endpoint default; Unity-enforced workspaces require a model service; user default preserved"
    return model, "Unity model-service default; user default preserved"


def probe_databricks_target(
    name: str,
    *,
    profile: str | None = None,
    timeout_seconds: int = 30,
    model_params: dict[str, Any] | None = None,
    force_tool: bool = False,
) -> ProbeResult:
    """Qualify a selected protocol with a bounded two-request agent tool loop.

    The only tool echoes a random qualification nonce. Optional forced selection
    verifies schema serialization, call decoding, tool-result replay, opaque
    state retention, and the final response. No arbitrary tools are installed.
    No protocol or provider fallback occurs on any failure.
    """
    import secrets

    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool

    from .databricks_chat import MantaChatDatabricks
    from .agents.usage import record_model_response
    from .providers.discovery import lookup_databricks_target
    from .providers.targets import resolve_target_policy

    protocol, surface, calls = "unresolved", "unresolved", 0
    try:
        from .main import _databricks_profile_env

        with _databricks_profile_env(profile):
            lookup = lookup_databricks_target(name, profile=profile)
            policy = resolve_target_policy(name, explicit={
                **(model_params or {}), "timeout": timeout_seconds, "max_retries": 0,
            }, capability=lookup.target.capabilities if lookup.target else None)
            protocol = str(getattr(policy.api, "value", policy.api))
            surface = str(getattr(policy.surface, "value", policy.surface))
            options = policy.model_kwargs()
            if profile:
                options["databricks_profile"] = profile
            model = MantaChatDatabricks(**options)
            nonce = f"MANTA_PROBE_{secrets.token_hex(8)}"

            @tool
            def manta_probe_echo(value: str) -> str:
                """Return the exact qualification value unchanged."""
                return value

            history = [HumanMessage(content=(
                "Call manta_probe_echo exactly once with value " + nonce + ". "
                "After receiving the tool result, reply with that exact value and nothing else."
            ))]
            bind_options = {"tool_choice": "manta_probe_echo"} if force_tool else {}
            bound = model.bind_tools([manta_probe_echo], **bind_options)
            calls += 1
            response = bound.invoke(history)
            record_model_response(model, response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if len(tool_calls) != 1:
                raise ValueError("expected one tool call; model did not complete requested tool selection")
            call = tool_calls[0]
            if call.get("name") != "manta_probe_echo" or call.get("args") != {"value": nonce}:
                raise ValueError("tool name/arguments did not round-trip")
            if not call.get("id"):
                raise ValueError("tool call did not contain a replay identifier")
            history.extend([
                response,
                ToolMessage(content=manta_probe_echo.invoke(call["args"]), tool_call_id=call["id"]),
            ])
            calls += 1
            final = model.bind_tools([manta_probe_echo]).invoke(history)
            record_model_response(model, final)
            if getattr(final, "tool_calls", None):
                raise ValueError("model requested another tool instead of completing the roundtrip")
            text = getattr(final, "text", None)
            if callable(text) and not isinstance(text, str):
                text = text()
            if text is None:
                text = getattr(final, "content", "")
            if not isinstance(text, str) or text.strip() != nonce:
                raise ValueError("final response did not preserve the tool result")
            return ProbeResult(name, protocol, surface, True, "tool call/result/final response qualified", calls)
    except Exception as exc:  # noqa: BLE001 - doctor reports each target independently
        # Never echo request bodies, headers, or arbitrary provider exceptions.
        # SDK/OpenAI error codes are safe and more useful than heuristic text.
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "error_code", None)
        reason = f"HTTP {status}" if status else (str(code) if code else type(exc).__name__)
        guidance = {
            400: "request rejected; check target API capabilities and model parameters",
            401: "credentials rejected; refresh the selected Databricks profile",
            403: "access denied; verify gateway scope, EXECUTE, USE CATALOG and USE SCHEMA",
            404: "target or route unavailable; verify the exact service name and workspace",
            429: "rate limit or gateway quota exceeded; retry after the limit clears",
        }.get(status)
        if guidance:
            reason += "; " + guidance
        if isinstance(exc, ValueError):
            reason = str(exc)
        return ProbeResult(name, protocol, surface, False, reason, calls)
