"""Execution receipts survive the actual dcode hook and filesystem stack."""
from __future__ import annotations

import asyncio
import sys

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from manta_code.tasks import store


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_real_dcode_execute_preserves_observed_test_receipt(tmp_path, monkeypatch):
    from manta_code import hook
    from manta_code.tasks.events import EventLogMiddleware
    import deepagents_code.agent as upstream
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setenv("DEEPAGENTS_HOME", str(tmp_path / "deepagents"))
    monkeypatch.setenv("MANTA_TASK_ID", "fixture")
    monkeypatch.chdir(tmp_path)
    store.create_task(store.TaskRecord(id="fixture", agent="agent", prompt="run a test", state="running"))
    (tmp_path / "test_receipt.py").write_text("def test_fixture():\n    assert 2 + 2 == 4\n")
    command = f"{sys.executable} -m pytest -q test_receipt.py"
    model = Model(responses=[AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": command}, "id": "execute-one"}]), AIMessage(content="done")])
    observations = []
    receipt = EventLogMiddleware._command_receipt
    def observe(self, request, result):
        observations.append((type(result).__name__, getattr(result, "artifact", None), request.tool_call))
        return receipt(self, request, result)
    monkeypatch.setattr(EventLogMiddleware, "_command_receipt", observe)
    original, original_cli = upstream.create_deep_agent, upstream.create_cli_agent
    monkeypatch.setattr(hook, "_installed", False)
    monkeypatch.setattr(hook, "_runtime_definitions", lambda: [])
    monkeypatch.setattr(hook, "build_databricks_tools", lambda: [])
    try:
        hook.install_build_hook()
        graph, _ = upstream.create_cli_agent(model, "agent", enable_shell=True,
            enable_interpreter=False, enable_memory=False, enable_skills=False,
            interactive=False, auto_approve=True, enforce_model_policy=False)
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Run the fixture test once")]},
            {"configurable": {"thread_id": "fixture-thread"}}))
        result = store.get_task("fixture").structured_result
        assert result.get("tests"), observations
        assert result["tests"][0]["exit_code"] == 0
    finally:
        upstream.create_deep_agent, upstream.create_cli_agent = original, original_cli
