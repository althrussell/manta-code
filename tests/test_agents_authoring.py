from __future__ import annotations

from manta_code.agents.authoring import draft_agent_from_description, slugify


def test_review_description_infers_read_only_and_gemini():
    defn = draft_agent_from_description("auditor", "review pull requests for security issues")
    assert defn.read_only is True
    assert defn.model == "databricks:databricks-claude-sonnet-4-5"
    assert "READ-ONLY" in defn.system_prompt


def test_planning_description_infers_opus():
    defn = draft_agent_from_description("architect", "design and plan a migration")
    assert defn.model == "databricks:databricks-claude-opus-4-8"


def test_data_description_infers_databricks_tools():
    defn = draft_agent_from_description("analyst", "analyze SQL queries over our lakehouse tables")
    assert "uc_catalog" in defn.databricks_tools
    assert "sql" in defn.databricks_tools
    assert defn.read_only is True  # "analyze" is a read-only hint


def test_default_is_read_write_coder():
    defn = draft_agent_from_description("builder", "implement features end to end")
    assert defn.read_only is False
    assert defn.model == "databricks:databricks-gpt-5-5"
    assert "Verify your work" in defn.system_prompt


def test_slugify():
    assert slugify("Data Reviewer!") == "data-reviewer"
    assert slugify("") == "agent"
