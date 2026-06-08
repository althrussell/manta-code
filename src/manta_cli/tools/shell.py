from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from manta_cli.policy import PolicyEngine
from manta_cli.schemas import ToolRequest


@dataclass
class ShellResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


def run_shell(command: str, cwd: Path | None = None, timeout: int = 300) -> ShellResult:
    policy = PolicyEngine(project_root=cwd or Path.cwd())
    decision = policy.decide(ToolRequest(tool="shell", action="execute", args={"command": command}, cwd=str(cwd or Path.cwd())))
    if decision.decision != "allow":
        raise PermissionError(f"Shell command not allowed: {decision.reason}")
    proc = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout, check=False)
    return ShellResult(command=command, exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
