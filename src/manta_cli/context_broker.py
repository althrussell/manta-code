from __future__ import annotations

from pathlib import Path

from .schemas import ContextManifest, RouteDecision

EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".manta"}
INTERESTING_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".json", ".yaml", ".yml"}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class RepoScanner:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path.cwd()).resolve()

    def iter_files(self, limit: int = 500) -> list[Path]:
        files: list[Path] = []
        for path in self.root.rglob("*"):
            if len(files) >= limit:
                break
            if path.is_dir():
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in INTERESTING_EXTS:
                files.append(path)
        return files

    def repo_summary(self) -> str:
        files = self.iter_files(limit=200)
        rels = [str(p.relative_to(self.root)) for p in files]
        return "\n".join(rels[:200])


class ContextBroker:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path.cwd()).resolve()
        self.scanner = RepoScanner(self.root)

    def build_manifest(self, session_id: str, route: RouteDecision, prompt: str) -> ContextManifest:
        files = self.scanner.iter_files(limit=200)
        prompt_terms = {term.lower() for term in prompt.replace("/", " ").replace("_", " ").split() if len(term) > 2}
        selected: list[str] = []
        for path in files:
            rel = str(path.relative_to(self.root))
            rel_lower = rel.lower()
            if any(term in rel_lower for term in prompt_terms):
                selected.append(rel)
            if len(selected) >= 20:
                break
        if not selected:
            selected = [str(p.relative_to(self.root)) for p in files[:10]]
        summary = "\n".join(selected)
        base_tokens = estimate_tokens(prompt + "\n" + summary)
        return ContextManifest(
            session_id=session_id,
            route=route.route.value,
            repo_root=str(self.root),
            selected_files=selected,
            excluded_paths=sorted(EXCLUDED_DIRS),
            role_token_estimates={
                "router": min(4000, base_tokens),
                "builder": base_tokens * 4,
                "reviewer": base_tokens * 3,
            },
            selection_reason="Selected by filename keyword match; fallback to first interesting files.",
        )
