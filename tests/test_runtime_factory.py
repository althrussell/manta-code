from pathlib import Path

from manta_cli.agents.factory import get_runtime
from manta_cli.agents.mock_runtime import MockRuntime


def test_dry_run_returns_mock_runtime():
    assert isinstance(get_runtime(dry_run=True), MockRuntime)


def test_real_returns_deepagents_runtime_without_importing_sdk(tmp_path: Path):
    # Constructing the adapter must not require the optional [agent] extra;
    # the Deep Agents import is deferred until run_role.
    runtime = get_runtime(dry_run=False, root=tmp_path)
    assert runtime.__class__.__name__ == "DeepAgentsRuntime"
