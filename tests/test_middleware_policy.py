from __future__ import annotations

import pytest

pytest.importorskip("langchain.agents.middleware.types")

from langchain_core.messages import ToolMessage  # noqa: E402

from manta_code.middleware.policy import ToolPolicyMiddleware  # noqa: E402


class _Req:
    """Minimal stand-in for langchain's ToolCallRequest."""

    def __init__(self, name: str, call_id: str = "call-1") -> None:
        self.tool_call = {"name": name, "id": call_id, "args": {}}


def _ran(request):
    return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])


def test_read_only_blocks_execute_and_writes():
    mw = ToolPolicyMiddleware(read_only=True, agent_name="review")
    for tool in ("execute", "write_file", "edit_file"):
        result = mw.wrap_tool_call(_Req(tool), _ran)
        assert result.status == "error"
        assert "read-only" in result.content


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
