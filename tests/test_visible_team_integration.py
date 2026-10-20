from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents_code")

from deepagents_code.app import DeepAgentsApp
from deepagents_code.tui.widgets.approval import ApprovalMenu
from deepagents_code.tui.widgets.ask_user import AskUserMenu
from textual.app import App
from textual.containers import Vertical
from textual.widgets import Input, Static, TextArea

from manta_code.agents import profiles
from manta_code.agents.registry import AgentDef
from manta_code.agents.workspace import AgentWorkspace
from manta_code.tasks import executor, store
from manta_code.tui.agents import AgentEditorScreen, AgentWorkspaceResult, AgentWorkspaceScreen
from manta_code.tui.integration import (
    SessionTaskBoardService, apply_agent_workspace_result, install_visible_agent_team,
    runtime_busy, visible_conversation_snapshot,
)
from manta_code.tui.tasks import TaskBoardScreen


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    monkeypatch.setattr(profiles, "DEEPAGENTS_CONFIG_DIR", tmp_path / "profiles")
    monkeypatch.setattr(profiles, "PROFILES_STATE_PATH", tmp_path / "profiles" / ".state" / "profiles.json")
    return tmp_path


class SessionApp(App):
    def __init__(self, widget=None):
        super().__init__()
        self.widget = widget
        self._lc_thread_id = "parent-thread"
        self._agent_running = False
        self._agent_switching = False
        self._shell_running = False
        self._connecting = False
        self._assistant_id = "agent"
        self._pending_approval_widget = widget if isinstance(widget, ApprovalMenu) else None
        self._pending_ask_user_widget = widget if isinstance(widget, AskUserMenu) else None
        self.notices = []
        self.stops = 0
        self.resumed = []

    def compose(self):
        with Vertical():
            if self.widget is not None:
                yield self.widget
            yield Static("Session")

    def notify(self, message, **kwargs):
        self.notices.append(message)

    def _force_interrupt_active_work(self):
        self.stops += 1

    async def _handle_threads_command(self, command):
        self.resumed.append(command)


def _inline(task_id, request, *, state="waiting_approval", thread="parent-thread", checkout=None):
    return store.create_task(store.TaskRecord(
        id=task_id, agent="review", prompt="Review fixture", state=state, thread_id=thread,
        execution_kind="inline", manifest={"parent_thread_id": thread, "shared_checkout": checkout or str(Path.cwd().resolve())},
        checkpoint={"pending_id": f"pending-{task_id}", "interrupts": {f"interrupt-{task_id}": request}}))


def _bind(widget, record):
    from manta_code.tui.decisions import _widget_future
    widget._manta_interrupts = record.checkpoint["interrupts"]
    widget._manta_decision_future = _widget_future(widget)


def test_visible_snapshot_contains_only_visible_user_and_assistant_text():
    messages = [SimpleNamespace(type="user", content="Do the work"),
                SimpleNamespace(type="assistant", content="I will inspect it"),
                SimpleNamespace(type="reasoning", content="hidden opaque state"),
                SimpleNamespace(type="tool", content="credential material"),
                SimpleNamespace(type="assistant", content={"signed": "opaque"})]
    app = SimpleNamespace(_message_store=SimpleNamespace(get_all_messages=lambda: messages))
    snapshot = visible_conversation_snapshot(app)
    assert "USER:\nDo the work" in snapshot
    assert "ASSISTANT:\nI will inspect it" in snapshot
    assert "text snapshot" in snapshot and "not a shared live thread" in snapshot
    assert "hidden opaque" not in snapshot and "credential" not in snapshot and "signed" not in snapshot


def test_actual_approval_widget_resolves_only_exact_owned_request(isolated):
    async def exercise():
        request = {"action_requests": [{"name": "fixture_write", "args": {"value": "safe"}}]}
        record = _inline("inline-one", request)
        widget = ApprovalMenu(request["action_requests"])
        future = asyncio.get_running_loop().create_future()
        widget.set_future(future)
        _bind(widget, record)
        app = SessionApp(widget)
        async with app.run_test(size=(110, 40)) as pilot:
            service = SessionTaskBoardService(app)
            assert {"approve", "reject", "send", "cancel"} <= service.available_actions(record)
            with pytest.raises(executor.TaskError, match="changed"):
                await asyncio.to_thread(service.respond, record.id, pending_id="stale", decision="approve")
            assert not future.done()
            await asyncio.to_thread(service.respond, record.id, pending_id=record.checkpoint["pending_id"], decision="approve")
            await pilot.pause()
            assert future.result() == {"type": "approve"}

    asyncio.run(exercise())


def test_duplicate_or_foreign_inline_request_cannot_resolve_parent_future(isolated):
    async def exercise():
        request = {"action_requests": [{"name": "fixture_write", "args": {}}]}
        record = _inline("inline-one", request)
        _inline("inline-two", request)
        foreign = _inline("inline-foreign", request, thread="other-thread")
        widget = ApprovalMenu(request["action_requests"])
        future = asyncio.get_running_loop().create_future()
        widget.set_future(future)
        app = SessionApp(widget)
        async with app.run_test(size=(110, 40)):
            service = SessionTaskBoardService(app)
            assert "approve" not in service.available_actions(record)
            for item, message in ((record, "identity"), (foreign, "another parent")):
                with pytest.raises(executor.TaskError, match=message):
                    await asyncio.to_thread(service.respond, item.id, pending_id=item.checkpoint["pending_id"], decision="approve")
            assert not future.done()

    asyncio.run(exercise())


def test_actual_ask_user_widget_answer_and_inline_controls_stay_in_parent(isolated, monkeypatch):
    async def exercise():
        questions = [{"question": "Which file?", "type": "text"}]
        request = {"type": "ask_user", "questions": questions, "tool_call_id": "fixture-call"}
        record = _inline("inline-input", request, state="waiting_input")
        widget = AskUserMenu(questions)
        future = asyncio.get_running_loop().create_future()
        widget.set_future(future)
        _bind(widget, record)
        app = SessionApp(widget)
        async with app.run_test(size=(110, 40)) as pilot:
            service = SessionTaskBoardService(app)
            def forbidden(*args, **kwargs):
                pytest.fail("Inline controls must not invoke a detached executor")
            monkeypatch.setattr(executor, "resume_task", forbidden)
            monkeypatch.setattr(executor, "cancel_task", forbidden)
            await asyncio.to_thread(service.send, record.id, "Use project conventions")
            assert store.unconsumed_inbox(record.id)[0].message == "Use project conventions"
            await asyncio.to_thread(service.respond, record.id, pending_id=record.checkpoint["pending_id"], decision="answer", value="code.py")
            await pilot.pause()
            assert future.result() == {"type": "answered", "answers": ["code.py"]}
            await asyncio.to_thread(service.cancel, record.id)
            assert app.stops == 1
            assert any("parent run" in message for message in app.notices)
            app._pending_ask_user_widget = None
            store.update_task(record.id, state="interrupted", checkpoint={}, expect_state="waiting_input")
            await asyncio.to_thread(service.resume, record.id)
            assert app.resumed == ["/threads -r parent-thread"]

    asyncio.run(exercise())


def test_failed_or_stale_reload_never_claims_saved_definitions_are_active(isolated):
    async def exercise():
        workspace = AgentWorkspace()
        app = SessionApp()
        app._manta_agent_generation = workspace.revision()
        workspace.create(AgentDef(name="new-agent"))
        async def fail(target, **kwargs):
            return False
        app._restart_server_for_agent_swap = fail
        result = AgentWorkspaceResult("reload", "new-agent", True, workspace.revision())
        assert not await apply_agent_workspace_result(app, result)
        assert app._manta_agent_reload_required
        assert any("reload failed" in message for message in app.notices)
        stale = AgentWorkspaceResult("reload", "new-agent", True, workspace.revision())
        workspace.update("new-agent", {"description": "concurrent edit"})
        assert not await apply_agent_workspace_result(app, stale)
        assert any("changed while" in message for message in app.notices)

    asyncio.run(exercise())


def test_overlapping_reload_cannot_clear_another_reloads_ownership(isolated, monkeypatch):
    import threading

    started, release = threading.Event(), threading.Event()
    def held_sync(definitions):
        started.set()
        assert release.wait(timeout=5)
    monkeypatch.setattr(profiles, "sync_agent_profiles", held_sync)

    async def exercise():
        app = SessionApp()
        calls = []
        async def restart(target, **kwargs):
            calls.append(target)
            return True
        app._restart_server_for_agent_swap = restart
        result = AgentWorkspaceResult("reload", "agent", True, AgentWorkspace().revision())
        first = asyncio.create_task(apply_agent_workspace_result(app, result))
        assert await asyncio.to_thread(started.wait, 2)
        assert runtime_busy(app) and app._agent_switching
        assert not await apply_agent_workspace_result(app, result)
        assert app._agent_switching and app._manta_agent_reload_lock.locked()
        release.set()
        assert await first
        assert calls == ["agent"]
        assert not app._agent_switching
    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_inline_retry_requires_owning_parent_and_original_checkout(isolated, monkeypatch):
    record = _inline("retry-owned", {}, state="done")
    foreign = _inline("retry-foreign", {}, state="done", thread="other-parent")
    other_checkout = _inline("retry-elsewhere", {}, state="done", checkout=str(isolated))
    service = SessionTaskBoardService(SessionApp())
    service.agents()
    assert "retry" in service.available_actions(record)
    assert "retry" not in service.available_actions(foreign)
    assert "retry" not in service.available_actions(other_checkout)
    submitted = []
    monkeypatch.setattr(service, "submit", lambda *args, **kwargs: submitted.append((args, kwargs)) or record)
    for item in (foreign, other_checkout):
        with pytest.raises(executor.TaskError, match="original checkout"):
            service.retry(item.id)
    assert not submitted
    service.retry(record.id)
    assert submitted[0][1]["parent_task_id"] == record.id


def test_actual_upstream_stream_per_call_and_native_input_bind_exact_future_ids(isolated, monkeypatch):
    from deepagents_code.app import TextualSessionState
    from deepagents_code.tui import textual_adapter
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from manta_code.tasks.approvals import checkpoint_requests, manta_request_input
    from manta_code.tui.decisions import install_decision_stream_bridge

    class Scripted(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    effects = []
    @tool
    def fixture_write(value: str) -> str:
        """Record a test value after human approval."""
        effects.append(value)
        return value
    model = Scripted(responses=[
        AIMessage(content="", tool_calls=[{"name": "fixture_write", "args": {"value": "APPROVED"}, "id": "write-call", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "manta_request_input", "args": {"question": "Choose the fixture"}, "id": "input-call", "type": "tool_call"}]),
        AIMessage(content="MANTA_VISIBLE_TEAM_OK"),
    ])
    graph = create_agent(model, tools=[fixture_write, manta_request_input], checkpointer=InMemorySaver(),
                         middleware=[HumanInTheLoopMiddleware({"fixture_write": True}, interrupt_mode="per_call")])

    class RecordedGraph:
        def __init__(self):
            self.inputs = []
        async def aput_store_item(self, *args, **kwargs):
            pass
        async def astream(self, value, **kwargs):
            self.inputs.append(value)
            if isinstance(value, Command):
                store.update_task("real-child", state="running", checkpoint={})
            async for chunk in graph.astream(value, **kwargs):
                _, mode, data = chunk
                if mode == "updates" and data.get("__interrupt__"):
                    requests = {item.id: item.value for item in data["__interrupt__"]}
                    checkpoint_requests("real-child", "parent-thread", requests)
                yield chunk

    class LiveApp(SessionApp):
        get_theme_variable_defaults = DeepAgentsApp.get_theme_variable_defaults

        async def mount_message(self, widget):
            await self.query_one(Vertical).mount(widget)
            return True
        async def request_approval(self, actions, assistant_id):
            widget = ApprovalMenu(actions)
            future = asyncio.get_running_loop().create_future()
            widget.set_future(future)
            self._pending_approval_widget = widget
            future.add_done_callback(lambda result: setattr(self, "_pending_approval_widget", None))
            await self.mount_message(widget)
            return future
        async def request_input(self, questions):
            widget = AskUserMenu(questions)
            future = asyncio.get_running_loop().create_future()
            widget.set_future(future)
            self._pending_ask_user_widget = widget
            future.add_done_callback(lambda result: setattr(self, "_pending_ask_user_widget", None))
            await self.mount_message(widget)
            return future

    # Keep the original callable available after this test even when another
    # installer test in this process already installed the bridge.
    monkeypatch.setattr(textual_adapter, "execute_task_textual", textual_adapter.execute_task_textual)
    assert install_decision_stream_bridge()
    source = RecordedGraph()
    store.create_task(store.TaskRecord(id="real-child", agent="review", prompt="Real graph", state="running",
                                      thread_id="parent-thread", execution_kind="inline",
                                      manifest={"parent_thread_id": "parent-thread"}))

    async def exercise():
        app = LiveApp()
        async with asyncio.timeout(10), app.run_test(size=(120, 55)) as pilot:
            adapter = textual_adapter.TextualUIAdapter(app.mount_message, lambda status: None,
                                                       app.request_approval, request_ask_user=app.request_input)
            execution = asyncio.create_task(textual_adapter.execute_task_textual(
                "Run fixture", source, "agent", TextualSessionState(thread_id="parent-thread"), adapter))
            async def wait_widget(attribute):
                for _ in range(150):
                    await pilot.pause(0.01)
                    if execution.done():
                        execution.result()
                    widget = getattr(app, attribute)
                    if widget is not None and getattr(widget, "_manta_interrupts", None):
                        return widget
                pytest.fail(f"No bound live widget: {attribute}")
            widget = await wait_widget("_pending_approval_widget")
            record = store.get_task("real-child")
            raw = next(iter(record.checkpoint["interrupts"].values()))
            assert raw["type"] == "tool_approval" and raw["tool_call_id"] == "write-call"
            assert widget._manta_interrupts == record.checkpoint["interrupts"]
            assert not effects
            stale = _inline("stale-identical", raw)
            service = SessionTaskBoardService(app)
            assert "approve" not in service.available_actions(stale)
            with pytest.raises(executor.TaskError, match="identity"):
                await asyncio.to_thread(service.respond, stale.id, pending_id=stale.checkpoint["pending_id"], decision="approve")
            await asyncio.to_thread(service.respond, record.id, pending_id=record.checkpoint["pending_id"], decision="approve")
            input_widget = await wait_widget("_pending_ask_user_widget")
            record = store.get_task("real-child")
            assert input_widget._manta_interrupts == record.checkpoint["interrupts"]
            await asyncio.to_thread(service.respond, record.id, pending_id=record.checkpoint["pending_id"], decision="answer", value="fixture.py")
            await asyncio.wait_for(execution, 5)
            assert effects == ["APPROVED"]
            assert next(iter(source.inputs[1].resume.values())) == {"type": "approve"}
            assert next(iter(source.inputs[2].resume.values()))["answers"] == ["fixture.py"]
            state = await graph.aget_state({"configurable": {"thread_id": "parent-thread"}})
            assert state.values["messages"][-1].content == "MANTA_VISIBLE_TEAM_OK"
            assert adapter._request_approval == app.request_approval
    asyncio.run(exercise())


def test_installed_routes_create_reload_then_dispatch_new_actual_child(isolated, monkeypatch):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from manta_code.agents.factory import compile_subagent

    class Scripted(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    async def original_command(self, text):
        self.other_commands.append(text)

    async def original_send(self, text, **kwargs):
        self.sent.append(text)
        self.dispatched = self.graph.invoke({"messages": [HumanMessage(content=text)]})

    async def original_criteria(self, request):
        self._agent_running = True

    monkeypatch.setattr(DeepAgentsApp, "__init__", lambda self: None)
    monkeypatch.setattr(DeepAgentsApp, "_handle_command", original_command)
    monkeypatch.setattr(DeepAgentsApp, "_send_to_agent", original_send)
    monkeypatch.setattr(DeepAgentsApp, "_run_goal_criteria_request", original_criteria)
    monkeypatch.setattr(DeepAgentsApp, "_show_agent_selector", DeepAgentsApp._show_agent_selector)
    from deepagents_code import command_registry
    for name in ("COMMANDS", "SIDE_EFFECT_FREE", "QUEUE_BOUND", "ALL_CLASSIFIED"):
        monkeypatch.setattr(command_registry, name, getattr(command_registry, name))
    assert install_visible_agent_team()
    assert install_visible_agent_team()

    class Harness(SessionApp):
        _handle_command = DeepAgentsApp._handle_command
        _send_to_agent = DeepAgentsApp._send_to_agent

        def __init__(self):
            super().__init__()
            self.other_commands = []
            self.sent = []
            self.reloads = 0
            self._manta_agent_generation = AgentWorkspace().revision()
            self._message_store = SimpleNamespace(get_all_messages=lambda: [])

        def _effective_model_spec(self):
            return "databricks:system.ai.gpt-6-1-sol"

        async def _restart_server_for_agent_swap(self, target, **kwargs):
            self.reloads += 1
            parent = Scripted(responses=[AIMessage(content="", tool_calls=[{
                "name": "task", "args": {"subagent_type": "visible-helper", "description": "Reply NEW_AGENT_OK"},
                "id": "new-child", "type": "tool_call"}]), AIMessage(content="DONE")])
            specs = []
            for definition in AgentWorkspace().list():
                spec = compile_subagent(definition)
                spec["model"] = Scripted(responses=[AIMessage(content="NEW_AGENT_OK")])
                specs.append(spec)
            self.graph = create_deep_agent(parent, subagents=specs)
            self._assistant_id = target
            return True

    async def exercise():
        app = Harness()
        async with app.run_test(size=(125, 58)) as pilot:
            await app._handle_command("/agents")
            await pilot.pause()
            assert isinstance(app.screen, AgentWorkspaceScreen)
            await pilot.click("#workspace-create")
            await pilot.pause()
            assert isinstance(app.screen, AgentEditorScreen)
            app.screen.query_one("#agent-name", Input).value = "visible-helper"
            app.screen.query_one("#agent-prompt", TextArea).load_text("A visible specialist")
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.click("#workspace-reload")
            await pilot.pause(0.5)
            assert app.reloads == 1
            assert not app._manta_agent_reload_required
            await app._send_to_agent("Delegate to visible-helper")
            assert any("NEW_AGENT_OK" in str(message.content) for message in app.dispatched["messages"])
            AgentWorkspace().update("visible-helper", {"description": "external edit"})
            await app._send_to_agent("Do not send through stale graph")
            assert app.sent == ["Delegate to visible-helper"]
            assert app._manta_agent_reload_required
            app._agent_running = True
            await app._handle_command("/agents")
            await pilot.pause()
            assert not app.screen.editable
            await pilot.press("escape")
            await app._handle_command("/tasks")
            await pilot.pause()
            assert isinstance(app.screen, TaskBoardScreen)
            await pilot.press("escape")
            await app._handle_command("/team")
            await pilot.pause()
            assert isinstance(app.screen, TaskBoardScreen)

    asyncio.run(exercise())


def test_goal_criteria_guard_precedes_new_run_state_and_preserves_checkpoint_continuations(isolated, monkeypatch):
    calls = []

    async def original_command(self, text):
        pass

    async def original_send(self, text):
        calls.append(("send", text))

    async def original_criteria(self, request):
        self._agent_running = True
        calls.append(("criteria", request))

    async def original_run(self, *args, **kwargs):
        calls.append(("checkpoint", args))

    monkeypatch.setattr(DeepAgentsApp, "__init__", lambda self: None)
    monkeypatch.setattr(DeepAgentsApp, "_handle_command", original_command)
    monkeypatch.setattr(DeepAgentsApp, "_send_to_agent", original_send)
    monkeypatch.setattr(DeepAgentsApp, "_run_goal_criteria_request", original_criteria)
    monkeypatch.setattr(DeepAgentsApp, "_run_agent_task", original_run)
    from deepagents_code import command_registry
    for name in ("COMMANDS", "SIDE_EFFECT_FREE", "QUEUE_BOUND", "ALL_CLASSIFIED"):
        monkeypatch.setattr(command_registry, name, getattr(command_registry, name))
    assert install_visible_agent_team()
    app = SessionApp()
    workspace = AgentWorkspace()
    app._manta_agent_generation = workspace.revision()

    async def exercise():
        await DeepAgentsApp._run_goal_criteria_request(app, {"request_id": "fresh"})
        assert calls == [("criteria", {"request_id": "fresh"})]
        app._agent_running = False
        workspace.update("review", {"description": "saved outside the app"})
        await DeepAgentsApp._run_goal_criteria_request(app, {"request_id": "stale"})
        assert calls == [("criteria", {"request_id": "fresh"})]
        assert not app._agent_running
        assert app._manta_agent_reload_required
        assert any("Apply reload" in message for message in app.notices)
        # Existing graph checkpoints remain owned by their original run. The
        # new-run guards must not intercept their low-level continuation.
        await DeepAgentsApp._run_agent_task(app, "", resume_value={"type": "approve"})
        assert calls[-1][0] == "checkpoint"

    asyncio.run(exercise())


def test_busy_predicate_covers_pending_decisions_and_transition():
    for name in ("_agent_running", "_shell_running", "_agent_switching", "_connecting",
                 "_pending_approval_widget", "_pending_ask_user_widget"):
        assert runtime_busy(SimpleNamespace(**{name: True}))
