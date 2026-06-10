from __future__ import annotations

import pytest

from manta_code.agents.factory import compile_subagent
from manta_code.agents.registry import AgentDef, FsRule

pytest.importorskip("deepagents")


def test_plain_agent_compiles_to_minimal_subagent():
    sub = compile_subagent(AgentDef(name="helper", description="d", system_prompt="p"))
    assert sub["name"] == "helper"
    assert sub["description"] == "d"
    assert sub["system_prompt"] == "p"
    # No constraints => no enforcement keys (inherits parent tools/permissions).
    assert "permissions" not in sub
    assert "middleware" not in sub
    assert "interrupt_on" not in sub
    assert "model" not in sub


def test_model_pin_is_emitted():
    sub = compile_subagent(
        AgentDef(name="planner", model="databricks:databricks-claude-opus-4-8")
    )
    assert sub["model"] == "databricks:databricks-claude-opus-4-8"


def test_read_only_denies_writes_and_adds_policy():
    from deepagents.middleware.filesystem import FilesystemPermission

    sub = compile_subagent(AgentDef(name="review", read_only=True))
    perms = sub["permissions"]
    assert isinstance(perms[0], FilesystemPermission)
    # First rule denies all writes.
    assert perms[0].mode == "deny"
    assert "write" in perms[0].operations
    # A tool-policy middleware enforces the non-filesystem half (execute, etc.).
    from manta_code.middleware.policy import ToolPolicyMiddleware

    assert any(isinstance(m, ToolPolicyMiddleware) for m in sub["middleware"])


def test_explicit_filesystem_rules_follow_read_only_rule():
    sub = compile_subagent(
        AgentDef(
            name="scoped",
            read_only=True,
            filesystem=[FsRule(operations=["read"], paths=["/repo/src/**"], mode="allow")],
        )
    )
    perms = sub["permissions"]
    assert len(perms) == 2
    assert perms[0].mode == "deny"  # read-only write-deny first
    assert perms[0].paths == ["/**"]
    assert perms[1].paths == ["/repo/src/**"]


def test_allow_and_deny_lists_add_policy_middleware():
    from manta_code.middleware.policy import ToolPolicyMiddleware

    sub = compile_subagent(
        AgentDef(name="limited", tools_allow=["read_file", "grep"], tools_deny=["execute"])
    )
    mws = [m for m in sub["middleware"] if isinstance(m, ToolPolicyMiddleware)]
    assert len(mws) == 1


def test_approval_maps_to_interrupt_on():
    sub = compile_subagent(AgentDef(name="careful", approval=["write_file", "execute"]))
    assert sub["interrupt_on"] == {"write_file": True, "execute": True}


def test_extra_middleware_is_appended():
    from manta_code.middleware.policy import ToolPolicyMiddleware

    sentinel = ToolPolicyMiddleware(deny=["foo"], agent_name="sentinel")
    sub = compile_subagent(
        AgentDef(name="budgeted", read_only=True), extra_middleware=[sentinel]
    )
    assert sentinel in sub["middleware"]
