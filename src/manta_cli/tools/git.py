from __future__ import annotations

import subprocess
from pathlib import Path


def git_status(root: Path | None = None) -> str:
    proc = subprocess.run(["git", "status", "--short"], cwd=root or Path.cwd(), capture_output=True, text=True, check=False)
    return proc.stdout.strip()


def git_diff(root: Path | None = None) -> str:
    proc = subprocess.run(["git", "diff", "--no-ext-diff"], cwd=root or Path.cwd(), capture_output=True, text=True, check=False)
    return proc.stdout
