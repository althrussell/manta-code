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
    assert "permissions" not in sub
    assert "interrupt_on" not in sub
    assert "model" not in sub
    # Every agent that has not opted into the task tools gets a policy that
    # denies them: subagents inherit the orchestrator's extra tools, and a
    # read-only agent must not route around its boundary by submitting a
    # background swe task.
    (policy,) = sub["middleware"]
    assert "manta_task_submit" in policy._deny
    assert "manta_task_cancel" in policy._deny


def test_chief_opt_in_keeps_task_tools_allowed():
    from manta_code.agents.defaults import CHIEF
    from manta_code.agents.factory import _effective_deny

    assert "manta_task_submit" not in _effective_deny(CHIEF)
    from manta_code.agents.defaults import REVIEW

    assert "manta_task_submit" in _effective_deny(REVIEW)


def test_model_pin_is_emitted():
    sub = compile_subagent(
        AgentDef(name="planner", model="databricks:databricks-claude-opus-4-8")
    )
    assert sub["model"] == "databricks:databricks-claude-opus-4-8"


def test_read_only_adds_policy_without_permissions():
    # We never emit FilesystemPermission: the execute-capable sandbox backend
    # rejects it. read-only is enforced entirely by the tool-policy middleware.
    from manta_code.middleware.policy import ToolPolicyMiddleware

    sub = compile_subagent(AgentDef(name="review", read_only=True))
    assert "permissions" not in sub
    assert any(isinstance(m, ToolPolicyMiddleware) for m in sub["middleware"])


def test_explicit_filesystem_rules_go_to_policy_middleware():
    from manta_code.middleware.policy import ToolPolicyMiddleware

    rule = FsRule(operations=["read"], paths=["/repo/src/**"], mode="allow")
    sub = compile_subagent(AgentDef(name="scoped", filesystem=[rule]))
    assert "permissions" not in sub
    policy = next(m for m in sub["middleware"] if isinstance(m, ToolPolicyMiddleware))
    assert policy._filesystem == [rule]


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


def test_compile_chief_includes_task_tools():
    import pytest as _pytest

    _pytest.importorskip("langchain_core")
    from manta_code.agents.defaults import CHIEF
    from manta_code.agents.factory import compile_subagent

    compiled = compile_subagent(CHIEF)
    names = {t.name for t in compiled.get("tools", [])}
    assert "manta_task_submit" in names
    assert "manta_task_output" in names


def test_compile_without_manta_tools_sets_no_tools_key():
    from manta_code.agents.defaults import SWE
    from manta_code.agents.factory import compile_subagent

    assert "tools" not in compile_subagent(SWE)
