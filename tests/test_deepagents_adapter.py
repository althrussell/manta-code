import sys
import types
from pathlib import Path

import pytest

from manta_cli.agents.deepagents_adapter import DeepAgentsRuntime
from manta_cli.config import MantaConfig
from manta_cli.roles import default_roles
from manta_cli.schemas import ContextManifest


class _FakeMessage:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


def _install_fake_deepagents(monkeypatch, captured, *, structured=None, builder_usage=None):
    module = types.ModuleType("deepagents")

    def create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def invoke(self, state):
                captured["state"] = state
                if "response_format" in kwargs:
                    return {
                        "messages": [_FakeMessage("review done", {"input_tokens": 100, "output_tokens": 50})],
                        "structured_response": structured,
                    }
                return {
                    "messages": [
                        _FakeMessage("built it", builder_usage or {"input_tokens": 200, "output_tokens": 80})
                    ]
                }

        return _Agent()

    module.create_deep_agent = create_deep_agent
    monkeypatch.setitem(sys.modules, "deepagents", module)


def _ctx(tmp_path: Path) -> ContextManifest:
    return ContextManifest(
        session_id="s1",
        route="normal_code_change",
        repo_root=str(tmp_path),
        selected_files=["src/manta_cli/main.py"],
    )


def test_builder_role_maps_model_and_captures_usage(monkeypatch, tmp_path: Path):
    captured: dict = {}
    _install_fake_deepagents(monkeypatch, captured)
    role = default_roles(MantaConfig())["builder"]

    result = DeepAgentsRuntime(root=tmp_path).run_role(role, "add a feature", _ctx(tmp_path))

    assert result.status == "completed"
    assert result.output["message"] == "built it"
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 80
    assert captured["model"] == role.model
    # Builder is not a reviewer, so no structured response_format is requested.
    assert "response_format" not in captured
    # Builder receives policy-wrapped side-effecting tools.
    assert any(getattr(t, "__name__", "") == "apply_patch" for t in captured["tools"])


def test_reviewer_role_requests_structured_output_and_is_read_only(monkeypatch, tmp_path: Path):
    captured: dict = {}
    _install_fake_deepagents(monkeypatch, captured, structured={"approved": True, "findings": []})
    role = default_roles(MantaConfig())["code_reviewer"]

    result = DeepAgentsRuntime(root=tmp_path).run_role(role, "review the diff", _ctx(tmp_path))

    assert "response_format" in captured
    assert result.status == "completed"
    assert result.output["review"]["approved"] is True
    tool_names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "apply_patch" not in tool_names and "run_shell" not in tool_names


def test_reviewer_blocks_when_not_approved(monkeypatch, tmp_path: Path):
    captured: dict = {}
    _install_fake_deepagents(
        monkeypatch,
        captured,
        structured={"approved": False, "findings": [
            {"severity": "high", "category": "correctness", "issue": "bug", "required_fix": "fix it"}
        ]},
    )
    role = default_roles(MantaConfig())["code_reviewer"]

    result = DeepAgentsRuntime(root=tmp_path).run_role(role, "review the diff", _ctx(tmp_path))

    assert result.status == "blocked"
    assert result.output["review"]["findings"][0]["severity"] == "high"


def test_missing_deepagents_raises_helpful_error(monkeypatch, tmp_path: Path):
    # Ensure the import fails even if deepagents happens to be installed.
    monkeypatch.setitem(sys.modules, "deepagents", None)
    role = default_roles(MantaConfig())["builder"]
    with pytest.raises(RuntimeError, match="Deep Agents is not installed"):
        DeepAgentsRuntime(root=tmp_path).run_role(role, "x", _ctx(tmp_path))
