from __future__ import annotations

from pathlib import Path

from .schemas import ToolDecision, ToolRequest

DEFAULT_ALLOWLIST = (
    "pytest",
    "python -m pytest",
    "uv run pytest",
    "ruff check",
    "mypy",
    "npm test",
    "npm run test",
    "npm run lint",
    "pnpm test",
    "pnpm lint",
    "yarn test",
    "yarn lint",
)

PROTECTED_PATTERNS = (".env", ".env.", "secret", "credential", "private_key", "id_rsa", "id_ed25519")


class PolicyEngine:
    def __init__(self, project_root: Path | None = None, allowlist: tuple[str, ...] = DEFAULT_ALLOWLIST):
        self.project_root = (project_root or Path.cwd()).resolve()
        self.allowlist = allowlist

    def decide(self, request: ToolRequest) -> ToolDecision:
        if request.tool == "shell":
            return self._decide_shell(str(request.args.get("command", "")))
        if request.tool in {"write_file", "edit_file", "apply_patch"}:
            path = request.args.get("path") or request.args.get("file")
            return self._decide_write_path(path)
        if request.tool == "network":
            return ToolDecision(decision="block", reason="Network access is denied by default.")
        if request.tool == "git" and request.action == "push":
            return ToolDecision(decision="block", reason="git push is denied by default.")
        if request.tool == "git" and request.action == "commit":
            return ToolDecision(decision="approval_required", reason="git commit requires human approval.")
        return ToolDecision(decision="allow", reason="No blocking policy matched.")

    def _decide_shell(self, command: str) -> ToolDecision:
        stripped = command.strip()
        if not stripped:
            return ToolDecision(decision="block", reason="Empty shell command.")
        if any(stripped == allowed or stripped.startswith(allowed + " ") for allowed in self.allowlist):
            return ToolDecision(decision="allow", reason="Shell command is allowlisted.")
        dangerous = ["rm -rf", "curl ", "wget ", "| bash", "sudo ", "chmod 777", "scp ", "ssh "]
        if any(term in stripped for term in dangerous):
            return ToolDecision(decision="block", reason="Shell command contains dangerous pattern.")
        return ToolDecision(decision="approval_required", reason="Shell command is not allowlisted.")

    def _decide_write_path(self, path_value: object) -> ToolDecision:
        if not path_value or not isinstance(path_value, str):
            return ToolDecision(decision="approval_required", reason="Write path missing or ambiguous.")
        path = Path(path_value)
        resolved = (self.project_root / path).resolve() if not path.is_absolute() else path.resolve()
        if not str(resolved).startswith(str(self.project_root)):
            return ToolDecision(decision="block", reason="Writes outside project root are blocked.")
        lower = str(resolved).lower()
        if any(pattern in lower for pattern in PROTECTED_PATTERNS):
            return ToolDecision(decision="block", reason="Protected path cannot be modified automatically.")
        return ToolDecision(decision="allow", reason="Write path is inside project root and not protected.")
