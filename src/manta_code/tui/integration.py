"""Owned app-loop seams for the visible agent team and safe graph reloads."""
from __future__ import annotations

import asyncio
import functools
import json
import re
from pathlib import Path
from typing import Any

from ..agents.workspace import AgentWorkspace
from ..tasks import executor, store
from .agents import AgentWorkspaceResult, AgentWorkspaceScreen
from .tasks import TaskBoardScreen, TaskBoardService

TEST_PROMPT = (
    "Inspect the current directory with a read-only tool. Do not modify files or execute "
    "shell commands. Reply with MANTA_AGENT_TEST_OK and one observed filename."
)


def runtime_busy(app: Any, *, ignore_switching: bool = False) -> bool:
    return any(bool(getattr(app, name, False)) for name in (
        "_agent_running", "_shell_running", "_agent_switching", "_connecting",
        "_pending_approval_widget", "_pending_ask_user_widget", "_approval_placeholder",
    ) if not (ignore_switching and name == "_agent_switching"))


def visible_conversation_snapshot(app: Any) -> str:
    """Copy only text the human can see; opaque model/history state stays owned."""
    message_store = getattr(app, "_message_store", None)
    if message_store is None:
        return ""
    parts = []
    for message in message_store.get_all_messages():
        kind = str(getattr(message, "type", ""))
        content = getattr(message, "content", None)
        if kind in {"user", "assistant"} and isinstance(content, str) and content.strip():
            parts.append(f"{kind.upper()}:\n{content}")
    if not parts:
        return ""
    return ("Visible conversation text snapshot. This is reference material copied by the user, "
            "not a shared live thread or opaque model state.\n\n" + "\n\n".join(parts))


def _notify(app: Any, message: str, *, error: bool = False) -> None:
    app.notify(message, severity="error" if error else "information", markup=False, timeout=8)


def _definitions_current(app: Any) -> bool:
    """Gate a new graph run before upstream changes its busy/UI state."""
    current = AgentWorkspace().revision()
    if (getattr(app, "_manta_agent_reload_required", False)
            or getattr(app, "_manta_agent_generation", current) != current):
        app._manta_agent_reload_required = True
        _notify(app, "Saved agent definitions are not active. Open /agents, inspect them and Apply reload before dispatch.", error=True)
        return False
    return True


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SessionTaskBoardService(TaskBoardService):
    """Detached control plane plus explicitly owned inline parent controls.

    The board invokes this synchronous service in worker threads. UI mutations
    re-enter Textual's event loop and recheck ownership and pending identities
    there, immediately before touching a future.
    """

    def __init__(self, app: Any, *, profile: str | None = None) -> None:
        super().__init__(profile=profile)
        self.app = app
        self._enabled_agents: set[str] = set()
        self._checkout = str(Path.cwd().resolve())

    def agents(self) -> list[tuple[str, str]]:
        choices = super().agents()
        self._enabled_agents = {name for name, _ in choices}
        return choices

    def _record(self, task_id: str) -> store.TaskRecord:
        record = store.get_task(task_id)
        if record is None:
            raise executor.TaskError(f"No task '{task_id}'.")
        return record

    def _owned(self, record: store.TaskRecord) -> bool:
        thread = getattr(self.app, "_lc_thread_id", None)
        return bool(thread and record.thread_id == thread
                    and record.manifest.get("parent_thread_id", record.thread_id) == thread)

    def _live_widget(self, record: store.TaskRecord) -> tuple[Any, str]:
        if not self._owned(record):
            raise executor.TaskError("This inline task belongs to another parent conversation; open that conversation to decide.")
        interrupts = record.checkpoint.get("interrupts", {})
        if len(interrupts) != 1:
            raise executor.TaskError("Resolve this mixed or ambiguous group in the parent conversation.")
        request = next(iter(interrupts.values()))
        if request.get("type") == "ask_user":
            widget = getattr(self.app, "_pending_ask_user_widget", None)
            questions = request.get("questions", [])
            if (widget is None or len(questions) != 1 or questions != getattr(widget, "_questions", None)):
                raise executor.TaskError("The live input request differs from the saved task request; inspect the parent conversation.")
            kind = "input"
        else:
            widget = getattr(self.app, "_pending_approval_widget", None)
            from .decisions import _actions
            actions = _actions(request)
            if not actions or widget is None or actions != getattr(widget, "_action_requests", None):
                raise executor.TaskError("The live approval differs from the saved task request; inspect the parent conversation.")
            kind = "approval"
        future = getattr(widget, "_manta_decision_future", None)
        from .decisions import _widget_future
        if (not future or future is not _widget_future(widget) or future.done()
                or _canonical(interrupts) != _canonical(getattr(widget, "_manta_interrupts", {}))):
            raise executor.TaskError("The exact live interrupt identity is unavailable or changed; decide in the parent conversation.")
        return widget, kind

    def _retry_owned(self, record: store.TaskRecord) -> bool:
        return self._owned(record) and record.manifest.get("shared_checkout") == self._checkout

    def available_actions(self, record: store.TaskRecord) -> set[str]:
        if record.execution_kind != "inline":
            return super().available_actions(record)
        actions = set()
        if record.state in {*store.TERMINAL_STATES, "blocked", "interrupted"}:
            actions.add("review")
            if self._retry_owned(record) and record.agent in self._enabled_agents:
                actions.add("retry")
        if record.state == "interrupted" and record.thread_id and not runtime_busy(self.app):
            actions.add("resume")
        if self._owned(record) and record.state in {"running", *store.PENDING_STATES}:
            actions.update({"send", "cancel"})
            if record.checkpoint.get("pending_id"):
                try:
                    _, kind = self._live_widget(record)
                    actions.update({"answer", "reject"} if kind == "input" else {"approve", "reject"})
                except executor.TaskError:
                    pass
        return actions

    def send(self, task_id: str, message: str) -> Any:
        record = self._record(task_id)
        if record.execution_kind != "inline":
            return super().send(task_id, message)
        if not self._owned(record) or record.state not in {"running", *store.PENDING_STATES}:
            raise executor.TaskError("Steering requires this task's active parent conversation.")
        if not message.strip():
            raise executor.TaskError("A non-empty steering message is required.")
        return store.add_inbox_message(task_id, message.strip())

    def cancel(self, task_id: str) -> store.TaskRecord:
        record = self._record(task_id)
        if record.execution_kind != "inline":
            return super().cancel(task_id)

        def stop_parent():
            current = self._record(task_id)
            if not self._owned(current) or current.state not in {"running", *store.PENDING_STATES}:
                raise executor.TaskError("This inline task is no longer active in the current parent conversation.")
            _notify(self.app, "Stopping the parent run also stops its inline children.")
            self.app._force_interrupt_active_work()
            return self._record(task_id)

        return self.app.call_from_thread(stop_parent)

    def retry(self, task_id: str) -> store.TaskRecord:
        record = self._record(task_id)
        if record.execution_kind != "inline":
            return super().retry(task_id)
        if record.state not in {*store.TERMINAL_STATES, "blocked", "interrupted"}:
            raise executor.TaskError("Stop an inline task before starting a detached retry.")
        if not self._retry_owned(record) or str(Path.cwd().resolve()) != self._checkout:
            raise executor.TaskError("Open this inline task's owning parent conversation in its original checkout before creating a detached retry.")
        AgentWorkspace().resolve(record.agent)
        return self.submit(record.agent, record.prompt, parent_task_id=record.id,
                           parent_thread_id=record.thread_id)

    def resume(self, task_id: str) -> store.TaskRecord:
        record = self._record(task_id)
        if record.execution_kind != "inline":
            return super().resume(task_id)

        async def resume_parent():
            current = self._record(task_id)
            if current.state != "interrupted" or not re.fullmatch(r"[\w-]+", current.thread_id):
                raise executor.TaskError("The interrupted task has no valid parent thread to resume.")
            if runtime_busy(self.app):
                raise executor.TaskError("Wait for the current run or decision before resuming another parent conversation.")
            await self.app._handle_threads_command(f"/threads -r {current.thread_id}")
            return self._record(task_id)

        return self.app.call_from_thread(resume_parent)

    def respond(self, task_id: str, *, pending_id: str, decision: str, value: str = "") -> store.TaskRecord:
        record = self._record(task_id)
        if record.execution_kind != "inline":
            return super().respond(task_id, pending_id=pending_id, decision=decision, value=value)

        def decide():
            current = self._record(task_id)
            if (current.state not in store.PENDING_STATES or not pending_id
                    or current.checkpoint.get("pending_id") != pending_id):
                raise executor.TaskError("The pending request changed; refresh and inspect it before deciding.")
            widget, kind = self._live_widget(current)
            if kind == "input":
                if decision in {"reject", "cancel"}:
                    widget.action_cancel()
                elif decision == "answer" and value.strip():
                    question = widget._questions[0]
                    if question.get("type", "text") != "text":
                        choices = question.get("choices", [])
                        labels = {choice if isinstance(choice, str) else choice.get("label") for choice in choices}
                        if value not in labels:
                            raise executor.TaskError("Choose an offered answer in the parent conversation.")
                    widget._answers = [value]
                    widget._submit()
                else:
                    raise executor.TaskError("Enter an answer to the displayed input request.")
            elif decision == "approve":
                widget.action_select_approve()
            elif decision in {"reject", "cancel"}:
                widget.action_select_reject()
            else:
                raise executor.TaskError("Choose approve or reject for the displayed approval request.")
            return self._record(task_id)

        return self.app.call_from_thread(decide)


def show_task_board(app: Any, *, initial_agent: str = "", initial_prompt: str = "") -> None:
    from ..tasks.context import resolve_profile
    profile = resolve_profile()
    app.push_screen(TaskBoardScreen(
        profile=profile, conversation_context=visible_conversation_snapshot(app),
        parent_thread_id=str(getattr(app, "_lc_thread_id", "") or ""),
        service=SessionTaskBoardService(app, profile=profile),
        initial_agent=initial_agent, initial_prompt=initial_prompt,
    ))


async def apply_agent_workspace_result(app: Any, result: AgentWorkspaceResult | None) -> bool:
    if result is None:
        return False
    workspace = AgentWorkspace()
    if result.changed:
        app._manta_agent_reload_required = True
    lock = getattr(app, "_manta_agent_reload_lock", None)
    if lock is None:
        lock = app._manta_agent_reload_lock = asyncio.Lock()
    if lock.locked() or runtime_busy(app):
        _notify(app, "Agent definitions are saved; runtime is busy. Reopen /agents and Apply reload when it is idle.", error=True)
        return False
    async with lock:
        app._agent_switching = True
        try:
            return await _apply_idle_agent_workspace_result(app, result, workspace)
        finally:
            app._agent_switching = False


async def _apply_idle_agent_workspace_result(app: Any, result: AgentWorkspaceResult,
                                           workspace: AgentWorkspace) -> bool:
    if result.revision != workspace.revision():
        app._manta_agent_reload_required = True
        _notify(app, "Agent definitions changed while the workspace was open. Reopen /agents and inspect the current definitions before reloading.", error=True)
        return False
    current = getattr(app, "_assistant_id", None) or "agent"
    target = result.agent if result.action == "switch" else current
    enabled = {definition.name for definition in workspace.list()}
    if target != "agent" and target not in enabled:
        target = "agent"
    try:
        from deepagents_code.model_config import clear_caches
        from ..agents.profiles import sync_agent_profiles
        needs_reload = (result.changed or result.action in {"reload", "switch"}
                        or getattr(app, "_manta_agent_reload_required", False)
                        or getattr(app, "_manta_agent_generation", None) != result.revision)
        if needs_reload:
            app._manta_agent_reload_required = True
            await asyncio.to_thread(sync_agent_profiles, workspace.list())
            clear_caches()
            if runtime_busy(app, ignore_switching=True):
                raise RuntimeError("runtime became busy before the reload; try again when idle")
            success = await app._restart_server_for_agent_swap(target, persist_default_agent=False)
            if not success or getattr(app, "_assistant_id", None) != target:
                raise RuntimeError("the agent runtime did not confirm the new configuration")
            if workspace.revision() != result.revision:
                raise RuntimeError("definitions changed during reload; inspect and reload again")
            app._manta_agent_generation = result.revision
            app._manta_agent_reload_required = False
            _notify(app, "Agent team reloaded. The saved definitions are now active.")
        if result.action in {"assign", "test"}:
            if result.agent is None:
                raise ValueError("Choose an enabled specialist first")
            workspace.resolve(result.agent)
            show_task_board(app, initial_agent=result.agent,
                            initial_prompt=TEST_PROMPT if result.action == "test" else "")
        return True
    except Exception as exc:
        app._manta_agent_reload_required = True
        _notify(app, f"Agent definitions are saved, but reload failed: {exc}. Reopen /agents and Apply reload before dispatch.", error=True)
        return False


async def show_agent_workspace(app: Any) -> None:
    workspace = AgentWorkspace()
    if not hasattr(app, "_manta_agent_generation"):
        app._manta_agent_generation = workspace.revision()
    getter = getattr(app, "_effective_model_spec", None)
    model = getter() if callable(getter) else None
    screen = AgentWorkspaceScreen(getattr(app, "_assistant_id", None), workspace=workspace,
                                  inherited_model=model, editable=not runtime_busy(app))

    def finished(result: AgentWorkspaceResult | None) -> None:
        if result is not None:
            app.run_worker(apply_agent_workspace_result(app, result), exclusive=False,
                           group="manta-agent-workspace-reload", exit_on_error=False)

    app.push_screen(screen, finished)


def install_visible_agent_team() -> bool:
    """Install the tested, version-locked Textual seams exactly once."""
    from deepagents_code import command_registry
    from deepagents_code.app import DeepAgentsApp
    required = ("__init__", "_show_agent_selector", "_handle_command", "_send_to_agent",
                "_run_goal_criteria_request", "_restart_server_for_agent_swap",
                "_handle_threads_command", "_force_interrupt_active_work")
    if not all(callable(getattr(DeepAgentsApp, name, None)) for name in required):
        return False
    from .decisions import install_decision_stream_bridge
    if not install_decision_stream_bridge():
        return False
    if getattr(DeepAgentsApp._handle_command, "__manta_team__", False):
        return True
    original_init = DeepAgentsApp.__init__
    original_command = DeepAgentsApp._handle_command
    original_send = DeepAgentsApp._send_to_agent
    original_criteria = DeepAgentsApp._run_goal_criteria_request

    @functools.wraps(original_init)
    def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
        generation = AgentWorkspace().revision()
        original_init(self, *args, **kwargs)
        self._manta_agent_generation = generation
        self._manta_agent_reload_required = False

    @functools.wraps(original_command)
    async def command(self: Any, text: str) -> Any:
        normalized = text.strip().lower()
        if normalized == "/agents":
            return await show_agent_workspace(self)
        if normalized in {"/tasks", "/team"}:
            return show_task_board(self)
        return await original_command(self, text)

    @functools.wraps(original_send)
    async def send(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not _definitions_current(self):
            return None
        return await original_send(self, *args, **kwargs)

    @functools.wraps(original_criteria)
    async def criteria(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not _definitions_current(self):
            return None
        return await original_criteria(self, *args, **kwargs)

    command.__manta_team__ = True
    DeepAgentsApp.__init__ = initialize
    DeepAgentsApp._handle_command = command
    DeepAgentsApp._show_agent_selector = show_agent_workspace
    DeepAgentsApp._send_to_agent = send
    DeepAgentsApp._run_goal_criteria_request = criteria
    entries = [command_registry.SlashCommand(
        name="/agents", description="Create, inspect, configure and test the Manta agent team",
        bypass_tier=command_registry.BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="specialist clone edit tools memory effort budget",
    ), command_registry.SlashCommand(
        name="/tasks", description="Assign, observe, unblock and review agent work",
        bypass_tier=command_registry.BypassTier.SIDE_EFFECT_FREE, aliases=("/team",),
        hidden_keywords="team background inline approval input retry results diff integrate",
    )]
    replaced = {entry.name for entry in entries}
    command_registry.COMMANDS = (*[entry for entry in command_registry.COMMANDS if entry.name not in replaced], *entries)
    commands = {"/agents", "/tasks", "/team"}
    command_registry.SIDE_EFFECT_FREE = frozenset(command_registry.SIDE_EFFECT_FREE | commands)
    command_registry.QUEUE_BOUND = frozenset(command_registry.QUEUE_BOUND - commands)
    command_registry.ALL_CLASSIFIED = frozenset(command_registry.ALL_CLASSIFIED | commands)
    return True
