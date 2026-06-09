"""Provision Manta's default planning / SWE / review subagents.

``deepagents-code`` already implements a full subagent system: the main agent
gets a ``task`` tool and delegates to child agents defined as markdown files
under ``~/.deepagents/<agent>/agents/<name>/AGENTS.md`` (user level) or
``.deepagents/agents/<name>/AGENTS.md`` (project level). Each file is YAML
frontmatter (``name``, ``description``, optional ``model``) plus a body that
becomes the subagent's system prompt. See
``deepagents_code.subagents._parse_subagent_file``.

Manta ships three opinionated defaults so ``manta`` has a planning / build /
review division of labour out of the box, the same way it preconfigures the
Databricks provider:

- **planning** — turns a request into an ordered ``write_todos`` plan; read-only.
- **swe** — implements changes hands-on (edit files, run tests).
- **review** — read-only review that reports findings (prompt-enforced; markdown
  subagents cannot yet restrict tools, so the read-only contract lives in the
  prompt rather than in ``permissions``).

Each subagent pins a Databricks endpoint suited to its role (a "right model for
the job" split): a strong reasoning model for planning, a coding model for the
SWE agent, and a different-vendor model for review (an independent reviewer
catches what the author model misses). The main orchestration agent runs on the
configured ``default_endpoint`` (see :mod:`manta_code.config`). All endpoints
resolve through Manta's Databricks provider.

Provisioning is **once, then hands-off**: a state marker records that defaults
were written so the user's later edits — or deletions — are never clobbered on
subsequent launches. Pre-existing files of the same name are also left
untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Default ``deepagents-code`` agent identifier (its ``DEFAULT_AGENT_NAME``);
#: this is the profile ``manta`` launches with when the user passes no ``-a``.
DEFAULT_AGENT_NAME = "agent"

#: ``~/.deepagents`` base (hard-coded upstream; mirrors ``dcode`` constants).
DEEPAGENTS_CONFIG_DIR = Path.home() / ".deepagents"

#: State dir + marker recording that Manta provisioned its default subagents.
DEEPAGENTS_STATE_DIR = DEEPAGENTS_CONFIG_DIR / ".state"
SUBAGENTS_MARKER_PATH = DEEPAGENTS_STATE_DIR / "manta_subagents_provisioned"


@dataclass(frozen=True)
class SubagentSpec:
    """A Manta default subagent: its task-tool name, description, and prompt.

    ``model`` is a ``provider:endpoint`` string (e.g.
    ``databricks:databricks-claude-opus-4-8``). When ``None`` the subagent
    inherits the parent agent's model.
    """

    name: str
    description: str
    body: str
    model: str | None = None


#: ``provider:`` prefix every subagent model resolves through (Manta's
#: Databricks AI Gateway provider in ``~/.deepagents/config.toml``).
DATABRICKS_PROVIDER = "databricks"


def _databricks_model(endpoint: str) -> str:
    return f"{DATABRICKS_PROVIDER}:{endpoint}"


PLANNING = SubagentSpec(
    name="planning",
    description=(
        "Turn an ambiguous or multi-step request into a clear, ordered "
        "implementation plan. Delegate here before large changes or when "
        "requirements are unclear; it plans only and does not modify code."
    ),
    model=_databricks_model("databricks-claude-opus-4-8"),
    body="""You are Manta's planning specialist in a Databricks-focused engineering environment.

Your job is to turn the requested work into a concrete, ordered plan — not to implement it.

- Explore the codebase enough to ground the plan in reality: read files, search, inspect structure. Do NOT edit, write, or run state-changing commands.
- Use the write_todos tool to record the plan as small, verifiable steps in dependency order.
- Surface unknowns, risks, the files each step touches, and any decision the user must make.
- Keep each step small enough to map to a single focused change.

Return a short summary of the plan and the key decisions. The main agent or the `swe` subagent will execute it.""",
)

SWE = SubagentSpec(
    name="swe",
    description=(
        "Implement code changes hands-on: read, edit, and write files, then "
        "run tests and shell commands to verify. Delegate well-scoped "
        "engineering tasks here."
    ),
    model=_databricks_model("databricks-gpt-5-5"),
    body="""You are Manta's software-engineering subagent in a Databricks-focused environment.

Implement the requested change end to end:

- Read the relevant files first; match the existing style and conventions.
- Make focused edits with the file tools (read_file, write_file, edit_file, glob, grep).
- Run tests, linters, and builds with the execute tool to verify your work; fix any failures you introduce.
- Prefer minimal, surgical changes; do not refactor unrelated code.
- If the task is underspecified or you hit a blocking decision, stop and report rather than guessing.

Return a concise summary of what changed, why, and how you verified it.""",
)

REVIEW = SubagentSpec(
    name="review",
    description=(
        "Read-only code review: inspect diffs and files for bugs, security "
        "issues, and style problems, and report findings. Does not modify code."
    ),
    model=_databricks_model("databricks-gemini-3-1-pro"),
    body="""You are Manta's code-review subagent in a Databricks-focused environment.

Review the code under discussion and report findings. This is a READ-ONLY role:

- Do NOT write, edit, create, or delete files, and do not run state-changing commands. If asked to fix something, describe the fix instead of applying it.
- Use read_file, glob, and grep to inspect the change and its surrounding context.
- Look for correctness bugs, edge cases, security issues, error-handling gaps, and deviations from the codebase's conventions.
- Prioritize findings by severity and cite specific files and line ranges.

Return a structured list of findings (severity, location, issue, suggested fix). If the code looks good, say so plainly.""",
)

#: The default set Manta provisions, in delegation-workflow order.
MANTA_SUBAGENTS: tuple[SubagentSpec, ...] = (PLANNING, SWE, REVIEW)


def _yaml_double_quote(value: str) -> str:
    """Return ``value`` as a safely double-quoted YAML scalar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_subagent_markdown(spec: SubagentSpec) -> str:
    """Render a ``SubagentSpec`` to the AGENTS.md format deepagents-code parses.

    When ``spec.model`` is set, a ``model:`` line is emitted in the YAML
    frontmatter so the subagent runs on its pinned Databricks endpoint;
    otherwise it is omitted and the subagent inherits the parent agent's model.
    """
    model_line = (
        f"model: {_yaml_double_quote(spec.model)}\n" if spec.model else ""
    )
    return (
        "---\n"
        f"name: {spec.name}\n"
        f"description: {_yaml_double_quote(spec.description)}\n"
        f"{model_line}"
        "---\n\n"
        f"{spec.body.strip()}\n"
    )


def user_subagents_dir(
    *, base_dir: Path | None = None, agent_name: str = DEFAULT_AGENT_NAME
) -> Path:
    """Return ``~/.deepagents/<agent_name>/agents`` (deepagents-code's user dir)."""
    base = base_dir or DEEPAGENTS_CONFIG_DIR
    return base / agent_name / "agents"


def project_subagents_dir(root: Path | None = None) -> Path:
    """Return ``<root>/.deepagents/agents`` (deepagents-code's project dir)."""
    return (root or Path.cwd()) / ".deepagents" / "agents"


@dataclass(frozen=True)
class SubagentInfo:
    """A subagent discovered on disk: its parsed frontmatter plus raw text.

    ``model`` is the ``provider:endpoint`` string from the frontmatter, or
    ``None`` when the file omits it (the subagent then inherits the orchestrator
    model). ``source`` is ``"user"`` or ``"project"``. ``raw`` is the verbatim
    file content (used for the full-config view, so it never diverges from what
    the agent actually loads).
    """

    name: str
    description: str
    model: str | None
    source: str
    path: Path
    body: str
    raw: str


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _unquote_yaml_scalar(value: str) -> str:
    """Strip surrounding quotes from a simple YAML scalar and unescape it."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Parse ``---`` YAML frontmatter into a flat dict plus the trailing body.

    Deliberately minimal — it handles the ``key: value`` scalars Manta writes
    (``name``, ``description``, ``model``). Returns ``None`` when ``text`` has no
    frontmatter block.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, raw_value = line.partition(":")
        if not sep:
            continue
        frontmatter[key.strip()] = _unquote_yaml_scalar(raw_value.strip())
    return frontmatter, match.group(2).strip()


def discover_subagents(
    *,
    base_dir: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    project_root: Path | None = None,
) -> list[SubagentInfo]:
    """Discover provisioned subagents from the user and project agent dirs.

    Scans ``<user>/<name>/AGENTS.md`` and ``<project>/<name>/AGENTS.md``, parsing
    each file's frontmatter. Project subagents override user subagents of the
    same name (matching deepagents-code's precedence). Returns them sorted by
    name; an empty list when nothing is provisioned.
    """
    discovered: dict[str, SubagentInfo] = {}
    sources = (
        ("user", user_subagents_dir(base_dir=base_dir, agent_name=agent_name)),
        ("project", project_subagents_dir(project_root)),
    )
    for source, agents_dir in sources:
        if not agents_dir.is_dir():
            continue
        for entry in sorted(agents_dir.iterdir()):
            agents_md = entry / "AGENTS.md"
            if not agents_md.is_file():
                continue
            text = agents_md.read_text(encoding="utf-8")
            parsed = _parse_frontmatter(text)
            if parsed is None:
                continue
            frontmatter, body = parsed
            name = frontmatter.get("name") or entry.name
            discovered[name] = SubagentInfo(
                name=name,
                description=frontmatter.get("description", ""),
                model=frontmatter.get("model") or None,
                source=source,
                path=agents_md,
                body=body,
                raw=text,
            )
    return sorted(discovered.values(), key=lambda info: info.name)


def ensure_manta_subagents(
    *,
    base_dir: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    marker_path: Path | None = None,
    specs: tuple[SubagentSpec, ...] = MANTA_SUBAGENTS,
) -> list[Path]:
    """Provision Manta's default subagents once, never clobbering user content.

    On the first run (no marker), writes ``<agents_dir>/<name>/AGENTS.md`` for
    each spec whose file does not already exist, then drops the marker so later
    launches leave the user's edits and deletions alone. Returns the list of
    files actually written (empty when the marker already exists or every file
    was present).
    """
    marker = marker_path or SUBAGENTS_MARKER_PATH
    if marker.exists():
        return []

    agents_dir = user_subagents_dir(base_dir=base_dir, agent_name=agent_name)
    written: list[Path] = []
    for spec in specs:
        target = agents_dir / spec.name / "AGENTS.md"
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_subagent_markdown(spec), encoding="utf-8")
        written.append(target)

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="utf-8")
    return written
