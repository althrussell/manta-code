"""Honest context preflight for Gateway requests, without changing history.

Gateway services do not expose a portable token-count endpoint. Text uses the
installed OpenAI tokenizer as an estimate (including instructions and tool
schemas); images, audio, video and opaque signed state remain explicitly
unmeasured. This is a local guard, never a guarantee of provider acceptance.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import cache
from typing import Any


@dataclass(frozen=True)
class ContextPreflight:
    context_window: int | None
    input_tokens_estimate: int
    reserved_output_tokens: int | None
    max_output_tokens: int | None
    estimate_complete: bool
    unmeasured: tuple[str, ...]
    method: str = "o200k_base text estimate; provider tokenization may differ"

    @property
    def total_tokens_estimate(self) -> int | None:
        if self.reserved_output_tokens is None:
            return None
        return self.input_tokens_estimate + self.reserved_output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_tokens_estimate": self.total_tokens_estimate}


@cache
def _encoding() -> Any:
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


def context_preflight(payload: dict[str, Any], profile: dict[str, Any]) -> ContextPreflight:
    """Estimate the actual serialized input plus the requested output reserve.

    The omitted reserve defaults to the documented output maximum, when known.
    Unknown model limits or media counts stay unknown; encoded media/signatures
    are never counted as ordinary text or copied into diagnostics.
    """
    unmeasured: set[str] = set()

    def text_only(value: Any, key: str = "") -> Any:
        if key in {"encrypted_content", "signature", "thoughtSignature", "thought_signature"}:
            unmeasured.add("opaque continuation state")
            return None
        if isinstance(value, dict):
            block = str(value.get("type", ""))
            if any(part in block for part in ("image", "audio", "video", "file")):
                unmeasured.add(block or "media")
                return {"type": block}
            return {k: text_only(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [text_only(v) for v in value]
        if isinstance(value, str) and value.startswith("data:"):
            unmeasured.add("encoded media")
            return None
        return value

    inputs = {key: payload[key] for key in ("messages", "input", "instructions", "tools") if key in payload}
    sanitized = text_only(inputs)
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), default=str)
    method = ContextPreflight.__dataclass_fields__["method"].default
    try:
        tokens = len(_encoding().encode(serialized, disallowed_special=()))
    except (ImportError, OSError, ValueError):
        # Remain usable without tokenizer assets; label the coarser estimate.
        tokens = (len(serialized) + 3) // 4
        method = "characters / 4 text estimate; provider tokenization may differ"
    maximum = profile.get("max_output_tokens")
    unary_maximum = profile.get("max_output_tokens_unary")
    if payload.get("stream") is not True and isinstance(unary_maximum, int):
        maximum = min(maximum, unary_maximum) if isinstance(maximum, int) else unary_maximum
    maximum = maximum if isinstance(maximum, int) else None
    reserve = next((payload[k] for k in ("max_output_tokens", "max_completion_tokens", "max_tokens") if payload.get(k) is not None), maximum)
    reserve = reserve if isinstance(reserve, int) and not isinstance(reserve, bool) else None
    limits = [profile.get(key) for key in ("context_window", "max_input_tokens")]
    limits = [limit for limit in limits if isinstance(limit, int) and not isinstance(limit, bool)]
    context = min(limits) if limits else None
    context = context if isinstance(context, int) and not isinstance(context, bool) else None
    return ContextPreflight(context, tokens, reserve, maximum, not unmeasured, tuple(sorted(unmeasured)), method)
