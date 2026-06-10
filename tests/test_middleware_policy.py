from __future__ import annotations

import pytest

pytest.importorskip("langchain.agents.middleware.types")

from langchain_core.messages import ToolMessage  # noqa: E402

from manta_code.middleware.policy import ToolPolicyMiddleware  # noqa: E402


class _Req:
    """Minimal stand-in for langchain's ToolCallRequest."""

    def __init__(
        self, name: str, call_id: str = "call-1", args: dict | None = None
    ) -> None:
        self.tool_call = {"name": name, "id": call_id, "args": args or {}}


class _Rule:
    """Minimal stand-in for an AgentDef filesystem rule."""

    def __init__(self, operations, paths, mode):
        self.operations = operations
        self.paths = paths
        self.mode = mode


def _ran(request):
    return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])


def test_read_only_blocks_execute_and_writes():
    mw = ToolPolicyMiddleware(read_only=True, agent_name="review")
    for tool in ("execute", "write_file", "edit_file"):
        result = mw.wrap_tool_call(_Req(tool), _ran)
        assert result.status == "error"
        assert "read-only" in result.content


def test_read_only_denial_names_profile_and_exit_step():
    # The block must be self-describing: which agent, and how to leave it.
    mw = ToolPolicyMiddleware(read_only=True, agent_name="planning")
    result = mw.wrap_tool_call(_Req("execute"), _ran)
    assert "planning" in result.content  # names the active profile
    assert "/agents" in result.content  # tells the user the exit step
    assert "swe" in result.content  # names a writable alternative


def test_read_only_denial_without_name_is_generic():
    mw = ToolPolicyMiddleware(read_only=True)
    result = mw.wrap_tool_call(_Req("execute"), _ran)
    assert "this read-only agent" in result.content


def test_read_only_allows_reads():
    mw = ToolPolicyMiddleware(read_only=True)
    result = mw.wrap_tool_call(_Req("read_file"), _ran)
    assert result.content == "ran"


def test_deny_list_blocks():
    mw = ToolPolicyMiddleware(deny=["dangerous_tool"])
    result = mw.wrap_tool_call(_Req("dangerous_tool"), _ran)
    assert result.status == "error"
    assert "deny-list" in result.content


def test_allow_list_blocks_unlisted():
    mw = ToolPolicyMiddleware(allow=["read_file", "grep"])
    blocked = mw.wrap_tool_call(_Req("write_file"), _ran)
    assert blocked.status == "error"
    allowed = mw.wrap_tool_call(_Req("read_file"), _ran)
    assert allowed.content == "ran"


def test_no_policy_allows_everything():
    mw = ToolPolicyMiddleware()
    assert mw.wrap_tool_call(_Req("execute"), _ran).content == "ran"


def test_async_path_blocks():
    import asyncio

    mw = ToolPolicyMiddleware(read_only=True)

    async def _aran(request):
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    result = asyncio.run(mw.awrap_tool_call(_Req("execute"), _aran))
    assert result.status == "error"


def test_name_includes_agent():
    mw = ToolPolicyMiddleware(read_only=True, agent_name="review")
    assert mw.name == "Manta.ToolPolicy.review"


def test_filesystem_deny_rule_blocks_matching_write():
    mw = ToolPolicyMiddleware(
        filesystem=[_Rule(["write"], ["/repo/secrets/**"], "deny")]
    )
    blocked = mw.wrap_tool_call(
        _Req("write_file", args={"file_path": "/repo/secrets/key.pem"}), _ran
    )
    assert blocked.status == "error"
    assert "filesystem policy" in blocked.content


def test_filesystem_deny_rule_allows_non_matching_write():
    mw = ToolPolicyMiddleware(
        filesystem=[_Rule(["write"], ["/repo/secrets/**"], "deny")]
    )
    allowed = mw.wrap_tool_call(
        _Req("write_file", args={"file_path": "/repo/src/app.py"}), _ran
    )
    assert allowed.content == "ran"


def test_filesystem_allow_rule_short_circuits_later_deny():
    # First match wins: an explicit allow before a broad deny permits the path.
    mw = ToolPolicyMiddleware(
        filesystem=[
            _Rule(["write"], ["/repo/build/**"], "allow"),
            _Rule(["write"], ["/repo/**"], "deny"),
        ]
    )
    allowed = mw.wrap_tool_call(
        _Req("write_file", args={"file_path": "/repo/build/out.js"}), _ran
    )
    assert allowed.content == "ran"
    blocked = mw.wrap_tool_call(
        _Req("write_file", args={"file_path": "/repo/src/app.py"}), _ran
    )
    assert blocked.status == "error"


def test_filesystem_rule_only_applies_to_its_operation():
    # A write-deny rule does not block a read of the same path.
    mw = ToolPolicyMiddleware(
        filesystem=[_Rule(["write"], ["/repo/**"], "deny")]
    )
    allowed = mw.wrap_tool_call(
        _Req("read_file", args={"file_path": "/repo/src/app.py"}), _ran
    )
    assert allowed.content == "ran"
