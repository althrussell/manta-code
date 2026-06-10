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
them into the *subagent* tier. Profiles are therefore **generated artifacts**:
they are refreshed from the registry on each launch (edits happen via
``manta agents edit``), so the two tiers never drift.

Safety rules:

- Each managed ``AGENTS.md`` carries :data:`MANTA_PROFILE_SENTINEL`. A profile
  dir whose ``AGENTS.md`` lacks the sentinel is a user/non-Manta profile and is
  **never** touched.
- The base ``agent`` profile (deepagents' default) is never managed.
- Profiles for agents no longer in the registry are pruned (only when still
  Manta-managed), so ``manta agents delete`` removes the profile too.

Everything is stdlib + the dependency-light registry, so it runs inside
``dcode`` without the heavy ``agent`` extra installed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: deepagents' default profile name; Manta never manages this one.
DEFAULT_AGENT_NAME = "agent"

#: ``~/.deepagents`` base (hard-coded upstream; mirrors ``dcode`` constants).
DEEPAGENTS_CONFIG_DIR = Path.home() / ".deepagents"
DEEPAGENTS_STATE_DIR = DEEPAGENTS_CONFIG_DIR / ".state"

#: Records the set of profile names Manta currently manages, so profiles for
#: deleted agents can be pruned on the next sync.
PROFILES_STATE_PATH = DEEPAGENTS_STATE_DIR / "manta_profiles.json"

#: One-time marker: the legacy prompt-only markdown subagents were cleaned up.
LEGACY_CLEANUP_MARKER = DEEPAGENTS_STATE_DIR / "manta_legacy_subagents_cleaned"

#: Marker written at the top of every Manta-managed profile ``AGENTS.md`` so a
#: refresh/prune can tell Manta-generated profiles from user-authored ones.
MANTA_PROFILE_SENTINEL = (
    "<!-- managed-by: manta-agents; do not edit by hand "
    "(use 'manta agents edit <name>') -->"
)

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


def render_profile_md(defn: Any) -> str:
    """Render an agent definition to its profile ``AGENTS.md`` body.

    The sentinel goes first (so refresh/prune can recognize it), followed by the
    agent's system prompt — which ``deepagents-code`` loads as the profile's
    instructions.
    """
    prompt = (getattr(defn, "system_prompt", "") or "").strip()
    return f"{MANTA_PROFILE_SENTINEL}\n\n{prompt}\n".rstrip() + "\n"


def _is_manta_managed(md_path: Path) -> bool:
    """Return ``True`` if ``md_path`` is a Manta-generated profile file."""
    try:
        return MANTA_PROFILE_SENTINEL in md_path.read_text(encoding="utf-8")
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
        json.dumps({"managed": sorted(set(names))}, indent=2) + "\n",
        encoding="utf-8",
    )


def sync_agent_profiles(
    agents: list[Any],
    *,
    deepagents_dir: Path | None = None,
    state_path: Path | None = None,
) -> ProfileSyncResult:
    """Project ``agents`` into top-level deepagents profile directories.

    Writes/refreshes ``<deepagents>/<name>/AGENTS.md`` for each agent from the
    registry (skipping the base ``agent`` profile and any existing non-Manta
    profile), prunes profiles for agents that are gone, and records the managed
    set for the next run. Idempotent; never raises on a single bad entry.
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
        md_path = agent_dir / "AGENTS.md"
        if md_path.exists() and not _is_manta_managed(md_path):
            # A user-authored / non-Manta profile of the same name: never clobber.
            result.skipped.append(name)
            continue
        content = render_profile_md(defn)
        try:
            agent_dir.mkdir(parents=True, exist_ok=True)
            if not md_path.exists() or md_path.read_text(encoding="utf-8") != content:
                md_path.write_text(content, encoding="utf-8")
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
                shutil.rmtree(agent_dir)
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
    """One-time removal of the legacy prompt-only markdown subagents.

    Manta used to write ``<deepagents>/<agent>/agents/<name>/AGENTS.md`` for
    ``planning`` / ``swe`` / ``review``. Those are now superseded by the enforced
    registry (build hook) and the profile tier, so they are redundant. This
    removes only the dirs whose ``AGENTS.md`` still matches Manta's generated
    content (detected via :data:`_LEGACY_SUBAGENT_SIGNATURES`); anything a user
    edited is left untouched. Gated by ``marker_path`` so it runs at most once.

    Returns the names removed (empty when already cleaned or nothing matched).
    """
    base = deepagents_dir or DEEPAGENTS_CONFIG_DIR
    marker = marker_path or LEGACY_CLEANUP_MARKER
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
                shutil.rmtree(legacy_dir)
                removed.append(name)
            except OSError:
                pass

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="utf-8")
    return removed
