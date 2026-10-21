"""Translate qualified Chat tool-schema differences without changing tools.

The Databricks Gemini Chat bridge uses Google's OpenAPI Schema representation
for function parameters. That representation rejects JSON Schema's exclusive
numeric bounds, including the positive ``grep.max_count`` added by Deep Agents.
Only the wire schema is adapted: the original Pydantic tool validators still
enforce the original constraint when a tool is called.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .targets import TargetResolutionError

# Walk schema positions, never arbitrary dictionaries such as defaults or the
# property-name map itself. A user's property named "exclusiveMinimum" is data.
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_VALUES = (
    "items", "additionalProperties", "unevaluatedProperties", "propertyNames",
    "contains", "if", "then", "else", "not",
)


def _numeric(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool)
        or isinstance(value, float) and math.isfinite(value)
    )


def _exclusive_bound(schema: dict[str, Any], exclusive: str, inclusive: str) -> None:
    if exclusive not in schema:
        return
    value = schema.pop(exclusive)
    # Draft 4 expresses exclusivity as a boolean applying to minimum/maximum.
    if isinstance(value, bool):
        if not value:
            return
        value = schema.get(inclusive)
    if not _numeric(value):
        raise TargetResolutionError(
            f"Gemini Chat tool schema {exclusive} requires a finite numeric bound."
        )
    is_minimum = inclusive == "minimum"
    type_ = schema.get("type")
    integer = type_ == "integer" or (
        isinstance(type_, list) and "integer" in type_
        and all(item in {"integer", "null"} for item in type_)
    )
    # For integers this is an equivalent constraint. Arbitrary JSON numbers
    # have no exact inclusive successor, so retain the strict bound in the
    # description and use its inclusive bound as a wire-level approximation.
    bound = (math.floor(value) + 1 if is_minimum else math.ceil(value) - 1) if integer else value
    current = schema.get(inclusive)
    if current is not None and not _numeric(current):
        raise TargetResolutionError(
            f"Gemini Chat tool schema {inclusive} requires a finite numeric bound."
        )
    if current is None or (bound > current if is_minimum else bound < current):
        schema[inclusive] = bound
    relation = "greater than" if is_minimum else "less than"
    note = f"Must be strictly {relation} {value} ({exclusive}: {value})."
    description = schema.get("description")
    schema["description"] = f"{description}\n{note}" if description else note


def _adapt_gemini_schema(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    _exclusive_bound(schema, "exclusiveMinimum", "minimum")
    _exclusive_bound(schema, "exclusiveMaximum", "maximum")
    for keyword in _SCHEMA_MAPS:
        children = schema.get(keyword)
        if isinstance(children, dict):
            for child in children.values():
                _adapt_gemini_schema(child)
    for keyword in _SCHEMA_LISTS:
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                _adapt_gemini_schema(child)
    for keyword in _SCHEMA_VALUES:
        _adapt_gemini_schema(schema.get(keyword))


def adapt_chat_tool_schemas(payload: dict[str, Any], dialect: str | None) -> dict[str, Any]:
    """Adapt only the exact target/protocol's explicitly selected wire dialect.

    Responses and targets without a qualified dialect keep their schema. Never
    strip unknown keywords wholesale: a new constraint needs its own verified
    translation, while supported unions and metadata remain intact.
    """
    if dialect is None or "messages" not in payload or not payload.get("tools"):
        return payload
    if dialect != "gemini":
        raise TargetResolutionError(f"Unknown Chat tool_schema_dialect {dialect!r}.")
    result = copy.deepcopy(payload)
    for tool in result["tools"]:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            _adapt_gemini_schema(function.get("parameters"))
    return result
