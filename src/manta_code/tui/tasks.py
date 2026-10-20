"""The in-app task control plane, backed by the same durable jobs as the CLI.

All filesystem, SQLite, process and Git work runs off the Textual event loop.
The service is deliberately injectable: the app can attach controls for its
currently running inline children without giving another session their controls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TabbedContent, TabPane, TextArea

from ..tasks import executor, store


def _has_input_request(record: store.TaskRecord) -> bool:
    return any(
        request.get("type") == "ask_user" or any(
            action.get("name") == "manta_request_input" for action in request.get("action_requests", [])
        )
        for request in record.checkpoint.get("interrupts", {}).values()
    )


@dataclass(frozen=True)
class TaskBoardDetails:
    """One selected task's persisted evidence, with untrusted text kept literal."""

    record: store.TaskRecord
    output: str = ""
    events: str = ""
    result: dict | None = None


class TaskBoardService:
    """Synchronous public task operations; the screen calls these in workers."""

    def __init__(self, *, profile: str | None = None) -> None:
        self.profile = profile

    def agents(self) -> list[tuple[str, str]]:
        from .. import sdk

        return [(a.name, a.description or a.name) for a in sdk.agents()
                if getattr(a, "enabled", True)]

    def list_tasks(self) -> list[store.TaskRecord]:
        from ..tasks.inline import interrupt_orphans

        interrupt_orphans()
        executor.reconcile_stale_tasks()
        executor.schedule_tasks()
        return store.list_tasks(limit=200)

    def details(self, task_id: str) -> TaskBoardDetails:
        record = store.get_task(task_id)
        if record is None:
            raise executor.TaskError(f"No task '{task_id}'.")
        events = store.recent_events(task_id=task_id, limit=100)
        return TaskBoardDetails(
            record=record,
            output=executor.task_output(task_id),
            events="\n".join(f"{event.kind}: {event.detail}" for event in reversed(events)),
            result=getattr(record, "structured_result", None) or {},
        )

    def submit(
        self, agent: str, prompt: str, *, parent_task_id: str = "",
        dependencies: list[str] | None = None, context_mode: str = "isolated",
        conversation_context: str = "", parent_thread_id: str = "",
    ) -> store.TaskRecord:
        return executor.submit_task(
            agent, prompt, profile=self.profile, parent_task_id=parent_task_id or None,
            dependencies=dependencies or [], context_mode=context_mode,
            conversation_context=conversation_context, parent_thread_id=parent_thread_id,
        )

    def send(self, task_id: str, message: str) -> Any:
        return executor.send_to_task(task_id, message)

    def cancel(self, task_id: str) -> store.TaskRecord:
        return executor.cancel_task(task_id)

    def retry(self, task_id: str) -> store.TaskRecord:
        return executor.retry_task(task_id)

    def resume(self, task_id: str) -> store.TaskRecord:
        record = store.get_task(task_id)
        if record is None:
            raise executor.TaskError(f"No task '{task_id}'.")
        return executor.resume_task(task_id, resume_value=record.checkpoint.get("resume_value"))

    def respond(
        self, task_id: str, *, pending_id: str, decision: str, value: str = "",
    ) -> store.TaskRecord:
        from ..tasks.approvals import respond_task

        return respond_task(task_id, pending_id=pending_id, decision=decision, value=value)

    def review(self, task_id: str) -> dict:
        return executor.review_task(task_id)

    def integrate(self, task_id: str, *, expected_patch_sha256: str | None = None) -> store.TaskRecord:
        return executor.integrate_task(task_id, expected_patch_sha256=expected_patch_sha256)

    def discard(self, task_id: str) -> store.TaskRecord:
        return executor.discard_task(task_id)

    def available_actions(self, record: store.TaskRecord) -> set[str]:
        """Inline controls belong to the owning live app, never a guessed PID."""
        if getattr(record, "execution_kind", "detached") == "inline":
            return {"review"} if record.state in {"done", "failed", "cancelled"} else set()
        actions = set()
        if record.state in {"queued", "running", "starting", "waiting_dependencies"}:
            actions.add("send")
        if record.state in {
            "queued", "running", "starting", "waiting_dependencies", "waiting_input",
            "waiting_approval", "paused_budget", "blocked", "interrupted",
        }:
            actions.add("cancel")
        unreviewed = getattr(record, "integration_state", "unreviewed") == "unreviewed"
        if (record.state in {"failed", "cancelled", "blocked", "interrupted"}
                and unreviewed and not (record.thread_id and record.checkpoint)):
            actions.add("retry")
        if (record.state == "interrupted" and record.thread_id
                and record.checkpoint.get("resume_value")):
            actions.add("resume")
        if record.checkpoint.get("pending_id"):
            if record.state in {"waiting_approval", "paused_budget"}:
                actions.update({"answer" if _has_input_request(record) else "approve", "reject"})
            elif record.state == "waiting_input":
                actions.add("answer")
        elif (record.state == "paused_budget" and record.thread_id
              and record.checkpoint.get("resume_value")):
            actions.add("resume")
        if record.state in {"done", "failed", "cancelled", "blocked", "interrupted"}:
            actions.add("review")
            if unreviewed and record.manifest.get("isolation"):
                actions.add("discard")
                if record.state == "done":
                    actions.add("integrate")
        return actions


class _ConfirmTaskAction(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", show=False)]
    CSS = """
    _ConfirmTaskAction { align: center middle; }
    _ConfirmTaskAction > Vertical {
        width: 70; max-width: 95%; height: auto; padding: 1 2;
        background: $surface; border: solid $primary;
    }
    _ConfirmTaskAction Static { height: auto; margin-bottom: 1; }
    _ConfirmTaskAction Horizontal { height: auto; align-horizontal: right; }
    """

    def __init__(self, message: str, action_label: str) -> None:
        super().__init__()
        self.message = message
        self.action_label = action_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message, markup=False)
            with Horizontal():
                yield Button("Back", id="confirm-back")
                yield Button(self.action_label, id="confirm-action", variant="warning")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-action")

    def action_cancel(self) -> None:
        self.dismiss(False)


class TaskBoardScreen(ModalScreen[None]):
    """Assign, observe, unblock and review a visible Manta agent team."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]
    CSS = """
    TaskBoardScreen { align: center middle; }
    TaskBoardScreen > Vertical {
        width: 96%; max-width: 150; height: 96%; padding: 0 1;
        background: $surface; border: solid $primary;
    }
    TaskBoardScreen #task-board-title { text-style: bold; height: 1; color: $primary; }
    TaskBoardScreen #task-board-tabs { height: 1fr; }
    TaskBoardScreen #task-table { height: 9; min-height: 4; }
    TaskBoardScreen #task-detail-tabs { height: 1fr; min-height: 7; }
    TaskBoardScreen TextArea { height: 1fr; }
    TaskBoardScreen .task-actions { height: 3; }
    TaskBoardScreen .task-actions Button { min-width: 8; width: auto; padding: 0 1; }
    TaskBoardScreen #task-message { width: 1fr; }
    TaskBoardScreen #task-board-status { height: 2; color: $text-muted; }
    TaskBoardScreen #task-board-status.error { color: $error; }
    TaskBoardScreen #task-board-help { height: 1; color: $text-muted; }
    TaskBoardScreen #task-assign-scroll { height: 1fr; }
    TaskBoardScreen #task-assign-scroll Label { margin-top: 1; }
    TaskBoardScreen #task-prompt { height: 7; min-height: 4; }
    TaskBoardScreen #task-assign-note { height: auto; margin: 1 0; color: $text-muted; }
    TaskBoardScreen .task-form-row { height: auto; }
    TaskBoardScreen .task-form-row > Vertical { width: 1fr; height: auto; }
    TaskBoardScreen.compact #task-table { height: 4; min-height: 3; }
    TaskBoardScreen.compact #task-detail-tabs { min-height: 4; }
    TaskBoardScreen.compact #task-board-status { height: 1; }
    """

    def __init__(
        self, *, profile: str | None = None, conversation_context: str = "",
        parent_thread_id: str = "", service: TaskBoardService | None = None,
        refresh_interval: float = 2.0, initial_agent: str = "", initial_prompt: str = "",
    ) -> None:
        super().__init__()
        self.service = service or TaskBoardService(profile=profile)
        self.conversation_context = conversation_context
        self.parent_thread_id = parent_thread_id
        self.refresh_interval = refresh_interval
        self.initial_agent = initial_agent
        self.initial_prompt = initial_prompt
        self.selected_task_id: str | None = None
        self.selected_record: store.TaskRecord | None = None
        self._records: dict[str, store.TaskRecord] = {}
        self._busy = False
        self._refreshing = False
        self._refresh_requested = False
        self._rendering = False
        self._revision = 0
        self._detail_revision = 0
        self._reviewed: dict[str, str] = {}
        self._agent_options: list[tuple[str, str]] = []
        self._last_error = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Manta · Agent team", id="task-board-title", markup=False)
            with TabbedContent(initial="team-assign" if self.initial_agent or self.initial_prompt else "team-tasks", id="task-board-tabs"):
                with TabPane("Tasks", id="team-tasks"):
                    yield DataTable(id="task-table", cursor_type="row", zebra_stripes=True)
                    with TabbedContent(id="task-detail-tabs"):
                        with TabPane("Details", id="task-details-pane"):
                            yield TextArea(read_only=True, id="task-details")
                        with TabPane("Logs", id="task-logs-pane"):
                            yield TextArea(read_only=True, id="task-logs")
                        with TabPane("Diff", id="task-diff-pane"):
                            yield TextArea(read_only=True, soft_wrap=False, id="task-diff")
                        with TabPane("Evidence", id="task-evidence-pane"):
                            yield TextArea(read_only=True, id="task-evidence")
                    with Horizontal(classes="task-actions"):
                        yield Input(placeholder="Steering message or answer to the displayed request", id="task-message")
                        yield Button("Send", id="task-send")
                        yield Button("Respond", id="task-answer")
                    with HorizontalScroll(classes="task-actions"):
                        for key, label in (
                            ("cancel", "Cancel"), ("retry", "Retry"), ("resume", "Resume"),
                            ("approve", "Approve"), ("reject", "Reject"), ("review", "Review"),
                            ("integrate", "Integrate"), ("discard", "Discard"),
                        ):
                            yield Button(label, id=f"task-{key}", tooltip={
                                "retry": "Start another attempt in the same isolated workspace",
                                "resume": "Continue the saved checkpoint without replaying the task",
                                "approve": "Approve every action in the request displayed in Details",
                                "reject": "Reject the displayed pending request",
                                "review": "Collect the observed diff, artifacts, tests and spend",
                                "integrate": "Review this task first, then apply its exact diff to the source project",
                                "discard": "Remove the isolated workspace and retain the recorded result",
                                "cancel": "Stop this task and keep its workspace for review",
                            }[key])
                        yield Button("Refresh", id="task-refresh")
                with TabPane("Assign task", id="team-assign"):
                    with VerticalScroll(id="task-assign-scroll"):
                        yield Label("Agent")
                        yield Select([], prompt="Select an enabled specialist", id="task-agent")
                        yield Label("Task")
                        yield TextArea(self.initial_prompt, id="task-prompt", placeholder="Describe the result and how to verify it")
                        with Horizontal(classes="task-form-row"):
                            with Vertical():
                                yield Label("Parent task (optional)")
                                yield Input(placeholder="Task ID", id="task-parent")
                            with Vertical():
                                yield Label("Dependencies (optional)")
                                yield Input(placeholder="Comma-separated task IDs", id="task-dependencies")
                        yield Label("Conversation context")
                        options = [("Isolated — task prompt only", "isolated")]
                        if self.conversation_context:
                            options.append(("Fork — include the visible conversation snapshot", "fork"))
                        yield Select(options, value="isolated", allow_blank=False, id="task-context")
                        yield Static(
                            "Writable tasks use an isolated worktree or directory. Dependencies must finish "
                            "before execution; their changes are reviewed and integrated separately. "
                            + ("Fork includes a copy of the visible conversation, not a live shared thread."
                               if self.conversation_context else "No conversation snapshot is available; context is isolated."),
                            id="task-assign-note", markup=False,
                        )
                        yield Button("Assign task", id="task-submit", variant="primary")
            yield Static("Loading tasks…", id="task-board-status", markup=False)
            yield Static("Esc close · Ctrl+R refresh · approvals apply only to the request shown", id="task-board-help", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("Task", "Agent", "State", "Kind", "Parent", "Depends on")
        self._set_actions()
        self._refresh_worker()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self._poll)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.height < 34, "compact")

    def _status(self, message: str, *, error: bool = False) -> None:
        if not self.is_mounted:
            return
        widget = self.query_one("#task-board-status", Static)
        widget.update(message)
        widget.set_class(error, "error")
        if error:
            self._last_error = message

    def _poll(self) -> None:
        if not self._busy and not self._refreshing:
            self._refresh_worker()

    def action_refresh(self) -> None:
        if not self._busy:
            self._last_error = ""
            self._refresh_worker()

    @work(group="task-board-refresh", exclusive=False, exit_on_error=False)
    async def _refresh_worker(self) -> None:
        if self._refreshing:
            self._refresh_requested = True
            return
        self._refreshing = True
        revision = self._revision
        try:
            records, agents = await asyncio.gather(
                asyncio.to_thread(self.service.list_tasks), asyncio.to_thread(self.service.agents),
            )
            if not self.is_mounted or revision != self._revision:
                return
            self._render_rows(records)
            if agents != self._agent_options:
                self._agent_options = agents
                select = self.query_one("#task-agent", Select)
                current = select.value
                select.set_options([(f"{name} · {description}", name) for name, description in agents])
                valid = {name for name, _ in agents}
                if current in valid:
                    select.value = current
                elif self.initial_agent in valid:
                    select.value = self.initial_agent
                elif len(agents) == 1:
                    select.value = agents[0][0]
            if self.selected_task_id:
                await self._load_details(self.selected_task_id)
            elif not self._last_error:
                self._status("No tasks yet. Choose Assign task to give an agent work.")
        except Exception as exc:
            self._status(f"Could not refresh tasks: {exc}", error=True)
        finally:
            self._refreshing = False
            if self._refresh_requested and self.is_mounted and not self._busy:
                self._refresh_requested = False
                self._refresh_worker()

    def _render_rows(self, records: list[store.TaskRecord]) -> None:
        self._records = {record.id: record for record in records}
        table = self.query_one("#task-table", DataTable)
        self._rendering = True
        try:
            table.clear()
            for record in records:
                table.add_row(
                    record.id, record.agent, record.state, getattr(record, "execution_kind", "detached"),
                    getattr(record, "parent_task_id", "") or "—",
                    ", ".join(getattr(record, "dependencies", [])) or "—", key=record.id,
                )
            if self.selected_task_id not in self._records:
                self.selected_task_id = records[0].id if records else None
            if self.selected_task_id:
                table.move_cursor(row=list(self._records).index(self.selected_task_id))
            else:
                self.selected_record = None
                for widget_id in ("task-details", "task-logs", "task-diff", "task-evidence"):
                    self.query_one(f"#{widget_id}", TextArea).load_text("")
                self._set_actions()
        finally:
            self._rendering = False

    @on(DataTable.RowHighlighted, "#task-table")
    def _selected(self, event: DataTable.RowHighlighted) -> None:
        if self._rendering or not event.row_key.value:
            return
        task_id = str(event.row_key.value)
        if task_id == self.selected_task_id:
            return
        self.selected_task_id = task_id
        self.selected_record = None
        self._set_actions()
        self._details_worker(task_id)

    @work(group="task-board-details", exclusive=True, exit_on_error=False)
    async def _details_worker(self, task_id: str) -> None:
        await self._load_details(task_id)

    async def _load_details(self, task_id: str) -> None:
        self._detail_revision += 1
        revision = self._detail_revision
        try:
            details = await asyncio.to_thread(self.service.details, task_id)
            if (not self.is_mounted or self.selected_task_id != task_id
                    or revision != self._detail_revision):
                return
            self.selected_record = details.record
            self._render_details(details)
            self._set_actions()
            if not self._last_error and not self._busy:
                self._status(f"{len(self._records)} tasks · selected {task_id} · {details.record.state}")
        except Exception as exc:
            self._status(f"Could not load task {task_id}: {exc}", error=True)

    def _render_details(self, details: TaskBoardDetails) -> None:
        record = details.record
        result = details.result or {}
        if (record.id in self._reviewed
                and self._reviewed[record.id] != result.get("patch_sha256")):
            self._reviewed.pop(record.id, None)
        children = [r.id for r in self._records.values() if getattr(r, "parent_task_id", "") == record.id]
        summary = (
            f"Task: {record.id} · {record.agent} · {record.state}\n"
            f"Kind: {getattr(record, 'execution_kind', 'detached')} · Attempt: {getattr(record, 'attempt', 1)}\n"
            f"Parent: {getattr(record, 'parent_task_id', '') or 'none'} · Children: {', '.join(children) or 'none'}\n"
            f"Dependencies: {', '.join(getattr(record, 'dependencies', [])) or 'none'}\n"
            f"Integration: {getattr(record, 'integration_state', 'unreviewed')}\n"
            f"Thread: {record.thread_id or 'not started'}\n"
            f"Workspace: {record.manifest.get('execution_root') or record.manifest.get('shared_checkout') or record.manifest.get('original_cwd') or 'unknown'}\n"
            f"Context: {record.manifest.get('context_mode', 'isolated')}\n\n"
            f"Task prompt\n{record.prompt}\n\n"
        )
        if getattr(record, "execution_kind", "detached") == "inline":
            summary += (
                "Inline delegation shares its parent conversation's checkout. Stop parent stops the "
                "whole parent run and its children. Retry isolated starts a new detached task; "
                "Open parent restores the parent conversation.\n\n"
            )
        if record.pending_reason:
            summary += f"Pending\n{record.pending_reason}\n\n"
        if record.checkpoint.get("pending_id"):
            summary += (
                f"Request identity: {record.checkpoint['pending_id']}\n"
                "Review every action in this request before approving.\n"
                + json.dumps(record.checkpoint.get("interrupts", {}), indent=2, ensure_ascii=False, default=str)
                + "\n\n"
            )
            if _has_input_request(record):
                guide = {
                    identity: ("Your answer" if request.get("type") == "ask_user" or bool(request.get("action_requests")) and all(
                        action.get("name") == "manta_request_input"
                        for action in request.get("action_requests", [])
                    ) else {"decision": "approve"})
                    for identity, request in record.checkpoint.get("interrupts", {}).items()
                    if identity not in record.checkpoint.get("automatic_responses", {})
                }
                summary += (
                    "Use Respond to answer. For a mixed group, enter a JSON object keyed by every "
                    "interrupt ID, with text for input and approve/reject decisions for actions:\n"
                    + json.dumps(guide, indent=2, ensure_ascii=False) + "\n\n"
                )
            if record.checkpoint.get("automatic_responses"):
                summary += (
                    "Previously computed hook responses and skill directory denials are retained "
                    "for the exact request IDs below. They will be delivered with your decision "
                    "without rerunning those hooks or granting skill trust:\n"
                    + json.dumps(record.checkpoint["automatic_responses"], indent=2, ensure_ascii=False, default=str)
                    + "\n\n"
                )
        final_answer = result.get("final_answer") or record.result
        if final_answer:
            summary += f"Final answer\n{final_answer}\n"
        self.query_one("#task-details", TextArea).load_text(summary)
        self.query_one("#task-logs", TextArea).load_text(
            details.output + (f"\n\nEvents\n{details.events}" if details.events else "")
        )
        self.query_one("#task-diff", TextArea).load_text(str(result.get("diff", "")))
        evidence = {key: value for key, value in result.items() if key not in {"diff", "final_answer"}}
        self.query_one("#task-evidence", TextArea).load_text(
            json.dumps(evidence, indent=2, ensure_ascii=False, default=str)
            if evidence else "No structured result yet. Review a finished task to collect its change and test evidence."
        )

    def _set_actions(self) -> None:
        actions = self.service.available_actions(self.selected_record) if self.selected_record else set()
        for key in ("send", "answer", "cancel", "retry", "resume", "approve", "reject", "review", "integrate", "discard"):
            disabled = self._busy or key not in actions
            if key == "integrate" and not self._reviewed.get(self.selected_task_id or ""):
                disabled = True
            button = self.query_one(f"#task-{key}", Button)
            button.disabled = disabled
            if key in {"cancel", "retry", "resume"}:
                labels = {"cancel": "Stop parent", "retry": "Retry isolated", "resume": "Open parent"}
                button.label = labels[key] if self.selected_record and getattr(self.selected_record, "execution_kind", "detached") == "inline" else key.title()
        self.query_one("#task-submit", Button).disabled = self._busy
        self.query_one("#task-refresh", Button).disabled = self._busy

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("task-"):
            return
        event.stop()
        action = button_id.removeprefix("task-")
        if action == "refresh":
            self.action_refresh()
        elif action == "submit":
            self._submit()
        elif action in {"integrate", "discard"}:
            self._confirm(action)
        else:
            self._dispatch(action)

    def _submit(self) -> None:
        if self._busy:
            return
        agent = self.query_one("#task-agent", Select).value
        prompt = self.query_one("#task-prompt", TextArea).text.strip()
        if agent is Select.BLANK or not prompt:
            self._status("Select an enabled agent and enter a task prompt.", error=True)
            return
        context_mode = str(self.query_one("#task-context", Select).value)
        if context_mode == "fork" and not self.conversation_context:
            self._status("Fork requires a visible conversation snapshot.", error=True)
            return
        parent = self.query_one("#task-parent", Input).value.strip()
        dependencies = list(dict.fromkeys(
            item.strip() for item in self.query_one("#task-dependencies", Input).value.split(",") if item.strip()
        ))
        self._action_worker("submit", None, {
            "agent": str(agent), "prompt": prompt, "parent_task_id": parent,
            "dependencies": dependencies, "context_mode": context_mode,
            "conversation_context": self.conversation_context if context_mode == "fork" else "",
            "parent_thread_id": self.parent_thread_id,
        })

    def _confirm(self, action: str) -> None:
        record = self.selected_record
        if self._busy or record is None or action not in self.service.available_actions(record):
            return
        if action == "integrate" and not self._reviewed.get(record.id):
            return
        task_id = record.id
        reviewed_hash = self._reviewed.get(task_id)
        if action == "integrate":
            target = record.manifest.get("source_project_root") or record.manifest.get("project_root") or "the source project"
            message = f"Integrate the reviewed changes from {task_id} into {target}?\nConflicts or changed source files stop integration."
        else:
            message = f"Discard the isolated workspace for {task_id}?\nThe task record and result remain available."

        def confirmed(ok: bool | None) -> None:
            if ok:
                kwargs = {"expected_patch_sha256": reviewed_hash} if action == "integrate" else {}
                self._action_worker(action, task_id, kwargs)

        self.app.push_screen(_ConfirmTaskAction(message, action.title()), confirmed)

    def _dispatch(self, action: str) -> None:
        record = self.selected_record
        if self._busy or record is None or action not in self.service.available_actions(record):
            return
        kwargs: dict[str, Any] = {}
        if action in {"send", "answer"}:
            message = self.query_one("#task-message", Input).value.strip()
            if not message:
                self._status("Enter a steering message or an answer first.", error=True)
                return
            kwargs["message" if action == "send" else "value"] = message
        if action in {"approve", "reject", "answer"}:
            kwargs["pending_id"] = str(record.checkpoint.get("pending_id", ""))
            kwargs["decision"] = action
            if not kwargs["pending_id"]:
                self._status("This task has no current request identity. Refresh its details.", error=True)
                return
        self._action_worker(action, record.id, kwargs)

    @work(group="task-board-action", exclusive=False, exit_on_error=False)
    async def _action_worker(self, action: str, task_id: str | None, kwargs: dict) -> None:
        if self._busy:
            return
        self._busy = True
        self._revision += 1
        self._detail_revision += 1
        self._last_error = ""
        self._set_actions()
        self._status(f"{action.title()} in progress…")
        try:
            method = self.service.respond if action in {"approve", "reject", "answer"} else getattr(self.service, action)
            if action == "submit":
                result = await asyncio.to_thread(method, **kwargs)
            else:
                result = await asyncio.to_thread(method, task_id, **kwargs)
            if not self.is_mounted:
                return
            if action in {"submit", "retry"}:
                self.selected_task_id = result.id
                self.query_one("#task-board-tabs", TabbedContent).active = "team-tasks"
                # Disabling the submit button can move focus into its form.
                # Move focus with the tab so Textual does not reopen that pane.
                self.query_one("#task-table", DataTable).focus()
            if action == "submit":
                self.query_one("#task-prompt", TextArea).load_text("")
            if action == "review" and task_id:
                self._reviewed[task_id] = result.get("patch_sha256", "")
                self.query_one("#task-detail-tabs", TabbedContent).active = "task-diff-pane"
            elif task_id and action in {"retry", "resume", "approve", "reject", "answer"}:
                self._reviewed.pop(task_id, None)
            if action in {"send", "answer"}:
                self.query_one("#task-message", Input).value = ""
            if (action == "resume" and result is not None
                    and getattr(result, "execution_kind", "detached") == "inline"):
                self._status(f"Open parent requested · {task_id}. Complete any conversation confirmation.")
            else:
                self._status(f"{action.title()} completed{f' · {task_id}' if task_id else ''}.")
        except Exception as exc:
            self._status(f"{action.title()} failed: {exc}", error=True)
        finally:
            self._busy = False
            if self.is_mounted:
                self._set_actions()
                self._refresh_worker()

    def action_close(self) -> None:
        if self._busy:
            self._status("The task operation is still running. You can close this board when it finishes.")
            return
        self.dismiss(None)
