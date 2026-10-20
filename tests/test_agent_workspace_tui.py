from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static, TextArea

from manta_code.agents.registry import AgentDef
from manta_code.agents.workspace import AgentWorkspace
from manta_code.tui.agents import AgentEditorScreen, AgentWorkspaceResult, AgentWorkspaceScreen


def _app(workspace, *, editable=True):
    class WorkspaceApp(App):
        result = None

        def on_mount(self):
            self.push_screen(AgentWorkspaceScreen("review", workspace=workspace, editable=editable,
                                                 inherited_model="databricks:system.ai.gpt-6-1-sol"),
                             lambda result: setattr(self, "result", result))

    return WorkspaceApp()


def test_mounted_workspace_create_edit_clone_disable_validate_and_apply(tmp_path):
    async def exercise():
        workspace = AgentWorkspace(root=tmp_path)
        app = _app(workspace)
        async with app.run_test(size=(125, 58)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AgentWorkspaceScreen)
            details = screen.query_one("#agent-details", Static).render().plain
            assert '"name": "review"' in details
            assert '"policy"' in details and '"memory"' in details and '"budget"' in details
            await pilot.click("#workspace-create")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, AgentEditorScreen)
            editor.query_one("#agent-name", Input).value = "specialist"
            editor.query_one("#agent-description", Input).value = "Reviews Python"
            editor.query_one("#agent-model", Input).value = "databricks:system.ai.gpt-6-1-sol"
            editor.query_one("#agent-effort", Input).value = "low"
            editor.query_one("#agent-output", Input).value = "2000"
            editor.query_one("#agent-read-only", Checkbox).value = True
            editor.query_one("#agent-prompt", TextArea).load_text("Review tests before reporting.")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is screen
            assert screen.changed is True
            assert "reload pending" in screen.query_one("#workspace-status", Static).render().plain.lower()
            assert workspace.resolve("specialist").reasoning_effort == "low"
            await pilot.click("#workspace-edit")
            await pilot.pause()
            app.screen.query_one("#agent-context", Select).value = "fork"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert workspace.resolve("specialist").context_mode == "fork"
            await pilot.click("#workspace-clone")
            await pilot.pause()
            assert app.screen.query_one("#agent-name", Input).value == "specialist-copy"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert workspace.resolve("specialist-copy").memory_namespace is None
            await pilot.click("#workspace-toggle")
            await pilot.pause()
            assert not workspace.resolve("specialist-copy", include_disabled=True).enabled
            assert screen.query_one("#workspace-assign", Button).disabled
            await pilot.click("#workspace-toggle")
            await pilot.click("#workspace-validate")
            await pilot.pause()
            assert "Offline compilation passed" in screen.query_one("#workspace-status", Static).render().plain
            await pilot.click("#workspace-reload")
            await pilot.pause()
            assert app.result == AgentWorkspaceResult("reload", "specialist-copy", True, workspace.revision())

    asyncio.run(exercise())


def test_editor_rejects_invalid_advanced_settings_without_saving(tmp_path):
    async def exercise():
        workspace = AgentWorkspace(root=tmp_path)
        app = _app(workspace)
        async with app.run_test(size=(125, 58)) as pilot:
            await pilot.click("#workspace-create")
            await pilot.pause()
            editor = app.screen
            editor.query_one("#agent-advanced", TextArea).load_text(json.dumps({"budget_max_tokens": -1}))
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is editor
            assert "greater than 0" in editor.query_one("#editor-error", Static).render().plain
            assert "new-specialist" not in {value.name for value in workspace.list()}
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AgentWorkspaceScreen)
            assert not app.screen.changed

    asyncio.run(exercise())


def test_busy_workspace_allows_inspection_only(tmp_path):
    async def exercise():
        workspace = AgentWorkspace(root=tmp_path)
        app = _app(workspace, editable=False)
        async with app.run_test(size=(125, 58)) as pilot:
            await pilot.pause()
            screen = app.screen
            for identity in ("create", "clone", "edit", "toggle", "switch", "assign", "test", "reload"):
                assert screen.query_one(f"#workspace-{identity}", Button).disabled
            assert not screen.query_one("#workspace-validate", Button).disabled
            screen._result("assign")
            assert "busy" in screen.query_one("#workspace-status", Static).render().plain
            await pilot.press("escape")
            await pilot.pause()
            assert app.result is None
            assert len(workspace.list()) == 4

    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["switch", "test", "assign"])
def test_workspace_dispatch_actions_report_exact_definition_revision(tmp_path, action):
    async def exercise():
        workspace = AgentWorkspace(root=tmp_path)
        app = _app(workspace)
        async with app.run_test(size=(125, 58)) as pilot:
            await pilot.click(f"#workspace-{action}")
            await pilot.pause()
            assert app.result == AgentWorkspaceResult(action, "review", False, workspace.revision())

    asyncio.run(exercise())


def test_saved_definition_does_not_claim_live_until_runtime_reload(tmp_path):
    async def exercise():
        workspace = AgentWorkspace(root=tmp_path)
        workspace.create(AgentDef(name="helper"))
        app = _app(workspace)
        async with app.run_test(size=(125, 58)) as pilot:
            screen = app.screen
            options = screen.query_one("#agent-options", OptionList)
            names = [value.name for value in workspace.list(include_disabled=True)]
            options.highlighted = names.index("helper")
            await pilot.pause()
            await pilot.click("#workspace-edit")
            await pilot.pause()
            app.screen.query_one("#agent-description", Input).value = "saved change"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert workspace.resolve("helper").description == "saved change"
            assert "reload pending" in screen.query_one("#workspace-status", Static).render().plain.lower()
            assert app.result is None
            await pilot.press("escape")
            await pilot.pause()
            assert app.result.action == "reload"
            assert app.result.changed is True

    asyncio.run(exercise())
