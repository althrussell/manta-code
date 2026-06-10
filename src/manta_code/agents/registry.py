"""The Manta agent definition schema and its on-disk registry.

A Manta *agent* is a richer thing than a ``deepagents-code`` markdown subagent.
Where the upstream markdown loader keeps only ``name`` / ``description`` /
``model``, a Manta :class:`AgentDef` also carries the fields the ``deepagents``
SDK can actually *enforce* — tool allow/deny, filesystem read/write permissions,
per-tool approval, a model pin, skills, memory, and a token budget — so
"read-only" is a real boundary, not a sentence in a prompt.

Definitions live under ``~/.manta/agents/<name>/`` (honoring ``MANTA_HOME``):

- ``agent.toml`` — the structured definition (everything except the prompt).
- ``AGENTS.md``  — the system prompt body, kept as plain markdown so it is easy
  to hand-edit and diff.

This module is deliberately dependency-light (pydantic + stdlib ``tomllib`` for
reads; ``tomli_w`` is imported lazily only when saving) so the registry — and
the ``manta agents`` CLI built on it — works without the heavy ``agent`` extra
installed. Compiling a definition into a runtime ``SubAgent`` lives in
:mod:`manta_code.agents.factory`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..config import user_manta_dir

#: Agent names are slugs: lowercase alphanumerics plus single internal dashes.
#: This keeps them safe as directory names and as deepagents subagent ids.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Filesystem operations a permission rule can govern (matches deepagents'
#: ``FilesystemOperation`` literal).
FilesystemOp = Literal["read", "write"]


class FsRule(BaseModel):
    """One filesystem permission rule (compiled to ``FilesystemPermission``).

    Rules are evaluated in declaration order, first match wins — the same
    semantics the ``deepagents`` ``FilesystemMiddleware`` applies. ``deepagents``
    matches glob patterns against absolute paths and requires a leading ``/``,
    so :meth:`_normalize_paths` rewrites convenient relative patterns
    (``src/**``) into depth-anywhere absolute globs (``/**/src/**``) and leaves
    already-absolute patterns (``/tmp/**``) untouched.
    """

    operations: list[FilesystemOp]
    paths: list[str] = Field(default_factory=lambda: ["/**"])
    mode: Literal["allow", "deny"] = "allow"

    @field_validator("paths")
    @classmethod
    def _normalize_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for path in value:
            if path.startswith("/"):
                normalized.append(path)
            else:
                # Relative pattern -> match it at any directory depth.
                normalized.append("/**/" + path.lstrip("/"))
        return normalized


class AgentDef(BaseModel):
    """A complete Manta agent definition.

    ``system_prompt`` is persisted separately in ``AGENTS.md`` (not in
    ``agent.toml``) so it stays a readable markdown body.
    """

    name: str
    description: str = ""
    #: ``provider:endpoint`` (e.g. ``databricks:databricks-claude-opus-4-8``) or
    #: ``None`` to inherit the orchestrator's model.
    model: str | None = None
    system_prompt: str = ""

    #: Convenience switch: when true the factory denies all filesystem writes
    #: and gates/blocks mutating tools, giving a provably read-only agent.
    read_only: bool = False

    #: When set, only these tool names are allowed (an allow-list). ``None``
    #: means "inherit every tool the orchestrator has".
    tools_allow: list[str] | None = None
    #: Tool names explicitly denied (a deny-list, applied after any allow-list).
    tools_deny: list[str] = Field(default_factory=list)

    #: Explicit filesystem rules, in addition to whatever ``read_only`` implies.
    filesystem: list[FsRule] = Field(default_factory=list)

    #: Tool names that must pause for human approval (HITL) before running.
    approval: list[str] = Field(default_factory=list)

    #: Skill source paths for this agent (passed to ``SkillsMiddleware``).
    skills: list[str] = Field(default_factory=list)

    #: Whether this agent gets its own durable memory namespace.
    memory: bool = True
    #: Memory namespace; defaults to the agent name when omitted.
    memory_namespace: str | None = None

    #: Per-task token / dollar caps (enforced by the token-economy middleware).
    budget_max_tokens: int | None = None
    budget_max_usd: float | None = None

    #: Named Databricks-native tools to enable for this agent (e.g.
    #: ``["uc_catalog", "sql", "jobs", "system_tables"]``).
    databricks_tools: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"invalid agent name {value!r}: use lowercase letters, digits, "
                "and single dashes (e.g. 'data-reviewer')"
            )
        return value

    def effective_namespace(self) -> str:
        """Return the memory namespace, defaulting to the agent name."""
        return self.memory_namespace or self.name


def is_valid_name(name: str) -> bool:
    """Return ``True`` if ``name`` is a valid agent slug."""
    return bool(_NAME_RE.match(name))


def agents_root(root: Path | None = None) -> Path:
    """Return the registry root (``<root>/agents`` or ``~/.manta/agents``)."""
    base = root if root is not None else user_manta_dir()
    return base / "agents"


def agent_dir(name: str, *, root: Path | None = None) -> Path:
    """Return the directory holding one agent's files."""
    return agents_root(root) / name


def agent_toml_path(name: str, *, root: Path | None = None) -> Path:
    return agent_dir(name, root=root) / "agent.toml"


def agent_md_path(name: str, *, root: Path | None = None) -> Path:
    return agent_dir(name, root=root) / "AGENTS.md"


def agent_exists(name: str, *, root: Path | None = None) -> bool:
    return agent_toml_path(name, root=root).is_file()


def _to_toml_dict(defn: AgentDef) -> dict:
    """Serialize a definition to a TOML-friendly dict, minus the prompt.

    ``None`` values are dropped (TOML has no null) and the prompt is omitted
    because it is stored in ``AGENTS.md``.
    """
    data = defn.model_dump(mode="python", exclude_none=True)
    data.pop("system_prompt", None)
    return data


def save_agent(defn: AgentDef, *, root: Path | None = None) -> Path:
    """Persist a definition to ``agent.toml`` + ``AGENTS.md``. Returns the dir.

    Overwrites an existing agent of the same name (callers that need
    create-only semantics should check :func:`agent_exists` first).
    """
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "tomli-w is not installed. Install with: pip install -e '.[agent]'"
        ) from exc

    directory = agent_dir(defn.name, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    with agent_toml_path(defn.name, root=root).open("wb") as handle:
        tomli_w.dump(_to_toml_dict(defn), handle)
    agent_md_path(defn.name, root=root).write_text(
        defn.system_prompt.strip() + "\n", encoding="utf-8"
    )
    return directory


def load_agent(name: str, *, root: Path | None = None) -> AgentDef:
    """Load one agent definition (raises ``FileNotFoundError`` if absent)."""
    toml_path = agent_toml_path(name, root=root)
    if not toml_path.is_file():
        raise FileNotFoundError(f"no Manta agent named {name!r} ({toml_path})")
    with toml_path.open("rb") as handle:
        data = tomllib.load(handle)
    data.setdefault("name", name)
    md_path = agent_md_path(name, root=root)
    if md_path.is_file():
        data["system_prompt"] = md_path.read_text(encoding="utf-8").strip()
    return AgentDef.model_validate(data)


def list_agents(*, root: Path | None = None) -> list[AgentDef]:
    """Return all valid agent definitions, sorted by name.

    Malformed entries are skipped rather than aborting the whole listing so one
    bad file never hides every other agent.
    """
    base = agents_root(root)
    if not base.is_dir():
        return []
    defs: list[AgentDef] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "agent.toml").is_file():
            continue
        try:
            defs.append(load_agent(entry.name, root=root))
        except Exception:  # noqa: BLE001 - skip malformed, keep listing usable
            continue
    return defs


def delete_agent(name: str, *, root: Path | None = None) -> bool:
    """Delete an agent's directory. Returns ``True`` if something was removed."""
    directory = agent_dir(name, root=root)
    if not directory.is_dir():
        return False
    import shutil

    shutil.rmtree(directory)
    return True
