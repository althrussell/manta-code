from __future__ import annotations

import copy

import pytest

from manta_code.providers.targets import TargetResolutionError
from manta_code.providers.tool_schema_compat import adapt_chat_tool_schemas


def _payload(schema):
    return {
        "messages": [{"role": "user", "content": "test"}],
        "tools": [{"type": "function", "function": {
            "name": "inspect", "description": "Inspect input", "parameters": schema,
        }}],
    }


def _schema(payload):
    return payload["tools"][0]["function"]["parameters"]


@pytest.mark.parametrize(("exclusive", "bound", "keyword", "expected"), [
    ("exclusiveMinimum", 0, "minimum", 1),
    ("exclusiveMinimum", -1.5, "minimum", -1),
    ("exclusiveMinimum", 2.7, "minimum", 3),
    ("exclusiveMaximum", 5, "maximum", 4),
    ("exclusiveMaximum", -1.5, "maximum", -2),
    ("exclusiveMaximum", 2.7, "maximum", 2),
])
def test_integer_exclusive_bounds_are_equivalent(exclusive, bound, keyword, expected):
    payload = _payload({"type": "integer", exclusive: bound, "description": "Original."})
    before = copy.deepcopy(payload)
    result = adapt_chat_tool_schemas(payload, "gemini")
    schema = _schema(result)
    assert schema[keyword] == expected
    assert exclusive not in schema
    assert schema["description"].startswith("Original.\n")
    assert f"{exclusive}: {bound}" in schema["description"]
    assert payload == before


def test_numeric_bounds_keep_strict_constraint_in_description_and_stronger_bounds():
    payload = _payload({
        "type": "number", "exclusiveMinimum": 0.5, "exclusiveMaximum": 2.5,
        "minimum": 1, "maximum": 2, "description": "A bounded value.",
    })
    result = _schema(adapt_chat_tool_schemas(payload, "gemini"))
    assert result["minimum"] == 1
    assert result["maximum"] == 2
    assert "strictly greater than 0.5" in result["description"]
    assert "strictly less than 2.5" in result["description"]


def test_number_without_exact_inclusive_successor_keeps_original_bound():
    schema = _schema(adapt_chat_tool_schemas(_payload({
        "type": "number", "exclusiveMinimum": 0.5,
    }), "gemini"))
    assert schema["minimum"] == 0.5
    assert "strictly greater than 0.5" in schema["description"]


def test_draft_four_exclusivity_is_preserved():
    schema = _schema(adapt_chat_tool_schemas(_payload({
        "type": "integer", "minimum": 2, "exclusiveMinimum": True,
        "maximum": 8, "exclusiveMaximum": False,
    }), "gemini"))
    assert schema["minimum"] == 3
    assert schema["maximum"] == 8
    assert "exclusiveMinimum" not in schema
    assert "exclusiveMaximum" not in schema


@pytest.mark.parametrize("bound", [None, "0", float("inf"), float("nan")])
def test_invalid_exclusive_bound_fails_locally(bound):
    with pytest.raises(TargetResolutionError, match="finite numeric bound"):
        adapt_chat_tool_schemas(_payload({"type": "number", "exclusiveMinimum": bound}), "gemini")


def test_schema_traversal_preserves_union_metadata_property_names_and_data_values():
    schema = {
        "type": "object", "description": "Root.", "required": ["exclusiveMinimum"],
        "properties": {
            "exclusiveMinimum": {
                "anyOf": [{"type": "integer", "exclusiveMinimum": 0}, {"type": "null"}],
                "description": "Optional positive count.", "default": None,
            },
            "properties": {"type": "string", "enum": ["exclusiveMinimum", "anyOf"]},
            "payload": {
                "type": "object", "default": {"exclusiveMinimum": 123},
                "examples": [{"exclusiveMaximum": 456}],
                "additionalProperties": {"type": "integer", "exclusiveMaximum": 5},
            },
        },
    }
    result = _schema(adapt_chat_tool_schemas(_payload(schema), "gemini"))
    count = result["properties"]["exclusiveMinimum"]
    assert count["anyOf"][0]["minimum"] == 1
    assert count["anyOf"][1] == {"type": "null"}
    assert count["description"] == "Optional positive count."
    assert count["default"] is None
    assert result["required"] == ["exclusiveMinimum"]
    assert result["properties"]["properties"] == schema["properties"]["properties"]
    data = result["properties"]["payload"]
    assert data["default"] == {"exclusiveMinimum": 123}
    assert data["examples"] == [{"exclusiveMaximum": 456}]
    assert data["additionalProperties"]["maximum"] == 4


def test_only_explicit_chat_dialect_changes_payload():
    payload = _payload({"type": "integer", "exclusiveMinimum": 0})
    assert adapt_chat_tool_schemas(payload, None) is payload
    responses = {"input": [], "tools": payload["tools"]}
    assert adapt_chat_tool_schemas(responses, "gemini") is responses
    with pytest.raises(TargetResolutionError, match="Unknown Chat tool_schema_dialect"):
        adapt_chat_tool_schemas(payload, "guess")


def test_harness_grep_wire_schema_keeps_runtime_validation():
    pytest.importorskip("deepagents")
    pytest.importorskip("langchain_openai")
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain_core.messages import HumanMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from pydantic import ValidationError

    from manta_code.databricks_chat import MantaChatDatabricks

    grep = next(tool for tool in FilesystemMiddleware().tools if tool.name == "grep")
    before = copy.deepcopy(convert_to_openai_tool(grep))
    model = MantaChatDatabricks(
        model="system.ai.schema-fixture", api="chat", profile={"tool_schema_dialect": "gemini"},
    )
    bound = model.bind_tools([grep])
    payload = model._get_request_payload([HumanMessage(content="Search for test")], **bound.kwargs)
    count = _schema(payload)["properties"]["max_count"]
    assert count["anyOf"][0]["minimum"] == 1
    assert "exclusiveMinimum" not in count["anyOf"][0]
    assert count["anyOf"][1] == {"type": "null"}
    assert count["description"] == before["function"]["parameters"]["properties"]["max_count"]["description"]
    assert convert_to_openai_tool(grep) == before
    with pytest.raises(ValidationError):
        grep.args_schema.model_validate({"pattern": "test", "max_count": 0})
    assert grep.args_schema.model_validate({"pattern": "test", "max_count": 1}).max_count == 1


def test_model_name_does_not_enable_schema_dialect():
    pytest.importorskip("langchain_openai")
    from langchain_core.messages import HumanMessage

    from manta_code.databricks_chat import MantaChatDatabricks

    tool = _payload({"type": "integer", "exclusiveMinimum": 0})["tools"][0]
    model = MantaChatDatabricks(model="private-gemini-4-argon", api="chat")
    payload = model._get_request_payload([HumanMessage(content="test")], tools=[tool])
    assert _schema(payload)["exclusiveMinimum"] == 0
