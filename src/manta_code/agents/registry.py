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

import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import user_manta_dir

#: Agent names are slugs: lowercase alphanumerics plus single internal dashes.
#: This keeps them safe as directory names and as deepagents subagent ids.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Filesystem operations a permission rule can govern (matches deepagents'
#: ``FilesystemOperation`` literal).
FilesystemOp = Literal["read", "write"]


class FsRule(BaseModel):
    """One filesystem path rule (enforced by ``ToolPolicyMiddleware``).

    Rules are evaluated in declaration order, first match wins — the same
    semantics the ``deepagents`` ``FilesystemMiddleware`` applies. Manta enforces
    them in its tool-policy ``wrap_tool_call`` (not via ``FilesystemPermission``,
    which the execute-capable sandbox backend rejects), matching glob patterns
    against the absolute ``file_path`` argument. Patterns require a leading
    ``/``, so :meth:`_normalize_paths` rewrites convenient relative patterns
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

    ``system_prompt`` is persisted separately in ``MANTA_ROLE.md`` (not in
    ``agent.toml``); ``AGENTS.md`` holds the agent's mutable learnings.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    context_mode: Literal["isolated", "fork"] = "isolated"
    description: str = ""
    #: ``provider:endpoint`` (e.g. ``databricks:databricks-claude-opus-4-8``) or
    #: ``None`` to inherit the orchestrator's model.
    model: str | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    #: Detached execution is shell-disabled unless commands are explicitly
    #: enabled here. Interactive shell availability follows launch settings.
    task_shell_allow_list: list[str] = Field(default_factory=list)
    system_prompt: str = ""

    #: Tool policy blocks filesystem mutations, execution and unknown effects.
    #: This is a harness boundary; it does not provide an OS/process sandbox.
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

    #: Tool names requiring a human decision at Manta's policy layer (ADR
    #: 0011). Interactively this rides the same HITL prompt as ``approval``;
    #: the difference is unattended behaviour: ask-gated tools **fail closed**
    #: in background tasks / headless runs (upstream HITL auto-approves)
    #: unless the submitter pre-approved with ``--allow-asks``.
    tools_ask: list[str] = Field(default_factory=list)

    #: Skill source paths for this agent (passed to ``SkillsMiddleware``).
    skills: list[str] = Field(default_factory=list)

    #: Whether this agent gets its own durable memory namespace.
    memory: bool = True
    #: Memory namespace; defaults to the agent name when omitted.
    memory_namespace: str | None = None

    #: Per-task token / dollar caps (enforced by the token-economy middleware).
    budget_max_tokens: int | None = Field(default=None, gt=0)
    budget_max_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    #: Named Databricks-native tools to enable for this agent (e.g.
    #: ``["uc_catalog", "sql", "jobs", "system_tables"]``).
    databricks_tools: list[str] = Field(default_factory=list)

    #: Named Manta control-plane tool groups for this agent. ``"tasks"``
    #: grants the background-task tools (submit/status/output/list/cancel) —
    #: what the ``chief`` built-in uses to delegate and collect.
    manta_tools: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"invalid agent name {value!r}: use lowercase letters, digits, "
                "and single dashes (e.g. 'data-reviewer')"
            )
        return value

    @model_validator(mode="after")
    def _validate_context(self) -> "AgentDef":
        if self.context_mode == "fork" and self.skills:
            raise ValueError("fork context inherits the parent's skills; remove explicit skills")
        return self

    @field_validator("task_shell_allow_list")
    @classmethod
    def _validate_task_shell(cls, values: list[str]) -> list[str]:
        values = [value.strip() for value in values]
        if any(not value or any(char in value for char in (",", "\n", "\r", "\x00")) for value in values):
            raise ValueError("task shell commands must be nonempty single-line entries without commas")
        if any(value.lower() == "all" for value in values) and values != ["all"]:
            raise ValueError("use ['all'] alone to allow all task shell commands")
        if any(value.lower() == "recommended" for value in values):
            raise ValueError("list explicit task shell commands instead of the upstream 'recommended' expansion")
        return list(dict.fromkeys(values))

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
    if not is_valid_name(name):
        raise ValueError(f"invalid agent name: {name!r}")
    return agents_root(root) / name


def agent_toml_path(name: str, *, root: Path | None = None) -> Path:
    return agent_dir(name, root=root) / "agent.toml"


def agent_md_path(name: str, *, root: Path | None = None) -> Path:
    return agent_dir(name, root=root) / "AGENTS.md"


def agent_role_path(name: str, *, root: Path | None = None) -> Path:
    """Role projection; edit through the service to commit a coherent revision."""
    return agent_dir(name, root=root) / "MANTA_ROLE.md"


def definition_revision(defn: AgentDef) -> str:
    """Stable identity for optimistic edits and runtime reload detection."""
    return hashlib.sha256(json.dumps(defn.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def agent_exists(name: str, *, root: Path | None = None) -> bool:
    return agent_toml_path(name, root=root).is_file()


def _to_toml_dict(defn: AgentDef) -> dict:
    """Serialize a definition to a TOML-friendly dict, minus the prompt.

    ``None`` values are dropped (TOML has no null) and the prompt is omitted
    because it is stored in the immutable role file committed by TOML pointer.
    """
    data = defn.model_dump(mode="python", exclude_none=True)
    data.pop("system_prompt", None)
    return data


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_agent(defn: AgentDef, *, root: Path | None = None,
               create_only: bool = False, expected_revision: str | None = None) -> Path:
    """Persist a coherent definition while preserving learned context.

    Overwrites an existing agent of the same name (callers that need
    create-only semantics should use ``create_only=True``).
    """
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        from ..installation import runtime_install_command

        raise RuntimeError(
            f"tomli-w is not installed. Install with: {runtime_install_command()}"
        ) from exc

    from ..state_lock import state_lock

    # Revalidate model_copy updates too; Pydantic does not validate those.
    defn = AgentDef.model_validate(defn.model_dump())
    directory = agent_dir(defn.name, root=root)
    with state_lock(agents_root(root) / ".registry.lock"):
        if create_only and agent_exists(defn.name, root=root):
            raise FileExistsError(f"agent {defn.name!r} already exists")
        if expected_revision is not None:
            current = load_agent(defn.name, root=root) if agent_exists(defn.name, root=root) else None
            if current is None or definition_revision(current) != expected_revision:
                raise ValueError(f"agent {defn.name!r} changed; reload before saving")
        directory.mkdir(parents=True, exist_ok=True)
        prompt = (defn.system_prompt.strip() + "\n").encode()
        digest = hashlib.sha256(prompt).hexdigest()
        role_file = f".roles/{digest}.md"
        (directory / ".roles").mkdir(exist_ok=True)
        _atomic_write(directory / role_file, prompt)
        _atomic_write(agent_role_path(defn.name, root=root), prompt)
        # Legacy mixed role/context stays intact, including user additions.
        memory_path = agent_md_path(defn.name, root=root)
        if not memory_path.exists():
            _atomic_write(memory_path, b"# Learned context\n\n")
        data = {**_to_toml_dict(defn), "role_file": role_file}
        # The TOML pointer is the commit point: readers see the complete old
        # definition or complete new definition, never mismatched model/prompt.
        _atomic_write(agent_toml_path(defn.name, root=root), tomli_w.dumps(data).encode())
    return directory


def load_agent(name: str, *, root: Path | None = None) -> AgentDef:
    """Load one agent definition (raises ``FileNotFoundError`` if absent)."""
    toml_path = agent_toml_path(name, root=root)
    if not toml_path.is_file():
        raise FileNotFoundError(f"no Manta agent named {name!r} ({toml_path})")
    with toml_path.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("name", name) != name:
        raise ValueError(f"agent directory {name!r} disagrees with its declared name")
    data.setdefault("name", name)
    role_file = data.pop("role_file", None)
    if role_file is not None:
        if not re.fullmatch(r"\.roles/[0-9a-f]{64}\.md", role_file):
            raise ValueError(f"invalid role_file for agent {name!r}")
        md_path = agent_dir(name, root=root) / role_file
        if not md_path.is_file():
            raise ValueError(f"missing role instructions for agent {name!r}")
    else:
        md_path = agent_role_path(name, root=root)
        if not md_path.is_file():
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
