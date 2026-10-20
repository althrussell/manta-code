"""Mounted end-to-end interactions for Manta's visible task control plane."""

from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, TextArea

from manta_code.tui.tasks import TaskBoardDetails, TaskBoardScreen, TaskBoardService


def task(task_id="task-one", state="running", **fields):
    values = dict(
        id=task_id, agent="swe", prompt="Fix the parser [bold]literally[/bold]",
        state=state, parent_task_id="", dependencies=[], attempt=1,
        integration_state="unreviewed", execution_kind="detached", result="",
        structured_result={}, thread_id="thread-one", checkpoint={}, pending_reason="",
        manifest={"execution_root": "/tmp/task-worktree", "source_project_root": "/tmp/source", "context_mode": "isolated", "isolation": "git_worktree"},
    )
    values.update(fields)
    return SimpleNamespace(**values)


class FakeService(TaskBoardService):
    def __init__(self, records=()):
        self.records = {record.id: record for record in records}
        self.calls = []
        self.threads = []
        self.fail = {}
        self.hold = None

    def _call(self, method, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.calls.append((method, args, kwargs))
        if method in self.fail:
            raise RuntimeError(self.fail[method])
        if self.hold and self.hold[0] == method:
            self.hold[1].set()
            self.hold[2].wait(5)

    def agents(self):
        self._call("agents")
        return [("swe", "Coding specialist"), ("review", "Read-only reviewer")]

    def list_tasks(self):
        self._call("list_tasks")
        return copy.deepcopy(list(self.records.values()))

    def details(self, task_id):
        self._call("details", task_id)
        record = copy.deepcopy(self.records[task_id])
        return TaskBoardDetails(record, "log [red]literal[/red]", "tool_start: read_file", record.structured_result)

    def submit(self, agent, prompt, **kwargs):
        self._call("submit", agent, prompt, **kwargs)
        record = task("task-new", "queued", agent=agent, prompt=prompt, **{
            key: value for key, value in kwargs.items() if key in {"parent_task_id", "dependencies"}
        })
        record.manifest.update(context_mode=kwargs["context_mode"])
        self.records[record.id] = record
        return copy.deepcopy(record)

    def send(self, task_id, message):
        self._call("send", task_id, message)

    def cancel(self, task_id):
        self._call("cancel", task_id)
        self.records[task_id].state = "cancelled"
        return copy.deepcopy(self.records[task_id])

    def retry(self, task_id):
        self._call("retry", task_id)
        record = self.records[task_id]
        record.state = "queued"
        record.attempt += 1
        return copy.deepcopy(record)

    def resume(self, task_id):
        self._call("resume", task_id)
        self.records[task_id].state = "queued"
        return copy.deepcopy(self.records[task_id])

    def respond(self, task_id, *, pending_id, decision, value=""):
        self._call("respond", task_id, pending_id=pending_id, decision=decision, value=value)
        record = self.records[task_id]
        if pending_id != record.checkpoint.get("pending_id"):
            raise RuntimeError("The pending request changed. Refresh before deciding.")
        record.state = "cancelled" if decision == "reject" else "queued"
        record.checkpoint = {}
        return copy.deepcopy(record)

    def review(self, task_id):
        self._call("review", task_id)
        result = {
            "status": "done", "final_answer": "Parser fixed", "changed_files": ["parser.py"],
            "diff": "diff --git a/parser.py b/parser.py\n+return 42\n",
            "artifacts": ["report.txt"], "tests": [{"command": "pytest", "exit_code": 0}],
            "spend": {"total_tokens": 234, "cost_usd": None, "complete": False},
            "patch_sha256": "reviewed-patch",
        }
        self.records[task_id].structured_result = result
        return copy.deepcopy(result)

    def integrate(self, task_id, *, expected_patch_sha256=None):
        self._call("integrate", task_id, expected_patch_sha256=expected_patch_sha256)
        if expected_patch_sha256 != self.records[task_id].structured_result.get("patch_sha256"):
            raise RuntimeError("The reviewed changes changed; review them again")
        self.records[task_id].integration_state = "integrated"
        return copy.deepcopy(self.records[task_id])

    def discard(self, task_id):
        self._call("discard", task_id)
        self.records[task_id].integration_state = "discarded"
        return copy.deepcopy(self.records[task_id])


class Harness(App):
    pass


async def settled(pilot, predicate, *, message="task board did not settle"):
    for _ in range(100):
        if predicate():
            return
        await pilot.pause(0.01)
    assert predicate(), message


async def mounted(service, scenario, **kwargs):
    app = Harness()
    async with app.run_test(size=(130, 50)) as pilot:
        screen = TaskBoardScreen(service=service, refresh_interval=0, **kwargs)
        await app.push_screen(screen)
        await settled(pilot, lambda: bool(screen._agent_options))
        if service.records:
            await settled(pilot, lambda: screen.selected_record is not None)
        await scenario(app, pilot, screen)


def test_board_shows_parent_children_dependencies_and_literal_evidence():
    service = FakeService([
        task("parent", "done"),
        task("child", parent_task_id="parent", dependencies=["dependency"]),
        task("dependency", "done", execution_kind="inline"),
    ])

    async def scenario(app, pilot, screen):
        assert "Manta" in str(screen.query_one("#task-board-title", Static).content)
        details = screen.query_one("#task-details", TextArea).text
        assert "Children: child" in details
        assert "[bold]literally[/bold]" in details
        assert "[red]literal[/red]" in screen.query_one("#task-logs", TextArea).text
        table = screen.query_one("#task-table", DataTable)
        table.move_cursor(row=1)
        await settled(pilot, lambda: screen.selected_record and screen.selected_record.id == "child")
        details = screen.query_one("#task-details", TextArea).text
        assert "Parent: parent" in details and "Dependencies: dependency" in details
        # Refresh preserves an explicit selection instead of jumping to the first row.
        screen.action_refresh()
        await pilot.pause()
        assert screen.selected_task_id == "child"
        table.move_cursor(row=2)
        await settled(pilot, lambda: screen.selected_record and screen.selected_record.id == "dependency")
        assert screen.query_one("#task-cancel", Button).disabled
        assert screen.query_one("#task-integrate", Button).disabled
        assert not screen.query_one("#task-review", Button).disabled
        await pilot.press("escape")
        assert len(app.screen_stack) == 1

    asyncio.run(mounted(service, scenario))


def test_assign_task_validation_fork_snapshot_dependencies_and_new_selection():
    service = FakeService([task("parent", "done")])

    async def scenario(app, pilot, screen):
        screen.query_one("#task-board-tabs", TabbedContent).active = "team-assign"
        await pilot.pause()
        await pilot.click("#task-submit")
        assert "Select an enabled agent" in str(screen.query_one("#task-board-status", Static).content)
        screen.query_one("#task-agent", Select).value = "review"
        screen.query_one("#task-prompt", TextArea).load_text("Review the parser; run focused tests")
        screen.query_one("#task-parent", Input).value = "parent"
        screen.query_one("#task-dependencies", Input).value = "parent, dep-two, parent"
        screen.query_one("#task-context", Select).value = "fork"
        screen.action_refresh()
        await pilot.pause()
        assert screen.query_one("#task-prompt", TextArea).text.startswith("Review the parser")
        await pilot.pause(0.25)  # Textual's button activation animation ignores a second immediate click.
        await pilot.click("#task-submit")
        await settled(pilot, lambda: screen.selected_task_id == "task-new" and not screen._busy)
        await settled(pilot, lambda: screen.selected_record and screen.selected_record.id == "task-new")
        submitted = [call for call in service.calls if call[0] == "submit"]
        assert submitted == [("submit", ("review", "Review the parser; run focused tests"), {
            "parent_task_id": "parent", "dependencies": ["parent", "dep-two"],
            "context_mode": "fork", "conversation_context": "Visible conversation", "parent_thread_id": "parent-thread",
        })]
        assert screen.query_one("#task-board-tabs", TabbedContent).active == "team-tasks"
        assert screen.query_one("#task-prompt", TextArea).text == ""
        # Every service operation, including listing/detail reads, ran off the app thread.
        assert service.threads and threading.get_ident() not in service.threads

    asyncio.run(mounted(service, scenario, conversation_context="Visible conversation", parent_thread_id="parent-thread"))


def test_no_snapshot_exposes_only_isolated_context():
    service = FakeService()

    async def scenario(app, pilot, screen):
        screen.query_one("#task-board-tabs", TabbedContent).active = "team-assign"
        await pilot.pause()
        select = screen.query_one("#task-context", Select)
        assert select.value == "isolated"
        with pytest.raises(Exception):
            select.value = "fork"
        assert "No conversation snapshot" in str(screen.query_one("#task-assign-note", Static).content)

    asyncio.run(mounted(service, scenario))


def test_agent_workspace_assign_prefill_opens_the_assignment_form_without_dispatch():
    service = FakeService([task()])

    async def scenario(app, pilot, screen):
        assert screen.query_one("#task-board-tabs", TabbedContent).active == "team-assign"
        assert screen.query_one("#task-agent", Select).value == "review"
        assert screen.query_one("#task-prompt", TextArea).text == "Inspect the repository"
        assert not any(call[0] == "submit" for call in service.calls)

    asyncio.run(mounted(service, scenario, initial_agent="review", initial_prompt="Inspect the repository"))


def test_steer_cancel_and_retry_are_real_actions_and_keep_task_identity():
    service = FakeService([task()])

    async def scenario(app, pilot, screen):
        await pilot.click("#task-send")
        assert "Enter a steering message" in str(screen.query_one("#task-board-status", Static).content)
        screen.query_one("#task-message", Input).value = "Keep the public API"
        await pilot.pause(0.25)
        await pilot.click("#task-send")
        await settled(pilot, lambda: any(call[0] == "send" for call in service.calls) and not screen._busy)
        assert ("send", ("task-one", "Keep the public API"), {}) in service.calls
        assert not screen.query_one("#task-message", Input).value
        await pilot.click("#task-cancel")
        await settled(pilot, lambda: screen.selected_record.state == "cancelled" and not screen._busy)
        await pilot.click("#task-retry")
        await settled(pilot, lambda: screen.selected_task_id == "task-one" and not screen._busy)
        await settled(pilot, lambda: screen.selected_record and screen.selected_record.attempt == 2)
        assert "Attempt: 2" in screen.query_one("#task-details", TextArea).text

    asyncio.run(mounted(service, scenario))


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_pending_approval_requires_exact_displayed_identity(decision):
    service = FakeService([task(
        state="waiting_approval", pending_reason="write_file requires permission",
        checkpoint={"pending_id": "pending-one", "interrupts": {
            "interrupt-1": {"action_requests": [{"name": "write_file", "args": {"path": "a.py", "content": "[bold]literal[/bold]"}}]},
            "interrupt-2": {"action_requests": [{"name": "delete_file", "args": {"path": "old.py"}}]},
        }},
    )])

    async def scenario(app, pilot, screen):
        details = screen.query_one("#task-details", TextArea).text
        assert "pending-one" in details and "delete_file" in details and "[bold]literal[/bold]" in details
        # A new pending group arrives after the displayed snapshot; the old decision must fail.
        service.records["task-one"].checkpoint = {"pending_id": "pending-two", "interrupts": {"interrupt-3": {"new": "request"}}}
        await pilot.click(f"#task-{decision}")
        await settled(pilot, lambda: bool(screen._last_error) and not screen._busy)
        assert "pending request changed" in screen._last_error
        assert service.records["task-one"].state == "waiting_approval"
        assert ("respond", ("task-one",), {"pending_id": "pending-one", "decision": decision, "value": ""}) in service.calls
        await settled(pilot, lambda: screen.selected_record.checkpoint.get("pending_id") == "pending-two")
        await pilot.pause(0.25)
        await pilot.click(f"#task-{decision}")
        await settled(pilot, lambda: service.records["task-one"].state != "waiting_approval" and not screen._busy)
        assert not screen._last_error
        assert service.records["task-one"].state == ("cancelled" if decision == "reject" else "queued")

    asyncio.run(mounted(service, scenario))


def test_waiting_input_answer_and_interrupted_resume():
    service = FakeService([task(
        state="waiting_input", pending_reason="Which API should remain compatible?",
        checkpoint={"pending_id": "input-one", "interrupts": {"question-1": {"question": "Which API?"}}},
    )])

    async def scenario(app, pilot, screen):
        assert screen.query_one("#task-approve", Button).disabled
        screen.query_one("#task-message", Input).value = "Preserve v1"
        await pilot.click("#task-answer")
        await settled(pilot, lambda: service.records["task-one"].state == "queued" and not screen._busy)
        assert ("respond", ("task-one",), {"pending_id": "input-one", "decision": "answer", "value": "Preserve v1"}) in service.calls
        service.records["task-one"].state = "interrupted"
        service.records["task-one"].checkpoint = {"resume_value": {"saved": {"decisions": [{"type": "approve"}]}}}
        screen.action_refresh()
        await settled(pilot, lambda: screen.selected_record.state == "interrupted")
        await pilot.click("#task-resume")
        await settled(pilot, lambda: any(call[0] == "resume" for call in service.calls) and not screen._busy)

    asyncio.run(mounted(service, scenario))


def test_mixed_group_exposes_response_with_explicit_per_interrupt_answer_and_decision():
    service = FakeService([task(
        state="waiting_approval", checkpoint={"pending_id": "mixed-one", "interrupts": {
            "input-id": {"action_requests": [{"name": "manta_request_input", "args": {"question": "Which API?"}}]},
            "edit-id": {"action_requests": [{"name": "write_file", "args": {"path": "a.py"}}]},
        }},
    )])

    async def scenario(app, pilot, screen):
        assert screen.query_one("#task-approve", Button).disabled
        assert not screen.query_one("#task-answer", Button).disabled
        assert "JSON object keyed by every interrupt ID" in screen.query_one("#task-details", TextArea).text
        value = '{"input-id":"Keep v1","edit-id":{"decision":"approve"}}'
        screen.query_one("#task-message", Input).value = value
        await pilot.click("#task-answer")
        await settled(pilot, lambda: service.records["task-one"].state == "queued" and not screen._busy)
        assert ("respond", ("task-one",), {"pending_id": "mixed-one", "decision": "answer", "value": value}) in service.calls

    asyncio.run(mounted(service, scenario))


def test_inline_parent_resume_reports_request_before_native_confirmation():
    service = FakeService([task(state="interrupted", execution_kind="inline")])
    service.available_actions = lambda record: {"resume"}

    async def scenario(app, pilot, screen):
        messages = []
        original_status = screen._status

        def capture_status(value, **kwargs):
            messages.append(value)
            original_status(value, **kwargs)

        screen._status = capture_status
        await pilot.click("#task-resume")
        await settled(pilot, lambda: any(call[0] == "resume" for call in service.calls) and not screen._busy)
        status = "\n".join(messages)
        assert "Open parent requested" in status
        assert "confirmation" in status
        assert "Resume completed" not in status
    asyncio.run(mounted(service, scenario))


def test_review_evidence_then_explicit_integration_and_discard_confirmation():
    service = FakeService([task(state="done")])

    async def scenario(app, pilot, screen):
        assert screen.query_one("#task-integrate", Button).disabled
        await pilot.click("#task-review")
        await settled(pilot, lambda: "task-one" in screen._reviewed and not screen._busy)
        await settled(pilot, lambda: "return 42" in screen.query_one("#task-diff", TextArea).text)
        evidence = screen.query_one("#task-evidence", TextArea).text
        assert '"exit_code": 0' in evidence and '"cost_usd": null' in evidence
        assert '"complete": false' in evidence and "report.txt" in evidence
        await pilot.click("#task-integrate")
        await pilot.pause()
        assert len(app.screen_stack) == 3
        assert not any(call[0] == "integrate" for call in service.calls)
        await pilot.click("#confirm-back")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.click("#task-integrate")
        await pilot.click("#confirm-action")
        await settled(pilot, lambda: service.records["task-one"].integration_state == "integrated" and not screen._busy)
        await settled(pilot, lambda: screen.query_one("#task-integrate", Button).disabled)
        # Another finished task can be discarded with the same explicit confirmation.
        service.records["task-two"] = task("task-two", "done")
        screen.action_refresh()
        await settled(pilot, lambda: len(screen._records) == 2)
        screen.query_one("#task-table", DataTable).move_cursor(row=1)
        await settled(pilot, lambda: screen.selected_record and screen.selected_record.id == "task-two")
        await pilot.click("#task-discard")
        await pilot.click("#confirm-action")
        await settled(pilot, lambda: service.records["task-two"].integration_state == "discarded" and not screen._busy)
        assert "task-two" in service.records

    asyncio.run(mounted(service, scenario))


def test_background_action_keeps_ui_responsive_and_errors_visible():
    started, release = threading.Event(), threading.Event()
    service = FakeService([task()])
    service.hold = ("send", started, release)

    async def scenario(app, pilot, screen):
        screen.query_one("#task-message", Input).value = "Wait for the test"
        await pilot.click("#task-send")
        await settled(pilot, started.is_set)
        assert screen._busy and screen.query_one("#task-cancel", Button).disabled
        # A live UI timer continues to tick during the held synchronous operation.
        ticks = []
        screen.set_timer(0.01, lambda: ticks.append(True))
        await settled(pilot, lambda: bool(ticks))
        await pilot.press("escape")
        assert len(app.screen_stack) == 2
        release.set()
        await settled(pilot, lambda: not screen._busy)
        service.fail["cancel"] = "process changed while cancelling"
        await pilot.click("#task-cancel")
        await settled(pilot, lambda: "process changed" in screen._last_error)
        assert service.records["task-one"].state == "running"
        assert "failed" in str(screen.query_one("#task-board-status", Static).content)

    try:
        asyncio.run(mounted(service, scenario))
    finally:
        release.set()


def test_integration_confirmation_is_bound_to_the_diff_the_user_reviewed():
    service = FakeService([task(state="done")])

    async def scenario(app, pilot, screen):
        await pilot.click("#task-review")
        await settled(pilot, lambda: bool(screen._reviewed.get("task-one")) and not screen._busy)
        await pilot.click("#task-integrate")
        await pilot.pause()
        # Another session reviews a different patch while confirmation is open.
        service.records["task-one"].structured_result["patch_sha256"] = "different-patch"
        await pilot.click("#confirm-action")
        await settled(pilot, lambda: "reviewed changes changed" in screen._last_error and not screen._busy)
        assert service.records["task-one"].integration_state == "unreviewed"
        assert ("integrate", ("task-one",), {"expected_patch_sha256": "reviewed-patch"}) in service.calls
        await settled(pilot, lambda: screen.query_one("#task-integrate", Button).disabled)

    asyncio.run(mounted(service, scenario))


def test_refresh_failure_is_visible_and_recoverable():
    service = FakeService([task()])

    async def scenario(app, pilot, screen):
        service.fail["list_tasks"] = "database temporarily unavailable"
        screen.action_refresh()
        await settled(pilot, lambda: "database temporarily" in screen._last_error)
        del service.fail["list_tasks"]
        screen.action_refresh()
        await settled(pilot, lambda: not screen._last_error)
        assert screen.selected_task_id == "task-one"

    asyncio.run(mounted(service, scenario))


def test_slow_poll_does_not_cancel_and_multiply_filesystem_workers():
    service = FakeService([task()])
    started, release = threading.Event(), threading.Event()

    async def scenario(app, pilot, screen):
        await settled(pilot, lambda: not screen._refreshing)
        prior = len([call for call in service.calls if call[0] == "list_tasks"])
        service.hold = ("list_tasks", started, release)
        screen.action_refresh()
        await settled(pilot, started.is_set)
        for _ in range(5):
            screen._poll()
            screen.action_refresh()
            await pilot.pause(0.01)
        assert len([call for call in service.calls if call[0] == "list_tasks"]) == prior + 1
        service.hold = None
        release.set()
        await settled(pilot, lambda: not screen._refreshing and not screen._refresh_requested)
        assert len([call for call in service.calls if call[0] == "list_tasks"]) == prior + 2

    try:
        asyncio.run(mounted(service, scenario))
    finally:
        release.set()


def test_mounted_default_service_assigns_isolated_git_work_and_integrates_observed_result(tmp_path, monkeypatch):
    """Exercise actual SQLite/Git/service operations with a deterministic runner."""
    from manta_code.tasks import executor, store

    project = tmp_path / "repository"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "manta"))
    config = tmp_path / "databricks.ini"
    config.write_text("[DEFAULT]\nhost=https://workspace.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    for name in (
        "MANTA_TASK_CONFIG_ROOT", "MANTA_TASK_DB_PATH", "MANTA_TASK_ID", "MANTA_TASK_DEPTH",
        "MANTA_PROJECT_ROOT", "MANTA_PROFILE", "DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE",
        "DEEPAGENTS_CODE_SERVER_CWD", "DEEPAGENTS_CODE_SERVER_PROJECT_ROOT", "MANTA_ORIGINAL_CWD",
    ):
        monkeypatch.delenv(name, raising=False)

    def git(*args):
        return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "user.name", "Manta Test")
    git("config", "user.email", "test@example.com")
    (project / "answer.py").write_text("answer = 41\n")
    (project / "other.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    (project / "other.txt").write_text("caller staged\n")
    git("add", "other.txt")
    (project / "other.txt").write_text("caller staged and unstaged\n")
    (project / "private.txt").write_text("caller untracked\n")
    original_index = git("diff", "--cached")

    def fixture_runner(record, **kwargs):
        workspace = Path(record.manifest["execution_root"])
        assert workspace != project
        assert (workspace / "other.txt").read_text() == "original\n"
        (workspace / "answer.py").write_text("answer = 42\n")
        completed = subprocess.run(
            [sys.executable, "-c", "from answer import answer; assert answer == 42"],
            cwd=workspace, capture_output=True, text=True, check=False,
        )
        store.record_test_result(record.id, "python -c 'from answer import answer; assert answer == 42'", completed.returncode, completed.stdout + completed.stderr)
        assert completed.returncode == 0
        store.update_task(record.id, state="done", pid=None, result="Corrected the answer and checked it", expect_state="starting")
        return store.get_task(record.id)

    monkeypatch.setattr(executor, "_spawn_task", fixture_runner)
    monkeypatch.setattr("manta_code.tasks.scheduler.ensure_scheduler", lambda **kwargs: None)
    service = TaskBoardService()

    async def run():
        app = Harness()
        async with app.run_test(size=(120, 45)) as pilot:
            screen = TaskBoardScreen(service=service, refresh_interval=0)
            await app.push_screen(screen)
            await settled(pilot, lambda: bool(screen._agent_options))
            screen.query_one("#task-board-tabs", TabbedContent).active = "team-assign"
            screen.query_one("#task-agent", Select).value = "swe"
            screen.query_one("#task-prompt", TextArea).load_text("Correct answer.py and run the check")
            await pilot.pause()
            await pilot.click("#task-submit")
            await settled(pilot, lambda: screen.selected_record and screen.selected_record.state == "done")
            assert (project / "answer.py").read_text() == "answer = 41\n"
            assert screen.query_one("#task-integrate", Button).disabled
            await pilot.click("#task-review")
            await settled(pilot, lambda: "answer = 42" in screen.query_one("#task-diff", TextArea).text)
            evidence = screen.query_one("#task-evidence", TextArea).text
            assert '"exit_code": 0' in evidence and '"cost_complete": false' in evidence
            assert '"source_dirty": true' in evidence
            await pilot.click("#task-integrate")
            await pilot.click("#confirm-action")
            await settled(pilot, lambda: store.get_task(screen.selected_task_id).integration_state == "integrated")
            assert (project / "answer.py").read_text() == "answer = 42\n"
            assert (project / "other.txt").read_text() == "caller staged and unstaged\n"
            assert (project / "private.txt").read_text() == "caller untracked\n"
            assert git("diff", "--cached") == original_index

    asyncio.run(run())


def test_compact_terminal_keeps_task_actions_reachable():
    service = FakeService([task()])

    async def run():
        app = Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = TaskBoardScreen(service=service, refresh_interval=0)
            await app.push_screen(screen)
            await settled(pilot, lambda: screen.selected_record is not None)
            assert screen.has_class("compact")
            cancel = screen.query_one("#task-cancel", Button)
            assert cancel.region.bottom <= app.size.height
            await pilot.click("#task-cancel")
            await settled(pilot, lambda: service.records["task-one"].state == "cancelled")

    asyncio.run(run())
