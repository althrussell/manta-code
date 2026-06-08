from pathlib import Path

from manta_cli.agents.tools import build_tools
from manta_cli.config import MantaConfig
from manta_cli.policy import PolicyEngine
from manta_cli.roles import default_roles


def _roles():
    return default_roles(MantaConfig())


def test_builder_gets_side_effecting_tools(tmp_path: Path):
    tools = {t.__name__: t for t in build_tools(_roles()["builder"], root=tmp_path)}
    assert {"read_file", "apply_patch", "run_shell", "git_diff"} <= set(tools)


def test_reviewer_is_read_only(tmp_path: Path):
    tools = {t.__name__ for t in build_tools(_roles()["code_reviewer"], root=tmp_path)}
    assert tools == {"read_file", "git_diff"}
    assert "apply_patch" not in tools
    assert "run_shell" not in tools


def test_write_tool_blocks_outside_root(tmp_path: Path):
    tools = {t.__name__: t for t in build_tools(_roles()["builder"], root=tmp_path)}
    out = tools["apply_patch"]("/etc/manta_hack.txt", "x")
    assert out.startswith("BLOCK")


def test_write_tool_writes_inside_root(tmp_path: Path):
    tools = {t.__name__: t for t in build_tools(_roles()["builder"], root=tmp_path)}
    out = tools["apply_patch"]("pkg/foo.py", "print(1)\n")
    assert out.startswith("OK")
    assert (tmp_path / "pkg" / "foo.py").read_text() == "print(1)\n"


def test_shell_tool_blocks_dangerous_and_requires_approval(tmp_path: Path):
    tools = {t.__name__: t for t in build_tools(_roles()["builder"], root=tmp_path)}
    assert tools["run_shell"]("rm -rf /").startswith("BLOCK")
    assert tools["run_shell"]("ls -la").startswith("APPROVAL_REQUIRED")


def test_shell_tool_runs_allowlisted_command(tmp_path: Path):
    policy = PolicyEngine(project_root=tmp_path, allowlist=("echo",))
    tools = {t.__name__: t for t in build_tools(_roles()["builder"], root=tmp_path, policy=policy)}
    out = tools["run_shell"]("echo manta")
    assert "exit=0" in out
    assert "manta" in out
