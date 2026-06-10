"""Import existing agent config to cut Manta's switching cost.

Adoption dies if a user has to re-author everything. This module reads the
artifacts other coding agents leave in a repo and folds them into Manta:

- ``CLAUDE.md`` / ``AGENTS.md`` (repo root) -> the system prompt of a Manta
  agent, so the project's house rules carry over.
- ``.cursor/rules/*.md`` and ``*.mdc`` -> appended to that same prompt (their
  YAML frontmatter, if any, is stripped).
- ``.mcp.json`` (repo root) -> its servers are merged into the user's
  ``~/.deepagents/.mcp.json`` so the same MCP tools are available in Manta.

Everything here is pure and filesystem-scoped so it is unit-testable with temp
dirs; the ``manta agents import`` command is a thin wrapper.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .registry import AgentDef, save_agent

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


@dataclass
class ImportReport:
    """What an import run found and did."""

    claude_md: Path | None = None
    cursor_rules: list[Path] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    agent_created: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def imported_anything(self) -> bool:
        return bool(self.claude_md or self.cursor_rules or self.mcp_servers)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def find_claude_md(root: Path) -> Path | None:
    """Return the project memory file (``CLAUDE.md`` or ``AGENTS.md``), if any."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def find_cursor_rules(root: Path) -> list[Path]:
    """Return ``.cursor/rules/*.md`` / ``*.mdc`` files, sorted."""
    rules_dir = root / ".cursor" / "rules"
    if not rules_dir.is_dir():
        return []
    rules = [
        p
        for p in sorted(rules_dir.rglob("*"))
        if p.is_file() and p.suffix in (".md", ".mdc")
    ]
    return rules


def build_imported_prompt(root: Path) -> tuple[str, ImportReport]:
    """Aggregate CLAUDE.md/AGENTS.md + cursor rules into one system prompt."""
    report = ImportReport()
    parts: list[str] = []

    claude_md = find_claude_md(root)
    if claude_md is not None:
        report.claude_md = claude_md
        parts.append(f"# Imported from {claude_md.name}\n\n" + claude_md.read_text(encoding="utf-8").strip())

    rules = find_cursor_rules(root)
    for rule in rules:
        report.cursor_rules.append(rule)
        body = _strip_frontmatter(rule.read_text(encoding="utf-8"))
        if body:
            parts.append(f"# Imported from .cursor/rules/{rule.name}\n\n{body}")

    return ("\n\n---\n\n".join(parts).strip(), report)


def import_mcp_servers(root: Path, dest_config_dir: Path) -> list[str]:
    """Merge ``<root>/.mcp.json`` servers into ``<dest>/.mcp.json``.

    Non-clobbering: existing servers in the destination are kept; only new
    server names from the source are added. Returns the names that were added.
    """
    src = root / ".mcp.json"
    if not src.is_file():
        return []
    try:
        src_data = json.loads(src.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    src_servers = src_data.get("mcpServers", {})
    if not isinstance(src_servers, dict) or not src_servers:
        return []

    dest = dest_config_dir / ".mcp.json"
    dest_data: dict = {}
    if dest.is_file():
        try:
            dest_data = json.loads(dest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            dest_data = {}
    dest_servers = dest_data.setdefault("mcpServers", {})
    if not isinstance(dest_servers, dict):
        return []

    added: list[str] = []
    for name, spec in src_servers.items():
        if name not in dest_servers:
            dest_servers[name] = spec
            added.append(name)
    if added:
        dest_config_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(dest_data, indent=2) + "\n", encoding="utf-8")
    return sorted(added)


def import_sources(
    root: Path,
    *,
    dest_root: Path | None = None,
    dest_config_dir: Path,
    agent_name: str = "imported",
) -> ImportReport:
    """Import CLAUDE.md + cursor rules + MCP servers; return a report.

    Creates a Manta agent named ``agent_name`` whose prompt is the aggregated
    project rules (only when there is content to import), and merges any MCP
    servers into ``dest_config_dir/.mcp.json``.
    """
    prompt, report = build_imported_prompt(root)
    report.mcp_servers = import_mcp_servers(root, dest_config_dir)

    if prompt:
        defn = AgentDef(
            name=agent_name,
            description="Imported project rules (CLAUDE.md / .cursor/rules).",
            system_prompt=prompt,
            memory=True,
        )
        save_agent(defn, root=dest_root)
        report.agent_created = agent_name
    else:
        report.notes.append("No CLAUDE.md / AGENTS.md / .cursor/rules found to import.")

    if report.mcp_servers:
        report.notes.append(
            f"Merged {len(report.mcp_servers)} MCP server(s) into {dest_config_dir / '.mcp.json'}."
        )
    return report
