"""Policy-wrapped tool callables for agent runtimes.

Every side-effecting tool routes through :class:`~manta_cli.policy.PolicyEngine`
before doing anything. This is the Manta enforcement boundary: Deep Agents'
own filesystem permissions do not protect custom tools or shell execution
(see ``docs/13-deepagents-integration.md``), so all side effects must pass
through here regardless of which runtime is active.

This module deliberately imports nothing from Deep Agents. The callables are
plain Python functions (with docstrings and type hints) so any LangChain-based
runtime can adapt them into tools, while remaining trivially unit-testable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from manta_cli.policy import PolicyEngine
from manta_cli.roles import RoleSpec
from manta_cli.schemas import ToolRequest

# Read-only tools are always safe to expose; side-effecting tools are gated by
# role capability (``can_write`` / ``shell``) and the policy engine.
READ_ONLY_TOOLS = {"read_file", "grep", "glob", "git_diff"}


def build_tools(
    role: RoleSpec,
    *,
    root: Path | None = None,
    policy: PolicyEngine | None = None,
) -> list[Callable[..., str]]:
    """Return policy-wrapped tool callables permitted for ``role``.

    Reviewers and any role without ``can_write`` get read-only tools only,
    enforcing ADR 0005 (reviewers are read-only) at the tool layer rather than
    relying on prompt instructions.
    """
    project_root = (root or Path.cwd()).resolve()
    engine = policy or PolicyEngine(project_root=project_root)

    def _resolve(path: str) -> Path:
        candidate = Path(path)
        resolved = (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        return resolved

    def _inside_root(path: Path) -> bool:
        return str(path).startswith(str(project_root))

    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the project. Returns file contents or an error string."""
        resolved = _resolve(path)
        if not _inside_root(resolved):
            return f"DENIED: reads outside project root are blocked: {path}"
        if not resolved.is_file():
            return f"ERROR: file not found: {path}"
        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"ERROR: file is not UTF-8 text: {path}"

    def glob(pattern: str) -> str:
        """List project files matching a glob pattern (relative to the project root)."""
        matches = [
            str(p.relative_to(project_root))
            for p in sorted(project_root.glob(pattern))
            if p.is_file()
        ]
        return "\n".join(matches) if matches else "(no matches)"

    def grep(pattern: str, path: str = ".") -> str:
        """Search project text files for a substring. Returns ``relpath:lineno:line`` matches."""
        base = _resolve(path)
        if not _inside_root(base):
            return f"DENIED: searches outside project root are blocked: {path}"
        results: list[str] = []
        candidates = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for file in candidates:
            try:
                for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
                    if pattern in line:
                        results.append(f"{file.relative_to(project_root)}:{lineno}:{line.strip()}")
                        if len(results) >= 200:
                            return "\n".join(results)
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(results) if results else "(no matches)"

    def git_diff() -> str:
        """Return the current unstaged git diff for the project (read-only)."""
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout or "(no changes)"

    def write_file(path: str, content: str) -> str:
        """Write ``content`` to ``path`` inside the project, subject to Manta policy."""
        decision = engine.decide(ToolRequest(tool="write_file", action="write", args={"path": path}))
        if decision.decision != "allow":
            return f"{decision.decision.upper()}: {decision.reason}"
        resolved = _resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {path}"

    def apply_patch(path: str, content: str) -> str:
        """Replace the full contents of ``path`` with ``content``, subject to Manta policy."""
        decision = engine.decide(ToolRequest(tool="apply_patch", action="write", args={"path": path}))
        if decision.decision != "allow":
            return f"{decision.decision.upper()}: {decision.reason}"
        resolved = _resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"OK: patched {path}"

    def run_shell(command: str) -> str:
        """Run an allowlisted shell command in the project. Non-allowlisted commands are denied."""
        decision = engine.decide(ToolRequest(tool="shell", action="execute", args={"command": command}))
        if decision.decision != "allow":
            return f"{decision.decision.upper()}: {decision.reason}"
        proc = subprocess.run(
            command,
            cwd=project_root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        return f"exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}".strip()

    available: dict[str, Callable[..., str]] = {
        "read_file": read_file,
        "glob": glob,
        "grep": grep,
        "git_diff": git_diff,
        "write_file": write_file,
        "apply_patch": apply_patch,
        "shell": run_shell,
    }

    tools: list[Callable[..., str]] = []
    for name in role.tools:
        fn = available.get(name)
        if fn is None:
            continue
        if name not in READ_ONLY_TOOLS and not role.can_write:
            # Read-only role requested a side-effecting tool; drop it.
            continue
        if name == "shell" and role.shell == "denied":
            continue
        tools.append(fn)
    return tools
