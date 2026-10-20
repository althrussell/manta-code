"""Agent authoring and effective-definition inspection inside Manta."""
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from ..agents.registry import AgentDef, definition_revision
from ..agents.workspace import AgentWorkspace


@dataclass(frozen=True)
class AgentWorkspaceResult:
    action: Literal["switch", "reload", "test", "assign"]
    agent: str | None = None
    changed: bool = False
    revision: str | None = None


class AgentEditorScreen(ModalScreen[AgentDef | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save")]
    CSS = """
    AgentEditorScreen { align: center middle; }
    AgentEditorScreen > Vertical { width: 94; max-width: 96%; height: 94%; background: $surface;
        border: solid $primary; padding: 1 2; }
    AgentEditorScreen VerticalScroll { height: 1fr; }
    AgentEditorScreen Input, AgentEditorScreen Select { margin-bottom: 1; }
    AgentEditorScreen TextArea { height: 10; margin-bottom: 1; }
    AgentEditorScreen #agent-prompt { height: 12; }
    AgentEditorScreen Horizontal { height: 3; }
    AgentEditorScreen #editor-error { height: auto; color: $error; }
    """

    def __init__(self, definition: AgentDef, *, title: str, name_editable: bool = False,
                 validator: Callable[[AgentDef], list[str]] | None = None) -> None:
        super().__init__()
        self.definition = definition
        self.title_text = title
        self.name_editable = name_editable
        self.validator = validator

    def compose(self) -> ComposeResult:
        definition = self.definition
        advanced = definition.model_dump(mode="json", exclude={
            "name", "description", "model", "system_prompt", "read_only", "memory",
            "context_mode", "reasoning_effort", "max_output_tokens", "enabled",
        })
        with Vertical():
            yield Label(self.title_text)
            with VerticalScroll():
                yield Label("Name")
                yield Input(definition.name, id="agent-name", disabled=not self.name_editable)
                yield Label("Description")
                yield Input(definition.description, id="agent-description")
                yield Label("Gateway model reference (blank inherits the session model)")
                yield Input(definition.model or "", placeholder="databricks:system.ai.gpt-6-1-sol", id="agent-model")
                yield Label("Inline context: isolated task prompt or forked parent conversation")
                yield Select([("Isolated", "isolated"), ("Fork parent conversation", "fork")],
                             value=definition.context_mode, allow_blank=False, id="agent-context")
                yield Label("Reasoning effort (blank inherits model default)")
                yield Input(definition.reasoning_effort or "", id="agent-effort")
                yield Label("Per-response output token limit (blank inherits; distinct from task budget)")
                yield Input(str(definition.max_output_tokens or ""), id="agent-output")
                yield Checkbox("Read-only tool policy", value=definition.read_only, id="agent-read-only")
                yield Checkbox("Project-scoped durable memory", value=definition.memory, id="agent-memory")
                yield Label("Role instructions — learned context is preserved separately")
                yield TextArea(definition.system_prompt, id="agent-prompt")
                yield Label("Tools, shell, filesystem rules, approvals, skills, memory namespace and task budgets (JSON)")
                yield TextArea(json.dumps(advanced, indent=2), id="agent-advanced", language="json")
            yield Static("", id="editor-error", markup=False)
            with Horizontal():
                yield Button("Save definition", id="editor-save", variant="primary")
                yield Button("Cancel", id="editor-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        try:
            advanced = json.loads(self.query_one("#agent-advanced", TextArea).text)
            if not isinstance(advanced, dict):
                raise ValueError("advanced settings must be a JSON object")
            controlled = {"name", "description", "model", "system_prompt", "read_only", "memory",
                          "context_mode", "reasoning_effort", "max_output_tokens", "enabled"}
            if controlled & advanced.keys():
                raise ValueError("use the form fields for " + ", ".join(sorted(controlled & advanced.keys())))
            output = self.query_one("#agent-output", Input).value.strip()
            definition = AgentDef.model_validate({
                **advanced, "name": self.query_one("#agent-name", Input).value.strip(),
                "description": self.query_one("#agent-description", Input).value.strip(),
                "model": self.query_one("#agent-model", Input).value.strip() or None,
                "context_mode": self.query_one("#agent-context", Select).value,
                "reasoning_effort": self.query_one("#agent-effort", Input).value.strip() or None,
                "max_output_tokens": int(output) if output else None,
                "system_prompt": self.query_one("#agent-prompt", TextArea).text,
                "read_only": self.query_one("#agent-read-only", Checkbox).value,
                "memory": self.query_one("#agent-memory", Checkbox).value,
                "enabled": self.definition.enabled,
            })
            if self.validator is not None:
                self.validator(definition)
        except (ValueError, TypeError) as exc:
            self.query_one("#editor-error", Static).update(str(exc))
            return
        self.dismiss(definition)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "editor-save":
            self.action_save()
        else:
            self.action_cancel()


class AgentWorkspaceScreen(ModalScreen[AgentWorkspaceResult | None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    CSS = """
    AgentWorkspaceScreen { align: center middle; }
    AgentWorkspaceScreen > Vertical { width: 116; max-width: 98%; height: 94%;
        background: $surface; border: solid $primary; padding: 1 2; }
    AgentWorkspaceScreen #agent-workspace-body { height: 1fr; }
    AgentWorkspaceScreen #agent-options { width: 30; height: 1fr; }
    AgentWorkspaceScreen #agent-details-scroll { width: 1fr; height: 1fr; padding-left: 2; }
    AgentWorkspaceScreen #agent-details { height: auto; }
    AgentWorkspaceScreen .agent-actions { height: 3; }
    AgentWorkspaceScreen Button { min-width: 10; margin-right: 1; }
    AgentWorkspaceScreen #workspace-status { height: auto; color: $warning; margin: 1 0; }
    """

    def __init__(self, current_agent: str | None = None, *, workspace: AgentWorkspace | None = None,
                 inherited_model: str | None = None, inherited_tools: list[str] | None = None,
                 editable: bool = True) -> None:
        super().__init__()
        self.workspace = workspace or AgentWorkspace()
        self.current_agent = current_agent
        self.inherited_model = inherited_model
        self.inherited_tools = inherited_tools
        self.editable = editable
        self.changed = False
        self.selected_agent = current_agent

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Manta agent team")
            yield Static("Select a specialist, inspect its controls, or create one. Edits apply after a safe runtime reload.", markup=False)
            with Horizontal(id="agent-workspace-body"):
                yield OptionList(id="agent-options")
                with VerticalScroll(id="agent-details-scroll"):
                    yield Static("", id="agent-details", markup=False)
            yield Static("", id="workspace-status", markup=False)
            with HorizontalScroll(classes="agent-actions"):
                for label, identity in (("Create", "create"), ("Clone", "clone"), ("Edit", "edit"),
                                        ("Disable", "toggle"), ("Validate", "validate")):
                    yield Button(label, id=f"workspace-{identity}", disabled=not self.editable and identity != "validate")
            with HorizontalScroll(classes="agent-actions"):
                for label, identity in (("Select", "switch"), ("Base session", "base"), ("Assign task", "assign"), ("Test agent", "test"),
                                        ("Apply reload", "reload"), ("Close", "close")):
                    yield Button(label, id=f"workspace-{identity}", disabled=not self.editable and identity != "close")

    def on_mount(self) -> None:
        self.refresh_agents()
        if not self.editable:
            self.status("Inspection only while the runtime is busy or awaiting a decision. Reopen /agents when it is idle.")

    def status(self, message: str) -> None:
        self.query_one("#workspace-status", Static).update(message)

    def refresh_agents(self, selected: str | None = None) -> None:
        definitions = self.workspace.list(include_disabled=True)
        names = [definition.name for definition in definitions]
        chosen = selected or self.selected_agent
        self.selected_agent = chosen if chosen in names else names[0] if names else None
        options = self.query_one("#agent-options", OptionList)
        options.clear_options()
        for definition in definitions:
            suffix = " (disabled)" if not definition.enabled else " (current)" if definition.name == self.current_agent else ""
            options.add_option(Option(definition.name + suffix, id=definition.name))
        if self.selected_agent in names:
            options.highlighted = names.index(self.selected_agent)
        self.show_details()

    def show_details(self) -> None:
        if self.selected_agent is None:
            return
        try:
            effective = self.workspace.effective(self.selected_agent, inherited_model=self.inherited_model,
                                                  inherited_tools=self.inherited_tools)
            self.query_one("#agent-details", Static).update(json.dumps(effective, indent=2))
            self.query_one("#workspace-toggle", Button).label = "Disable" if effective["enabled"] else "Enable"
            for identity in ("switch", "assign", "test"):
                self.query_one(f"#workspace-{identity}", Button).disabled = not self.editable or not effective["enabled"]
        except (ValueError, OSError) as exc:
            self.status(str(exc))

    @on(OptionList.OptionHighlighted)
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.selected_agent = event.option.id
        self.show_details()

    def _saved(self, definition: AgentDef | None, *, original: str | None, revision: str | None) -> None:
        if definition is None:
            return
        try:
            if original is None:
                self.workspace.create(definition)
            else:
                self.workspace.update(original, definition.model_dump(), expected_revision=revision)
        except (ValueError, OSError, RuntimeError) as exc:
            self.status(f"Definition was not saved: {exc}")
            return
        self.changed = True
        self.refresh_agents(definition.name)
        self.status("Definition saved. Runtime reload pending; select Apply reload or close to apply safely.")

    def _result(self, action: Literal["switch", "reload", "test", "assign"]) -> None:
        if not self.editable:
            self.status("Runtime is busy; reopen /agents after the current work or approval finishes.")
            return
        if action != "reload" and self.selected_agent is not None:
            self.workspace.resolve(self.selected_agent)
        self.dismiss(AgentWorkspaceResult(action, self.selected_agent, self.changed, self.workspace.revision()))

    def action_close(self) -> None:
        if self.changed:
            self._result("reload")
        else:
            self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        action = (event.button.id or "").removeprefix("workspace-")
        if action == "close":
            self.action_close()
            return
        if action == "validate":
            if self.selected_agent:
                try:
                    result = self.workspace.test(self.selected_agent)
                    self.status("Offline compilation passed. Inference access remains unverified. " + " ".join(result["warnings"]))
                except (ValueError, OSError, RuntimeError) as exc:
                    self.status(f"Validation failed: {exc}")
            return
        if not self.editable:
            self.status("Runtime is busy; definitions cannot be changed until it is idle.")
            return
        if action == "create":
            self.app.push_screen(AgentEditorScreen(AgentDef(name="new-specialist"), title="Create specialist", name_editable=True, validator=self.workspace.validate),
                                 lambda definition: self._saved(definition, original=None, revision=None))
            return
        if action == "base":
            self.dismiss(AgentWorkspaceResult("switch", "agent", self.changed, self.workspace.revision()))
            return
        if self.selected_agent is None:
            return
        try:
            definition = self.workspace.resolve(self.selected_agent, include_disabled=True)
            if action == "edit":
                self.app.push_screen(AgentEditorScreen(definition, title=f"Edit {definition.name}", validator=self.workspace.validate),
                                     lambda value: self._saved(value, original=definition.name, revision=definition_revision(definition)))
            elif action == "clone":
                clone = AgentDef.model_validate({**definition.model_dump(), "name": f"{definition.name}-copy", "enabled": True, "memory_namespace": None})
                self.app.push_screen(AgentEditorScreen(clone, title=f"Clone {definition.name}", name_editable=True, validator=self.workspace.validate),
                                     lambda value: self._saved(value, original=None, revision=None))
            elif action == "toggle":
                self.workspace.set_enabled(definition.name, not definition.enabled)
                self.changed = True
                self.refresh_agents(definition.name)
                self.status("Enabled state saved. Runtime reload pending; apply reload to update dispatch.")
            elif action in {"switch", "reload", "test", "assign"}:
                self._result(action)  # type: ignore[arg-type]
        except (ValueError, OSError, RuntimeError) as exc:
            self.status(str(exc))
