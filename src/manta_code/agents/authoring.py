"""Draft a Manta :class:`AgentDef` from a plain-English description.

``manta agents create --describe "..."`` lets a user describe an agent in
English rather than hand-editing TOML; Manta drafts a definition they then
confirm/edit. The CLI runs offline (no model call), so this drafter is a
transparent, deterministic heuristic: it infers read-only intent, a sensible
Databricks model pin, and Databricks tool scopes from keywords, and scaffolds a
system prompt. The user reviews and tunes the result with ``manta agents edit``.

Keeping it deterministic (not an LLM call) makes ``create`` fast, offline, and
predictable; the in-session agent can always rewrite the prompt afterward.
"""

from __future__ import annotations

import re

from .defaults import _model
from .registry import AgentDef

#: Keyword -> signal mappings. Order independent; first matching model wins.
_READ_ONLY_HINTS = (
    "review",
    "audit",
    "read-only",
    "read only",
    "inspect",
    "analyze",
    "analyse",
    "explain",
    "investigate",
    "summarize",
    "summarise",
)
_PLANNING_HINTS = ("plan", "design", "architect", "scope", "break down", "roadmap")
_DATA_HINTS = ("sql", "query", "unity catalog", "lakehouse", "table", "warehouse", "lineage")


def _infer_read_only(text: str) -> bool:
    return any(hint in text for hint in _READ_ONLY_HINTS)


def _infer_model(text: str) -> str:
    if any(hint in text for hint in _PLANNING_HINTS):
        return _model("databricks-claude-opus-4-8")
    if any(hint in text for hint in _READ_ONLY_HINTS):
        return _model("databricks-claude-sonnet-4-5")
    return _model("databricks-gpt-5-4")


def _infer_databricks_tools(text: str) -> list[str]:
    tools: list[str] = []
    if any(hint in text for hint in _DATA_HINTS):
        tools.extend(["uc_catalog", "sql"])
    if any(hint in text for hint in ("job", "pipeline", "deploy", "dab", "bundle")):
        tools.append("jobs")
    if any(hint in text for hint in ("cost", "billing", "usage", "spend")):
        tools.append("system_tables")
    # Preserve order, dedupe.
    return list(dict.fromkeys(tools))


def _scaffold_prompt(description: str, *, read_only: bool) -> str:
    role = description.strip().rstrip(".") or "a focused engineering assistant"
    discipline = (
        "This is a READ-ONLY role and it is enforced: file writes and shell "
        "execution are blocked. If asked to change something, describe the "
        "change instead of applying it."
        if read_only
        else "Verify your work: run the relevant tests/queries and self-correct "
        "on failure. Do not claim success you have not checked, and admit "
        "uncertainty rather than guessing."
    )
    return (
        f"You are {role}, working in a Databricks-focused engineering environment.\n\n"
        f"{discipline}\n\n"
        "Ground everything you do in the actual code and data — read before you "
        "act, cite specifics, and keep changes minimal and well-scoped."
    )


def slugify(text: str) -> str:
    """Turn free text into a valid agent slug (best effort)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "agent"


def draft_agent_from_description(name: str, description: str) -> AgentDef:
    """Draft an :class:`AgentDef` from ``description`` for the user to confirm."""
    text = description.lower()
    read_only = _infer_read_only(text)
    return AgentDef(
        name=name,
        description=description.strip(),
        model=_infer_model(text),
        system_prompt=_scaffold_prompt(description, read_only=read_only),
        read_only=read_only,
        databricks_tools=_infer_databricks_tools(text),
        memory=True,
    )
