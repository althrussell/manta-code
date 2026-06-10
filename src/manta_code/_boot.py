"""Runtime entry that rebrands ``deepagents-code`` and scopes it to Databricks.

``deepagents-code`` exposes no public hook for its startup wordmark — the big
"DEEP AGENTS" ASCII art lives in module-level constants
(:data:`deepagents_code.config._UNICODE_BANNER` /
:data:`deepagents_code.config._ASCII_BANNER`) that :func:`get_banner` reads at
render time. To present Manta branding without forking the TUI, this shim
overrides those constants in-process before the Textual app paints, then
delegates to ``deepagents-code``'s real CLI entry point.

It also scopes the runtime to Databricks AI Gateway only. Upstream bundles the
``langchain-anthropic`` / ``langchain-openai`` / ``langchain-google-genai``
packages as hard dependencies, so its model discovery
(:func:`deepagents_code.model_config.get_available_models`) surfaces those
providers in both ``/model`` and ``/auth`` even though Manta never uses them.
There's no "only provider X" config switch (only per-provider ``enabled =
false``, which would mean enumerating a drift-prone provider list), so this
shim narrows discovery to the ``databricks`` provider declared in
``~/.deepagents/config.toml``. That single seam empties the ``/auth`` API-key
list (Databricks authenticates via your Databricks CLI profile, not an API
key) and reduces ``/model`` to the configured Databricks endpoints.

The launcher runs this via ``python -m manta_code._boot <passthrough args>``
(see :func:`manta_code.dcode.build_dcode_argv`). CLI args are forwarded
verbatim — ``cli_main`` does its own ``argparse`` on ``sys.argv``.

All patching is best-effort: any failure falls through to the unbranded,
unscoped upstream behaviour rather than blocking launch.
"""

from __future__ import annotations

import sys

#: Config-file provider key Manta wires up for Databricks AI Gateway.
DATABRICKS_PROVIDER = "databricks"

#: Manta-ray mark for Unicode terminals: cephalic horns up top, swept wings, a
#: layered-diamond body (a nod to the Databricks logo) tapering to a tail. Drawn
#: with single-width box-drawing glyphs so it stays aligned in any monospace
#: terminal. Rendered in the theme's primary colour (Databricks red).
MANTA_RAY_UNICODE = """\
╭╮  ╭╮
╰╮  ╭╯
╭───╯  ╰───╮
╭─╯    ◆◆    ╰─╮
╭─╯     ◆◆◆◆     ╰─╮
╰─╮      ◆◆      ╭─╯
╰──╮      ╭──╯
╰─╮  ╭─╯
╰──╯
││"""

#: Manta-ray mark for ASCII-only terminals (same silhouette, 7-bit glyphs).
MANTA_RAY_ASCII = r"""
      /\  /\
      \ \/ /
   __/    \__
 _/   <##>   \_
/    <####>    \
\     <##>     /
 \__        __/
    \__  __/
       \/
       ||"""

#: Manta wordmark for Unicode-capable terminals (ANSI Shadow style, matching
#: the upstream banner's visual weight).
MANTA_WORDMARK_UNICODE = """\
███╗   ███╗  █████╗  ███╗   ██╗ ████████╗  █████╗
████╗ ████║ ██╔══██╗ ████╗  ██║ ╚══██╔══╝ ██╔══██╗
██╔████╔██║ ███████║ ██╔██╗ ██║    ██║    ███████║
██║╚██╔╝██║ ██╔══██║ ██║╚██╗██║    ██║    ██╔══██║
██║ ╚═╝ ██║ ██║  ██║ ██║ ╚████║    ██║    ██║  ██║
╚═╝     ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝    ╚═╝    ╚═╝  ╚═╝"""

#: Manta wordmark for ASCII-only terminals.
MANTA_WORDMARK_ASCII = r"""
 __  __    _    _   _ _____  _
|  \/  |  / \  | \ | |_   _|/ \
| |\/| | / _ \ |  \| | | | / _ \
| |  | |/ ___ \| |\  | | |/ ___ \
|_|  |_/_/   \_\_| \_| |_/_/   \_\
"""


def _compose_banner(ray: str, wordmark: str) -> str:
    """Center the manta-ray mark over the wordmark and stack them.

    The wordmark is the widest element, so each non-blank ray line is left-padded
    to center it over the wordmark's width. A blank line separates the two. The
    result is wrapped in newlines so :func:`_versioned` can append the version
    tag beneath it exactly as upstream's banner constants are shaped.
    """
    word_lines = [ln for ln in wordmark.splitlines() if ln]
    width = max((len(ln) for ln in word_lines), default=0)
    centered = [
        "" if not ln.strip() else " " * max(0, (width - len(ln)) // 2) + ln
        for ln in ray.splitlines()
    ]
    return "\n" + "\n".join(centered) + "\n\n" + "\n".join(word_lines) + "\n"


#: Composed splash art: a Databricks-red manta gliding above the wordmark.
MANTA_UNICODE_BANNER = _compose_banner(MANTA_RAY_UNICODE, MANTA_WORDMARK_UNICODE)
MANTA_ASCII_BANNER = _compose_banner(MANTA_RAY_ASCII, MANTA_WORDMARK_ASCII)


def _versioned(art: str, version: str) -> str:
    """Append the runtime version tag so upstream's version logic still works.

    ``get_banner`` looks for the literal ``v{version}`` substring to apply the
    ``(local)`` editable-install suffix and the hide-version behaviour, so the
    tag must match the live ``deepagents-code`` version.
    """
    return f"{art.rstrip()}\n                                  v{version}\n"


def apply_branding() -> bool:
    """Override ``deepagents-code``'s banner constants with Manta art.

    Returns ``True`` when the override was applied, ``False`` if the upstream
    module layout changed and branding could not be applied (caller continues
    to launch regardless).
    """
    try:
        from deepagents_code import config
        from deepagents_code._version import __version__ as dcode_version
    except Exception:
        return False

    if not hasattr(config, "_UNICODE_BANNER") or not hasattr(config, "_ASCII_BANNER"):
        return False

    config._UNICODE_BANNER = _versioned(MANTA_UNICODE_BANNER, dcode_version)
    config._ASCII_BANNER = _versioned(MANTA_ASCII_BANNER, dcode_version)
    return True


def _databricks_only_models() -> dict[str, list[str]]:
    """Return only the Databricks provider's endpoints from the live config.

    Drop-in replacement for
    :func:`deepagents_code.model_config.get_available_models` that ignores the
    bundled LangChain providers and surfaces just the ``databricks`` endpoints
    declared in ``~/.deepagents/config.toml``. Returns an empty mapping when
    the provider or its model list is absent so the selector degrades to "no
    models" rather than crashing.
    """
    from deepagents_code.model_config import ModelConfig

    config = ModelConfig.load()
    provider_cfg = config.providers.get(DATABRICKS_PROVIDER, {})
    models = [str(model) for model in provider_cfg.get("models", [])]
    return {DATABRICKS_PROVIDER: models} if models else {}


def restrict_models_to_databricks() -> bool:
    """Scope model discovery to Databricks across ``/model`` and ``/auth``.

    Overrides ``get_available_models`` on the ``model_config`` module (the
    canonical definition) plus any widget modules that already imported the
    symbol by value, so a lazy ``from ... import get_available_models`` in the
    selector/auth screens binds to the Databricks-only variant.

    Returns ``True`` when the override was applied, ``False`` if the upstream
    module layout changed (caller continues to launch regardless).
    """
    try:
        from deepagents_code import model_config
    except Exception:
        return False

    if not hasattr(model_config, "get_available_models"):
        return False

    model_config.get_available_models = _databricks_only_models
    # `from ... import get_available_models` copies the reference, so re-point
    # any consumer modules already loaded at patch time.
    for module_name in (
        "deepagents_code.widgets.model_selector",
        "deepagents_code.widgets.auth",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "get_available_models"):
            module.get_available_models = _databricks_only_models
    return True


def switch_databricks_workspace(app: object, profile: str) -> None:
    """Switch the live session to Databricks ``profile`` and restart the server.

    Sets ``DATABRICKS_CONFIG_PROFILE`` in ``os.environ`` so the next server
    spawn authenticates against the new workspace (``deepagents-code`` snapshots
    ``os.environ`` in ``server._build_server_env`` at spawn time), then triggers
    a server respawn via the app's ``/restart`` handler. The conversation thread
    is preserved across the restart.

    Best-effort: if the running ``deepagents-code`` build exposes no
    ``_restart_server_manual`` hook, the env is still updated and the user is
    told to relaunch.
    """
    import asyncio
    import os

    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile

    restart = getattr(app, "_restart_server_manual", None)
    notify = getattr(app, "notify", None)
    if restart is None:
        if callable(notify):
            notify(
                f"Set profile to '{profile}', but this build can't restart the "
                "agent in place. Relaunch with: manta --profile " + profile,
                severity="warning",
                markup=False,
            )
        return

    if callable(notify):
        notify(
            f"Switching to workspace '{profile}' — restarting agent…",
            markup=False,
        )

    def _done(task: object) -> None:
        try:
            error = task.exception()  # type: ignore[attr-defined]
        except Exception:
            return
        if error is not None and callable(notify):
            notify(
                f"Workspace switch to '{profile}' failed: {error}",
                severity="error",
                markup=False,
            )

    task = asyncio.create_task(restart())
    task.add_done_callback(_done)


def rebrand_auth_screen() -> bool:
    """Recast ``/auth`` as a Databricks AI Gateway workspace switcher.

    Upstream's ``AuthManagerScreen`` is an API-key manager — title, provider
    option list, and "add/replace/delete" footer. Databricks AI Gateway
    authenticates through the Databricks CLI profile (no API key), so this
    replaces it with a profile picker: the option list shows the profiles in
    ``~/.databrickscfg`` (active one flagged), and selecting one prompts for
    confirmation, then switches the workspace and restarts the agent server
    against it.

    Profiles come from ``manta_code.auth`` (local file reads only — safe inside
    ``compose``; a live auth probe would block the UI thread). All overrides are
    monkeypatches on the screen class; nothing forks upstream source.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from textual.binding import Binding
        from textual.containers import Vertical
        from textual.content import Content
        from textual.screen import ModalScreen
        from textual.widgets import OptionList, Static
        from textual.widgets.option_list import Option

        from deepagents_code import theme
        from deepagents_code.config import get_glyphs, is_ascii_mode
        from deepagents_code.widgets.auth import AuthManagerScreen

        from manta_code import auth
    except Exception:
        return False

    class WorkspaceSwitchConfirmScreen(ModalScreen):
        """Confirmation overlay shown before restarting to switch workspace."""

        BINDINGS = [
            Binding("enter", "confirm", "Confirm", show=False, priority=True),
            Binding("escape", "cancel", "Cancel", show=False, priority=True),
        ]

        CSS = """
        WorkspaceSwitchConfirmScreen {
            align: center middle;
        }

        WorkspaceSwitchConfirmScreen > Vertical {
            width: 64;
            height: auto;
            background: $surface;
            border: solid $primary;
            padding: 1 2;
        }

        WorkspaceSwitchConfirmScreen .confirm-text {
            text-align: center;
            margin-bottom: 1;
        }

        WorkspaceSwitchConfirmScreen .confirm-help {
            text-align: center;
            color: $text-muted;
            text-style: italic;
        }
        """

        def __init__(self, profile: str) -> None:
            super().__init__()
            self._profile = profile

        def compose(self):  # noqa: ANN202 - Textual ComposeResult generator
            with Vertical():
                yield Static(
                    Content.assemble(
                        "Switch to workspace ",
                        Content.styled(self._profile, "bold $success"),
                        "?",
                    ),
                    classes="confirm-text",
                )
                yield Static(
                    "The agent server restarts; your chat history is preserved.",
                    classes="confirm-text",
                )
                yield Static("Enter confirm  •  Esc cancel", classes="confirm-help")

        def action_confirm(self) -> None:
            self.dismiss(True)

        def action_cancel(self) -> None:
            self.dismiss(False)

    def _compose(self: object):  # noqa: ANN202 - Textual ComposeResult generator
        glyphs = get_glyphs()
        profiles = auth.list_profiles()
        current = auth.resolve_profile() or "DEFAULT"
        with Vertical():
            yield Static("Databricks AI Gateway", classes="auth-manager-title")
            yield Static(
                "Manta authenticates through Databricks AI Gateway using your "
                "Databricks CLI profile. Select a profile to switch workspaces; "
                "model-provider API keys are not used.",
                classes="auth-manager-copy",
            )
            if profiles:
                options = []
                for info in profiles:
                    active = info.name == current
                    label = Content.assemble(
                        Content.styled(
                            info.name, "bold $success" if active else "bold"
                        ),
                        (f"  {info.host}", "$text-muted") if info.host else "",
                        ("  (active)", "$success") if active else "",
                    )
                    options.append(Option(label, id=info.name))
                yield OptionList(*options, id="auth-manager-options")
                yield Static(
                    f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate "
                    f"{glyphs.bullet} Enter switch workspace "
                    f"{glyphs.bullet} Esc close",
                    classes="auth-manager-help",
                )
            else:
                yield Static(
                    Content.assemble(
                        "No profiles found in ~/.databrickscfg. Add one with ",
                        Content.styled("databricks auth login", "bold"),
                        ".",
                    ),
                    classes="auth-manager-copy",
                )
                yield Static("Esc close", classes="auth-manager-help")

    def _on_mount(self: object) -> None:
        container = self.query_one(Vertical)
        # Fit the modal to its content rather than the upstream fixed 80%.
        container.styles.height = "auto"
        try:
            option_list = self.query_one("#auth-manager-options", OptionList)
            option_list.styles.height = "auto"
            option_list.styles.max_height = 12
        except Exception:
            pass
        if is_ascii_mode():
            colors = theme.get_theme_colors(self)
            container.styles.border = ("ascii", colors.success)

    def _on_option_selected(self: object, event: object) -> None:
        profile = event.option.id
        if not profile:
            return
        current = auth.resolve_profile() or "DEFAULT"
        if profile == current:
            self.app.notify(
                f"Already on workspace '{profile}'.",
                severity="information",
                markup=False,
            )
            return

        def _after_confirm(confirmed: object) -> None:
            if confirmed:
                switch_databricks_workspace(self.app, profile)
                self.dismiss(None)

        self.app.push_screen(WorkspaceSwitchConfirmScreen(profile), _after_confirm)

    AuthManagerScreen.compose = _compose
    AuthManagerScreen.on_mount = _on_mount
    AuthManagerScreen.on_option_list_option_selected = _on_option_selected
    return True


def rebrand_model_selector_footer() -> bool:
    """Show a neutral footer for profile-less Databricks endpoints in ``/model``.

    Databricks AI Gateway endpoints carry no upstream model profile (context
    window, modalities, capabilities), so the selector's detail footer renders
    "Model profile not available :(". Manta deliberately does not fabricate
    those values (a wrong ``max_input_tokens`` would drive real truncation), so
    instead this wraps ``_update_footer`` to print a neutral
    "Databricks AI Gateway endpoint" line for Databricks specs that lack a
    profile, delegating to the upstream footer for everything else (including
    Databricks endpoints the user has annotated with a profile in config).

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from textual.content import Content
        from textual.widgets import Static

        from deepagents_code.widgets.model_selector import ModelSelectorScreen
    except Exception:
        return False

    original_update_footer = ModelSelectorScreen._update_footer

    def _update_footer(self: object) -> None:
        if self._filtered_models:
            index = min(self._selected_index, len(self._filtered_models) - 1)
            spec, _ = self._filtered_models[index]
            entry = self._profiles.get(spec)
            profile = entry.get("profile") if entry else None
            if spec.startswith(f"{DATABRICKS_PROVIDER}:") and not profile:
                footer = self.query_one("#model-detail-footer", Static)
                footer.update(
                    Content.styled("Databricks AI Gateway endpoint\n\n\n", "dim")
                )
                return
        original_update_footer(self)

    ModelSelectorScreen._update_footer = _update_footer
    return True


def allow_blocking_server() -> bool:
    """Let the LangGraph dev server tolerate Databricks' blocking auth.

    ``deepagents-code`` runs the agent in a ``langgraph dev`` subprocess whose
    in-memory runtime arms ``blockbuster`` to raise on any synchronous I/O on
    the event loop. The Databricks SDK's ``databricks-cli`` auth is genuinely
    blocking — it resolves the ``databricks`` binary via ``os.readlink`` and
    shells out for an OAuth token — and exposes no async driver, so the first
    ``ChatDatabricks`` request inside the server trips ``blockbuster`` with
    ``ValueError: ... Blocking call to os.readlink``.

    ``langgraph dev`` only disables that detector when invoked with
    ``--allow-blocking`` (it sets ``LANGGRAPH_ALLOW_BLOCKING`` itself, so an
    inherited env var is overwritten — the flag is the sole seam). This wraps
    :func:`deepagents_code.server._build_server_cmd` to append the flag. The
    detector is a deployment-hygiene aid for multi-tenant ASGI servers;
    disabling it is the upstream-recommended remedy (option 3) and benign for
    this single-user local dev server.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from deepagents_code import server
    except Exception:
        return False

    original_build_cmd = getattr(server, "_build_server_cmd", None)
    if original_build_cmd is None:
        return False

    def _build_server_cmd(*args: object, **kwargs: object) -> list[str]:
        cmd = original_build_cmd(*args, **kwargs)
        if "--allow-blocking" not in cmd:
            cmd.append("--allow-blocking")
        return cmd

    server._build_server_cmd = _build_server_cmd
    return True


def install_manta_build_hook() -> bool:
    """Install Manta's control-plane build hook (best-effort).

    Wraps ``deepagents_code.agent.create_deep_agent`` so Manta's compiled agents,
    middleware, store, and Databricks tools are injected (ADR 0008). This covers
    the in-process ``create_cli_agent`` path; the server-subprocess path is
    covered by the same call in :mod:`manta_code.databricks_chat`. Never raises.
    """
    try:
        from manta_code.hook import install_build_hook

        return install_build_hook()
    except Exception:  # noqa: BLE001 - reliability: launch regardless
        return False


def main() -> None:
    """Apply branding and Databricks scoping, then run the upstream CLI."""
    apply_branding()
    restrict_models_to_databricks()
    rebrand_auth_screen()
    rebrand_model_selector_footer()
    allow_blocking_server()
    install_manta_build_hook()
    from deepagents_code.main import cli_main

    cli_main()


if __name__ == "__main__":
    main()
