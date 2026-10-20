"""Generate top-level ``deepagents-code`` agent *profiles* from the registry.

``deepagents-code`` has two distinct agent tiers:

1. **Profiles** — a directory ``~/.deepagents/<name>/`` with an ``AGENTS.md``.
   These are the entries the in-app ``/agents`` picker lists; selecting one
   restarts the session with that profile as the *primary* agent.
2. **Subagents** — delegation targets the orchestrator calls via the ``task``
   tool (compiled + enforced by the build hook from the same registry).

Manta's registry (``~/.manta/agents/``) is the single source of truth for both.
This module projects every registry agent (built-ins + user-created) into the
*profile* tier so the picker shows them, while the build hook keeps projecting
them into the *subagent* tier. Each profile separates generated ``MANTA_ROLE.md``
from mutable learned context in ``AGENTS.md``. The hook injects the registry
role into the primary agent, and profile sync preserves learned context.

Safety rules:

- Managed learned context carries :data:`MANTA_MEMORY_SENTINEL`. Legacy mixed
  role/memory files carry :data:`MANTA_PROFILE_SENTINEL`; they are backed up
  before splitting. A profile lacking both sentinels is never touched.
- The base ``agent`` profile (deepagents' default) is never managed.
- Profiles for deleted agents are archived in full, preserving memory/skills
  while removing the agent from the picker.

Everything is stdlib + the dependency-light registry, so it runs inside
``dcode`` without the heavy ``agent`` extra installed.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: deepagents' default profile name; Manta never manages this one.
DEFAULT_AGENT_NAME = "agent"

#: Mirrors the upstream profile selection and ``dcode`` constants.
DEEPAGENTS_CONFIG_DIR = Path(os.environ.get("DEEPAGENTS_HOME", Path.home() / ".deepagents")).expanduser()
DEEPAGENTS_STATE_DIR = DEEPAGENTS_CONFIG_DIR / ".state"

#: Records the set of profile names Manta currently manages, so profiles for
#: deleted agents can be archived on the next sync.
PROFILES_STATE_PATH = DEEPAGENTS_STATE_DIR / "manta_profiles.json"

#: One-time marker: the legacy prompt-only markdown subagents were cleaned up.
LEGACY_CLEANUP_MARKER = DEEPAGENTS_STATE_DIR / "manta_legacy_subagents_cleaned"

#: Legacy marker retained for migration and generated role attribution.
MANTA_PROFILE_SENTINEL = (
    "<!-- managed-by: manta-agents; do not edit by hand "
    "(use 'manta agents edit <name>') -->"
)
ROLE_FILENAME = "MANTA_ROLE.md"
MANTA_MEMORY_SENTINEL = "<!-- manta-learned-context: user-editable; preserved on sync -->"
MEMORY_STARTER = f"{MANTA_MEMORY_SENTINEL}\n\n# Learned context\n\n"

#: First-line signatures of the legacy markdown subagents Manta used to write,
#: keyed by name. Used by :func:`clean_legacy_subagents` to remove only
#: unmodified Manta-generated files and leave anything a user edited.
_LEGACY_SUBAGENT_SIGNATURES: dict[str, str] = {
    "planning": "You are Manta's planning specialist",
    "swe": "You are Manta's software-engineering subagent",
    "review": "You are Manta's code-review subagent",
}


@dataclass
class ProfileSyncResult:
    """Summary of a :func:`sync_agent_profiles` run."""

    written: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    migrated: list[str] = field(default_factory=list)


def render_profile_md(defn: Any) -> str:
    """Render the generated role file; mutable ``AGENTS.md`` is kept separate."""
    prompt = (getattr(defn, "system_prompt", "") or "").strip()
    return f"{MANTA_PROFILE_SENTINEL}\n\n{prompt}\n".rstrip() + "\n"


def _is_manta_managed(md_path: Path) -> bool:
    """Return ``True`` if ``md_path`` is a Manta-generated profile file."""
    try:
        text = md_path.read_text(encoding="utf-8")
        return MANTA_PROFILE_SENTINEL in text or MANTA_MEMORY_SENTINEL in text
    except OSError:
        return False


def _read_state(state_path: Path) -> list[str]:
    """Return the previously-managed profile names (empty on any error)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("managed") if isinstance(data, dict) else data
    if isinstance(names, list):
        return [str(n) for n in names]
    return []


def _write_state(state_path: Path, names: list[str]) -> None:
    """Persist the set of currently-managed profile names."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"schema_version": 2, "managed": sorted(set(names))}, indent=2) + "\n",
        encoding="utf-8",
    )


def _archive_legacy_memory(md_path: Path, state_path: Path) -> None:
    """Snapshot old mixed role/memory as unassigned data, never infer its scope."""
    content = md_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    directory = state_path.parent / "manta-profile-backups" / md_path.parent.name / digest
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / "AGENTS.md"
    if not backup.exists():
        backup.write_bytes(content)
        backup.chmod(0o600)
    provenance = directory / "provenance.json"
    if not provenance.exists():
        provenance.write_text(json.dumps({
            "schema_version": 2, "scope": "legacy-unassigned",
            "source_path": str(md_path), "sha256": digest,
            "agent": md_path.parent.name, "migrated_at": time.time(),
        }, indent=2) + "\n", encoding="utf-8")


def _write_atomic(path: Path, text: str) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_agent_profiles(
    agents: list[Any],
    *,
    deepagents_dir: Path | None = None,
    state_path: Path | None = None,
) -> ProfileSyncResult:
    """Synchronize generated roles without racing headless/TUI launches."""
    from ..state_lock import state_lock

    state = state_path or PROFILES_STATE_PATH
    state.parent.mkdir(parents=True, exist_ok=True)
    with state_lock(state.parent / ".manta-profiles.lock"):
        return _sync_agent_profiles(agents, deepagents_dir=deepagents_dir, state_path=state)


def _sync_agent_profiles(
    agents: list[Any], *, deepagents_dir: Path | None = None, state_path: Path | None = None,
) -> ProfileSyncResult:
    """Project ``agents`` into top-level deepagents profile directories.

    Writes generated roles and initializes mutable context for registry agents
    (skipping the base ``agent`` and existing non-Manta profiles), archives
    deleted profiles, and records the managed set for the next run. Idempotent;
    filesystem failures are reported in the skipped set.
    """
    base = deepagents_dir or DEEPAGENTS_CONFIG_DIR
    state = state_path or PROFILES_STATE_PATH
    result = ProfileSyncResult()

    desired = {
        a.name: a for a in agents if getattr(a, "name", None) and a.name != DEFAULT_AGENT_NAME
    }

    managed_now: list[str] = []
    for name, defn in desired.items():
        agent_dir = base / name
        if not agent_dir.exists():
            # Disabling removes a profile from the upstream picker by
            # archiving it. Re-enabling restores that same learned context.
            archive = state.parent / "manta-profile-archive"
            candidates = sorted((path for path in archive.glob(f"{name}-*")
                                 if re.fullmatch(re.escape(name) + r"-\d+", path.name)),
                                key=lambda path: path.name, reverse=True)
            for candidate in candidates:
                if candidate.is_dir() and _is_manta_managed(candidate / "AGENTS.md"):
                    try:
                        candidate.rename(agent_dir)
                    except OSError:
                        pass
                    break
        md_path = agent_dir / "AGENTS.md"
        if md_path.exists() and not _is_manta_managed(md_path):
            # A user-authored / non-Manta profile of the same name: never clobber.
            result.skipped.append(name)
            continue
        content = render_profile_md(defn)
        role_path = agent_dir / ROLE_FILENAME
        try:
            agent_dir.mkdir(parents=True, exist_ok=True)
            # Legacy profiles mixed generated role text and mutable learnings.
            # Archive the complete file before splitting; never guess which
            # lines belonged to a project or silently promote them to global.
            if md_path.exists() and MANTA_PROFILE_SENTINEL in md_path.read_text(encoding="utf-8"):
                _archive_legacy_memory(md_path, state)
                _write_atomic(md_path, MEMORY_STARTER)
                result.migrated.append(name)
            if not md_path.exists():
                _write_atomic(md_path, MEMORY_STARTER)
            if not role_path.exists() or role_path.read_text(encoding="utf-8") != content:
                _write_atomic(role_path, content)
                result.written.append(name)
            managed_now.append(name)
        except OSError:
            result.skipped.append(name)

    for name in _read_state(state):
        if name in desired or name == DEFAULT_AGENT_NAME:
            continue
        agent_dir = base / name
        md_path = agent_dir / "AGENTS.md"
        if md_path.exists() and _is_manta_managed(md_path):
            try:
                # Deleting an agent must not delete its learned context or
                # skills. Move the full profile out of the upstream picker.
                archive = state.parent / "manta-profile-archive"
                archive.mkdir(parents=True, exist_ok=True)
                destination = archive / f"{name}-{time.time_ns()}"
                agent_dir.rename(destination)
                result.pruned.append(name)
            except OSError:
                pass

    _write_state(state, managed_now)
    return result


def clean_legacy_subagents(
    *,
    deepagents_dir: Path | None = None,
    marker_path: Path | None = None,
    base_agent: str = DEFAULT_AGENT_NAME,
) -> list[str]:
    """One-time archival of the legacy prompt-only markdown subagents.

    Manta used to write ``<deepagents>/<agent>/agents/<name>/AGENTS.md`` for
    ``planning`` / ``swe`` / ``review``. Those are now superseded by the enforced
    registry (build hook) and the profile tier, so they are redundant. This
    archives dirs whose ``AGENTS.md`` contains Manta's generated signature,
    preserving the complete directory including any learned additions. Other
    user profiles are left untouched. Gated by ``marker_path``.

    Returns the names removed (empty when already cleaned or nothing matched).
    """
    base = deepagents_dir or DEEPAGENTS_CONFIG_DIR
    marker = marker_path or LEGACY_CLEANUP_MARKER
    from ..state_lock import state_lock

    with state_lock(marker.with_name(marker.name + ".lock")):
        return _clean_legacy_subagents(base, marker, base_agent)


def _clean_legacy_subagents(base: Path, marker: Path, base_agent: str) -> list[str]:
    if marker.exists():
        return []

    removed: list[str] = []
    agents_dir = base / base_agent / "agents"
    for name, signature in _LEGACY_SUBAGENT_SIGNATURES.items():
        legacy_dir = agents_dir / name
        md_path = legacy_dir / "AGENTS.md"
        if not md_path.is_file():
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if signature in text:
            try:
                archive = marker.parent / "manta-legacy-subagent-archive"
                archive.mkdir(parents=True, exist_ok=True)
                legacy_dir.rename(archive / f"{name}-{time.time_ns()}")
                removed.append(name)
            except OSError:
                pass

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="utf-8")
    return removed
