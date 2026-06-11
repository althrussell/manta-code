"""Runtime entry that rebrands ``deepagents-code``, Databricks-first.

``deepagents-code`` exposes no public hook for its startup wordmark — the big
"DEEP AGENTS" ASCII art lives in module-level constants
(:data:`deepagents_code.config._UNICODE_BANNER` /
:data:`deepagents_code.config._ASCII_BANNER`) that :func:`get_banner` reads at
render time. To present Manta branding without forking the TUI, this shim
overrides those constants in-process before the Textual app paints, then
delegates to ``deepagents-code``'s real CLI entry point.

It also makes model discovery **Databricks-first, not Databricks-only**
(ADR 0010). Upstream's :func:`deepagents_code.model_config.get_available_models`
surfaces every installed provider (anthropic, openai, google, …). Manta keeps
all of them — "Databricks is never the place you're trapped" (VISION.md) — and
wraps discovery only to list the ``databricks`` provider first, since it is
the default and the launch model. The ``/auth`` screen likewise leads with the
Databricks workspace (profile) picker and keeps upstream's provider API-key
manager below it, so off-Databricks use is configured in the same place.

The launcher runs this via ``python -m manta_code._boot <passthrough args>``
(see :func:`manta_code.dcode.build_dcode_argv`). CLI args are forwarded
verbatim — ``cli_main`` does its own ``argparse`` on ``sys.argv``.

All patching is best-effort: any failure falls through to the unbranded
upstream behaviour rather than blocking launch. Substantive degradation (the
control plane failing to install) is announced on stderr at startup — falling
back is non-negotiable, falling back *silently* is not (ADR 0010).
"""

from __future__ import annotations

import os
import sys

#: Config-file provider key Manta wires up for Databricks AI Gateway.
DATABRICKS_PROVIDER = "databricks"

#: Manta-ray mark for Unicode terminals: the compact half-block manta —
#: cephalic horns, swept wings, a tail — three lines tall. Rendered in the
#: theme's primary colour (Databricks red).
MANTA_RAY_UNICODE = """\
▗▌  ▄▄▄  ▐▖
▝▜██▀▀▀██▛▘
  ▝▄ ▀ ▄▘"""

#: Manta-ray mark for ASCII-only terminals (same silhouette, 7-bit glyphs).
MANTA_RAY_ASCII = r"""
.|  ___  |.
<##=======##>
   '. - .'"""

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

    The ray is centered as a **block**: one shared left pad computed from its
    widest line, so the art's internal alignment (including intentional
    leading spaces, e.g. the tail row) is preserved exactly as authored.
    Per-line centering would re-center each row and skew the shape. A blank
    line separates mark and wordmark; the result is wrapped in newlines so
    :func:`_versioned` can append the version tag beneath it exactly as
    upstream's banner constants are shaped.
    """
    word_lines = [ln for ln in wordmark.splitlines() if ln]
    width = max((len(ln) for ln in word_lines), default=0)
    ray_lines = ray.splitlines()
    ray_width = max((len(ln) for ln in ray_lines if ln.strip()), default=0)
    pad = " " * max(0, (width - ray_width) // 2)
    centered = ["" if not ln.strip() else pad + ln for ln in ray_lines]
    return "\n" + "\n".join(centered) + "\n\n" + "\n".join(word_lines) + "\n"


#: Composed splash art: a Databricks-red manta gliding above the wordmark.
MANTA_UNICODE_BANNER = _compose_banner(MANTA_RAY_UNICODE, MANTA_WORDMARK_UNICODE)
MANTA_ASCII_BANNER = _compose_banner(MANTA_RAY_ASCII, MANTA_WORDMARK_ASCII)


def _versioned(art: str, version: str) -> str:
    """Append Manta's version tag beneath the banner.

    The tag is **Manta's** version (the product the user installed), not the
    upstream runtime's — showing ``deepagents-code``'s version here confused
    users into thinking Manta was a different release. Upstream's
    ``get_banner`` logic that rewrites ``v{upstream_version}`` (the
    ``(local)`` suffix / hide-version behaviour) simply no-ops on this tag,
    which is harmless.
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

        from manta_code import __version__ as manta_version
    except Exception:
        return False

    if not hasattr(config, "_UNICODE_BANNER") or not hasattr(config, "_ASCII_BANNER"):
        return False

    config._UNICODE_BANNER = _versioned(MANTA_UNICODE_BANNER, manta_version)
    config._ASCII_BANNER = _versioned(MANTA_ASCII_BANNER, manta_version)
    return True


#: Original upstream ``get_available_models``, captured when the
#: Databricks-first wrapper installs (so the wrapper can delegate to it).
_original_get_available_models = None


def _databricks_first_models() -> dict[str, list[str]]:
    """Upstream model discovery, reordered so Databricks lists first.

    Wraps (not replaces) upstream's
    :func:`deepagents_code.model_config.get_available_models`: every installed
    provider stays available — Databricks-first, not Databricks-only
    (ADR 0010) — but the ``databricks`` provider is moved to the front of the
    mapping so ``/model`` and ``/auth`` lead with the default. Degrades to an
    empty mapping only if upstream discovery itself fails.
    """
    original = _original_get_available_models
    if original is None:
        return {}
    try:
        available = dict(original())
    except Exception:  # noqa: BLE001 - discovery must never crash the TUI
        return {}
    if DATABRICKS_PROVIDER not in available:
        return available
    ordered = {DATABRICKS_PROVIDER: available[DATABRICKS_PROVIDER]}
    for provider, models in available.items():
        if provider != DATABRICKS_PROVIDER:
            ordered[provider] = models
    return ordered


def prefer_databricks_models() -> bool:
    """Reorder model discovery so Databricks leads across ``/model`` and ``/auth``.

    Wraps ``get_available_models`` on the ``model_config`` module (the
    canonical definition) plus any widget modules that already imported the
    symbol by value, so a lazy ``from ... import get_available_models`` in the
    selector/auth screens binds to the Databricks-first variant. Idempotent.

    Returns ``True`` when the override was applied, ``False`` if the upstream
    module layout changed (caller continues to launch regardless).
    """
    global _original_get_available_models
    try:
        from deepagents_code import model_config
    except Exception:
        return False

    current = getattr(model_config, "get_available_models", None)
    if current is None:
        return False
    if current is not _databricks_first_models:
        _original_get_available_models = current
    model_config.get_available_models = _databricks_first_models
    # `from ... import get_available_models` copies the reference, so re-point
    # any consumer modules already loaded at patch time.
    for module_name in (
        "deepagents_code.widgets.model_selector",
        "deepagents_code.widgets.auth",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "get_available_models"):
            module.get_available_models = _databricks_first_models
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
    """Lead ``/auth`` with a Databricks workspace picker, keep provider keys.

    Upstream's ``AuthManagerScreen`` is an API-key manager — title, provider
    option list, and "add/replace/delete" footer. Databricks authenticates
    through the Databricks CLI profile (no API key), so Manta prepends a
    workspace section: the profiles in ``~/.databrickscfg`` (active one
    flagged), where selecting one prompts for confirmation, then switches the
    workspace and restarts the agent server against it.

    Upstream's provider API-key manager stays, below the workspace section —
    Databricks-first, not Databricks-only (ADR 0010): Anthropic/OpenAI/Google
    keys are added or replaced exactly as in vanilla ``deepagents-code``, on
    the same screen. The upstream helpers that render and refresh the provider
    list (``_build_options_with_warning``, ``_refresh_options``,
    ``_format_label``) are reused untouched.

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
        from deepagents_code.model_config import get_credential_env_var
        from deepagents_code.widgets.auth import AuthManagerScreen, AuthPromptScreen

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

    def _workspace_options() -> list:
        """Option list entries for the configured Databricks profiles."""
        current = auth.resolve_profile() or "DEFAULT"
        options = []
        for info in auth.list_profiles():
            active = info.name == current
            label = Content.assemble(
                Content.styled(info.name, "bold $success" if active else "bold"),
                (f"  {info.host}", "$text-muted") if info.host else "",
                ("  (active)", "$success") if active else "",
            )
            options.append(Option(label, id=info.name))
        return options

    def _compose(self: object):  # noqa: ANN202 - Textual ComposeResult generator
        glyphs = get_glyphs()
        workspace_options = _workspace_options()
        provider_options, store_warning = self._build_options_with_warning()
        with Vertical():
            yield Static("Authentication", classes="auth-manager-title")

            # Databricks-first: the workspace (profile) picker leads.
            yield Static(
                Content.styled("Databricks workspace", "bold"),
                classes="auth-manager-copy",
            )
            if workspace_options:
                yield OptionList(*workspace_options, id="manta-workspace-options")
            else:
                yield Static(
                    Content.assemble(
                        "No profiles in ~/.databrickscfg (optional). Add one with ",
                        Content.styled("databricks auth login", "bold"),
                        ".",
                    ),
                    classes="auth-manager-copy",
                )

            # Upstream's provider API-key manager, kept intact below.
            yield Static(
                Content.styled("Model provider API keys", "bold"),
                classes="auth-manager-copy",
            )
            yield Static(self._build_description(), classes="auth-manager-copy")
            if store_warning:
                yield Static(
                    Content.from_markup("$msg", msg=store_warning),
                    classes="auth-manager-warning",
                )
            yield OptionList(*provider_options, id="auth-manager-options")

            yield Static(
                f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate "
                f"{glyphs.bullet} Enter switch workspace / manage key "
                f"{glyphs.bullet} Esc close",
                classes="auth-manager-help",
            )

    def _on_mount(self: object) -> None:
        container = self.query_one(Vertical)
        try:
            workspace_list = self.query_one("#manta-workspace-options", OptionList)
            # The workspace list is short; size to content so the provider
            # list below keeps most of the modal (its upstream `1fr` CSS).
            workspace_list.styles.height = "auto"
            workspace_list.styles.max_height = 8
        except Exception:
            pass
        if is_ascii_mode():
            colors = theme.get_theme_colors(self)
            container.styles.border = ("ascii", colors.success)

    def _focus_other_list(self: object) -> None:
        """Move keyboard focus between the workspace and provider lists.

        Upstream binds Tab/Shift+Tab (priority) to ``action_cursor_down/up``,
        which hardcode the provider list — with two lists that moved the
        *provider* highlight while focus (and Enter) stayed on the workspace
        list, so a user tabbing toward an API key could trigger a workspace
        switch instead. With two lists, Tab means "switch section"; arrow
        keys move within the focused list.
        """
        lists = list(self.query(OptionList))
        if not lists:
            return
        if len(lists) == 1:
            lists[0].focus()
            return
        focused = self.app.focused
        try:
            index = lists.index(focused)
        except ValueError:
            index = -1
        lists[(index + 1) % len(lists)].focus()

    def _action_cursor_down(self: object) -> None:
        _focus_other_list(self)

    def _action_cursor_up(self: object) -> None:
        _focus_other_list(self)

    def _on_option_selected(self: object, event: object) -> None:
        selected = event.option.id
        if not selected:
            return
        option_list_id = getattr(getattr(event, "option_list", None), "id", None)

        if option_list_id == "manta-workspace-options":
            current = auth.resolve_profile() or "DEFAULT"
            if selected == current:
                self.app.notify(
                    f"Already on workspace '{selected}'.",
                    severity="information",
                    markup=False,
                )
                return

            def _after_confirm(confirmed: object) -> None:
                if confirmed:
                    switch_databricks_workspace(self.app, selected)
                    self.dismiss(None)

            self.app.push_screen(
                WorkspaceSwitchConfirmScreen(selected), _after_confirm
            )
            return

        # Provider key entry: upstream behaviour, verbatim.
        env_var = get_credential_env_var(selected)
        self.app.push_screen(
            AuthPromptScreen(selected, env_var),
            self._on_prompt_closed,
        )

    AuthManagerScreen.compose = _compose
    AuthManagerScreen.on_mount = _on_mount
    AuthManagerScreen.on_option_list_option_selected = _on_option_selected
    AuthManagerScreen.action_cursor_down = _action_cursor_down
    AuthManagerScreen.action_cursor_up = _action_cursor_up
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


def align_agent_switch_model() -> bool:
    """Make the ``/agents`` picker keep the conversation and apply the pin.

    Upstream's agent swap (``DeepAgentsApp._restart_server_for_agent_swap``)
    restarts the server with the new identity, starts a **new thread**, and
    leaves the *session model* (and therefore the footer) on whatever the
    session launched with. This wraps the swap to follow up with upstream's
    own primitives:

    - **Conversation continuity**: the previous thread is auto-resumed via
      ``_resume_thread`` (the same machinery as the ``/threads`` picker and
      launch-time ``-r``), so switching agents continues your session instead
      of wiping the chat — the new agent picks up where the old one left off.
      ``/clear`` starts fresh when that's what you want. Only threads that
      actually produced agent output are resumed (a brand-new empty thread has
      nothing to restore — mirrors upstream's own resume-hint gating).
      Disable with ``MANTA_SWAP_RESUME=0``.
    - **Model-pin alignment**: the selected Manta agent's pin (or, for
      unpinned agents, the configured cheap default) becomes the session model
      via ``_switch_model`` (thread-preserving; ``persist=False`` so a profile
      switch never redefines the user's saved default model). Without this,
      the footer lied about the model the agent runs on.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from deepagents_code.app import DeepAgentsApp
    except Exception:
        return False

    original_swap = getattr(DeepAgentsApp, "_restart_server_for_agent_swap", None)
    if original_swap is None or getattr(original_swap, "__manta_pin_align__", False):
        return original_swap is not None

    def _manta_pin(agent_name: str) -> str | None:
        try:
            from manta_code.agents.defaults import merged_agents
            from manta_code.agents.registry import list_agents

            for defn in merged_agents(list_agents()):
                if defn.name == agent_name:
                    return defn.model or None
        except Exception:  # noqa: BLE001 - pin lookup is best-effort
            pass
        return None

    def _fallback_spec() -> str | None:
        """Session model for an *unpinned* swap target (base agent included).

        Without this, switching from a pinned specialist to an unpinned agent
        would ratchet: the previous agent's (possibly premium) pin would stay
        the session model. Fall back to Manta's configured cheap default when
        Databricks is configured; otherwise leave the model alone.
        """
        try:
            from manta_code.auth import databricks_configured
            from manta_code.config import load_config

            if not databricks_configured():
                return None
            endpoint = load_config().interactive.default_endpoint
            return f"{DATABRICKS_PROVIDER}:{endpoint}" if endpoint else None
        except Exception:  # noqa: BLE001
            return None

    def _swap_resume_enabled() -> bool:
        value = os.getenv("MANTA_SWAP_RESUME")
        if value is None:
            return True
        return value.strip().lower() not in {"0", "false", "no", "off"}

    def _resumable_thread(self: object) -> str | None:
        """The pre-swap thread id, if it holds real agent output.

        Must be read *before* the swap (the swap clears the message store).
        Mirrors upstream's resume-hint gating: USER-only threads have no
        checkpoint row, so resuming them would fail.
        """
        try:
            thread_id = getattr(self, "_lc_thread_id", None)
            if not thread_id:
                return None
            from deepagents_code.widgets.message_store import MessageType

            signal_types = {
                MessageType.ASSISTANT,
                MessageType.TOOL,
                MessageType.SKILL,
            }
            messages = self._message_store.get_all_messages()
            if any(msg.type in signal_types for msg in messages):
                return str(thread_id)
        except Exception:  # noqa: BLE001 - continuity is best-effort
            pass
        return None

    async def wrapped(self: object, agent_name: str) -> None:
        previous_thread = (
            _resumable_thread(self) if _swap_resume_enabled() else None
        )
        await original_swap(self, agent_name)
        try:
            if getattr(self, "_assistant_id", None) != agent_name:
                return  # swap failed and rolled back; leave everything alone
            if previous_thread:
                await self._resume_thread(previous_thread)
                notify = getattr(self, "notify", None)
                if callable(notify):
                    notify(
                        "Continued your previous session with the new agent — "
                        "/clear starts a fresh thread.",
                        timeout=6,
                        markup=False,
                    )
            spec = _manta_pin(agent_name) or _fallback_spec()
            if spec:
                await self._switch_model(
                    spec, persist=False, announce_unchanged=False
                )
        except Exception:  # noqa: BLE001 - alignment must never break the swap
            pass

    wrapped.__manta_pin_align__ = True  # type: ignore[attr-defined]
    DeepAgentsApp._restart_server_for_agent_swap = wrapped
    return True


def add_agent_mentions_to_autocomplete() -> bool:
    """Teach the ``@`` autocomplete about Manta agents at message start.

    Upstream's ``@`` completion is file mentions only, so typing ``@swe …``
    (Manta's agent addressing, VISION pillar 4) fights a file picker and gets
    no completion. This wraps ``FuzzyFileController`` so that when the ``@``
    opens the message — the only position where agent addressing fires —
    matching agent names are suggested first (tagged ``agent``), with file
    suggestions following. Mid-message ``@`` stays pure file mention.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from deepagents_code.widgets.autocomplete import FuzzyFileController
    except Exception:
        return False

    original_suggest = getattr(FuzzyFileController, "_get_fuzzy_suggestions", None)
    original_changed = getattr(FuzzyFileController, "on_text_changed", None)
    if original_suggest is None or original_changed is None:
        return False
    if getattr(original_suggest, "__manta_agents__", False):
        return True

    def _agent_names() -> list[str]:
        try:
            from manta_code.agents.defaults import merged_agents
            from manta_code.agents.registry import list_agents

            return [a.name for a in merged_agents(list_agents())]
        except Exception:  # noqa: BLE001 - registry trouble just loses hints
            return []

    def on_text_changed(self: object, text: str, cursor_index: int) -> None:
        try:
            before = text[:cursor_index]
            at_index = before.rfind("@")
            # Agent addressing only applies when @ starts the message.
            self._manta_addressing = at_index >= 0 and not before[:at_index].strip()
        except Exception:  # noqa: BLE001
            self._manta_addressing = False
        original_changed(self, text, cursor_index)

    def _get_fuzzy_suggestions(self: object, search: str) -> list[tuple[str, str]]:
        suggestions = original_suggest(self, search)
        if not getattr(self, "_manta_addressing", False):
            return suggestions
        try:
            query = search.lower()
            agents = [
                (f"@{name}", "agent")
                for name in _agent_names()
                if name.startswith(query)
            ]
            if agents:
                return [*agents, *suggestions][:10]
        except Exception:  # noqa: BLE001 - hints must never break completion
            pass
        return suggestions

    _get_fuzzy_suggestions.__manta_agents__ = True  # type: ignore[attr-defined]
    FuzzyFileController.on_text_changed = on_text_changed
    FuzzyFileController._get_fuzzy_suggestions = _get_fuzzy_suggestions
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
    """Apply branding and Databricks-first scoping, then run the upstream CLI.

    Cosmetic patches (banner, model-selector footer) degrade silently;
    substantive ones announce themselves on stderr so a fallback to vanilla is
    never invisible (ADR 0010): the user learns *at launch* that agents,
    budgets, or the Databricks-first surfaces are inactive, instead of
    wondering later where they went.
    """
    apply_branding()
    rebrand_model_selector_footer()

    degraded: list[str] = []
    if not prefer_databricks_models():
        degraded.append("Databricks-first model list")
    if not rebrand_auth_screen():
        degraded.append("workspace picker in /auth")
    if not align_agent_switch_model():
        degraded.append("agent-pin model alignment in /agents")
    if not add_agent_mentions_to_autocomplete():
        degraded.append("@agent autocomplete")
    if not allow_blocking_server():
        degraded.append("Databricks auth shim")
    if not install_manta_build_hook():
        degraded.append("control plane (agents, budgets, Databricks tools)")
    if degraded:
        print(
            "⚠ Manta degraded — inactive: "
            + "; ".join(degraded)
            + ". Running closer to vanilla deepagents-code; run `manta doctor`.",
            file=sys.stderr,
        )

    from deepagents_code.main import cli_main

    cli_main()


if __name__ == "__main__":
    main()
