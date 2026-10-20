"""Runtime entry that brands ``deepagents-code`` and scopes its model picker.

The current TUI renders a compact welcome widget independently of the legacy
banner constants. Manta patches both rendering paths before the Textual app
paints, then delegates to the upstream CLI entry point.

``/model`` lists the active Databricks workspace's full chat-model inventory,
including Unity Gateway services, without unrelated direct-key providers or a
curated-only default. Both local and inference-host catalogs pass through this
scope. Explicit CLI provider selections and stored provider configuration are
preserved. ``/auth`` leads with the Databricks workspace picker.

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
    """Brand both the current welcome widget and the legacy splash banner.

    Returns ``True`` when the override was applied, ``False`` if the upstream
    module layout changed and branding could not be applied (caller continues
    to launch regardless).
    """
    try:
        from deepagents_code import config

        from manta_code import __version__ as manta_version
        from manta_code.branding import apply_tui_branding
    except Exception:
        return False

    if not hasattr(config, "_UNICODE_BANNER") or not hasattr(config, "_ASCII_BANNER"):
        return False

    config._UNICODE_BANNER = _versioned(MANTA_UNICODE_BANNER, manta_version)
    config._ASCII_BANNER = _versioned(MANTA_ASCII_BANNER, manta_version)
    try:
        return apply_tui_branding(
            unicode_banner=MANTA_UNICODE_BANNER,
            ascii_banner=MANTA_ASCII_BANNER,
            unicode_ray=MANTA_RAY_UNICODE,
            ascii_ray=MANTA_RAY_ASCII,
            version=manta_version,
        )
    except Exception:  # noqa: BLE001 - announce degradation without blocking launch
        return False


#: Original upstream ``get_available_models``, captured when the
#: Databricks-first wrapper installs (so the wrapper can delegate to it).
_original_get_available_models = None


def _databricks_first_models() -> dict[str, list[str]]:
    """Expose Databricks models without upstream direct-provider catalogs.

    Keep the historical helper name for callers of the boot shim. Explicit
    provider specs still construct normally; this scopes discovery in Manta's
    model picker, rather than changing provider configuration or credentials.
    """
    original = _original_get_available_models
    if original is None:
        return {}
    try:
        available = dict(original())
    except Exception:  # noqa: BLE001 - discovery must never crash the TUI
        return {}
    models = available.get(DATABRICKS_PROVIDER)
    return {DATABRICKS_PROVIDER: models} if models is not None else {}


def prefer_databricks_models() -> bool:
    """Install gateway-only discovery and the actual upstream catalog UI hooks.

    Wraps ``get_available_models`` on the ``model_config`` module (the
    canonical definition) plus any widget modules that already imported the
    symbol by value, so a lazy ``from ... import get_available_models`` in the
    selector/auth screens binds to the gateway variant. The current upstream
    selector also loads catalogs from its inference server, so patching this
    function alone cannot scope ``/model``. Idempotent.

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
        "deepagents_code.model_catalog",
        "deepagents_code.tui.widgets.model_selector",
        "deepagents_code.tui.widgets.auth",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "get_available_models"):
            module.get_available_models = _databricks_first_models
    return _install_gateway_model_picker()


def _gateway_picker_pins(current_spec: str | None = None) -> tuple[str, ...]:
    """Read explicit pins without treating old managed inventories as pins."""
    from manta_code.dcode import DEEPAGENTS_CONFIG_PATH, _read_toml

    names = []
    if current_spec and current_spec.startswith("databricks:"):
        names.append(current_spec.split(":", 1)[1])
    try:
        from manta_code.config import interactive_endpoints, load_config

        names.extend(interactive_endpoints(load_config()))
    except Exception:  # noqa: BLE001 - other explicit pins remain usable
        pass
    try:
        from manta_code.agents.defaults import merged_agents
        from manta_code.agents.registry import list_agents

        names.extend(
            agent.model.split(":", 1)[1] for agent in merged_agents(list_agents())
            if agent.model and agent.model.startswith("databricks:")
        )
    except Exception:  # noqa: BLE001 - corrupt registry must not empty the picker
        pass
    try:
        config = _read_toml(DEEPAGENTS_CONFIG_PATH)
        ownership = config.get("manta", {}).get("databricks_inventory", {})
        owned = set(ownership.get("models", ())) if ownership.get("version") == 1 else set()
        registered = config.get("models", {}).get("providers", {}).get("databricks", {}).get("models", ())
        for name in registered:
            if isinstance(name, str) and name not in owned:
                names.append(name)
    except Exception:  # noqa: BLE001 - malformed config still has the launch pins
        pass
    return tuple(dict.fromkeys(names))


def _gateway_picker_inventory(current_spec: str | None = None) -> object:
    """Discover the active workspace, retaining intentional user pins.

    Discovery is bounded and cached by auth context, workspace, profile,
    schemas, and configured names. Never use an old server catalog as another
    workspace's inventory, including when fresh discovery fails.
    """
    from manta_code.auth import resolve_profile
    from manta_code.dcode import _target_inventory

    args = [arg for name in _gateway_picker_pins(current_spec) for arg in ("-M", f"databricks:{name}")]
    return _target_inventory(resolve_profile(), args)


def _gateway_model_catalog(catalog: object) -> object:
    """Scope either local or server catalogs to the active gateway inventory."""
    from deepagents_code.model_catalog import CatalogProfile, CatalogProvider
    from deepagents_code.model_config import ProviderAuthState
    from manta_code.model_capabilities import profile_for_target

    prefix = f"{DATABRICKS_PROVIDER}:"
    try:
        inventory = _gateway_picker_inventory(catalog.current_spec)
    except Exception:  # noqa: BLE001 - a discovery failure must leave pins usable
        inventory = None
    # The SDK result is scoped to the currently selected auth context. In
    # particular, do not union an old server catalog from another profile,
    # even when fresh discovery is unavailable. Explicit local pins survive.
    names = inventory.names if inventory is not None else _gateway_picker_pins(catalog.current_spec)
    models = [f"{prefix}{name}" for name in names]
    policy = catalog.presentation_config()
    models = sorted({spec for spec in models if policy.is_model_allowed(spec)})
    provider = CatalogProvider(
        state=ProviderAuthState.MANAGED,
        detail="Databricks workspace authentication; access follows workspace grants",
        display_name="Databricks AI Gateway",
        short_name="Databricks",
    )
    profiles = {}
    for spec in models:
        previous = catalog.profiles.get(spec)
        upstream_profile = previous.profile if previous is not None else None
        profile = profile_for_target(spec, upstream_profile=upstream_profile)
        if not profile and previous is None:
            continue
        profiles[spec] = CatalogProfile(
            profile={key: value for key, value in profile.items()
                     if value is None or isinstance(value, (str, int, bool))},
            overridden_keys=previous.overridden_keys if previous is not None else [],
        )
    return catalog.model_copy(update={
        "models": models,
        "profiles": profiles,
        "providers": {DATABRICKS_PROVIDER: provider},
        "current_spec": catalog.current_spec if catalog.current_spec in models else None,
    })


def _install_gateway_model_picker() -> bool:
    """Adapt Deep Agents' catalog-backed selector, including remote catalogs."""
    try:
        from textual.content import Content
        from textual.widgets import Input

        from deepagents_code.tui.widgets.model_selector import ModelSelectorScreen
    except Exception:
        return False
    if getattr(ModelSelectorScreen.__init__, "__manta_gateway_picker__", False):
        return True
    required = ("compose", "_catalog_model_data", "_apply_subset", "_info_line_content", "_help_text", "_update_display", "action_select")
    if not all(callable(getattr(ModelSelectorScreen, name, None)) for name in required):
        return False
    original_init = ModelSelectorScreen.__init__
    original_data = ModelSelectorScreen._catalog_model_data
    original_select = ModelSelectorScreen.action_select
    original_help = ModelSelectorScreen._help_text
    original_compose = ModelSelectorScreen.compose
    original_display = ModelSelectorScreen._update_display

    def _init(self: object, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        self._recommended_only = False
        self._show_specs = True
        if self._title is None:
            self._title = "Select Databricks AI Gateway Model"
        self._recommended_models = {
            spec: name for spec, name in self._recommended_models.items()
            if spec.startswith(f"{DATABRICKS_PROVIDER}:")
        }

    def _compose(self: object) -> object:
        for widget in original_compose(self):
            if isinstance(widget, Input) and widget.id == "model-filter":
                widget.placeholder = "Search gateway models or enter catalog.schema.service..."
            yield widget

    def _catalog_model_data(catalog: object, *, include_recent: bool, default_scope: object) -> object:
        data = original_data(
            _gateway_model_catalog(catalog), include_recent=include_recent,
            default_scope=default_scope,
        )
        visible = {spec for spec, _provider in data.all_models}
        return data._replace(
            recent_specs=[spec for spec in data.recent_specs if spec in visible],
            default_spec=data.default_spec if data.default_spec in visible else None,
        )

    def _apply_subset(self: object, all_models: list[tuple[str, str]]) -> list[tuple[str, str]]:
        # Every discovered workspace model is visible immediately. Upstream's
        # Ctrl+R recommendation toggle must never hide an uncurated service.
        return [(spec, provider) for spec, provider in all_models if provider == DATABRICKS_PROVIDER]

    def _info_line_content(self: object) -> object:
        return Content.styled(
            "Search Databricks AI Gateway models" if self._filter_text.strip()
            else "All Databricks AI Gateway models · workspace authentication"
        )

    async def _update_display(self: object) -> None:
        await original_display(self)
        if self._filtered_models or not self._loaded or self._options_container is None:
            return
        from textual.widgets import Static

        typed = self._filter_text.strip()
        if ":" not in typed or typed.startswith(f"{DATABRICKS_PROVIDER}:"):
            return
        for widget in self._options_container.children:
            if isinstance(widget, Static):
                widget.update(Content.styled("No matching gateway models. Direct API providers are outside this picker.", "dim"))

    def _help_text(self: object) -> str:
        # Keep upstream's navigation and correct preference scope, while
        # removing a recommendation toggle that no longer narrows the list.
        from deepagents_code.config import get_glyphs

        separator = f" {get_glyphs().bullet} "
        return separator.join(
            part for part in original_help(self).split(separator)
            if part != "Ctrl+R recommended"
        )

    def _action_select(self: object) -> None:
        if self._filtered_models:
            original_select(self)
            return
        typed = self.query_one("#model-filter", Input).value.strip()
        if not typed:
            return
        if ":" in typed and not typed.startswith(f"{DATABRICKS_PROVIDER}:"):
            self.notify("Choose a Databricks AI Gateway model; direct API providers are outside this picker.", severity="warning", markup=False)
            return
        spec = typed if typed.startswith(f"{DATABRICKS_PROVIDER}:") else f"{DATABRICKS_PROVIDER}:{typed}"
        blocked = self._presentation_config().policy_error(spec, canonicalize=False)
        if blocked is not None:
            self.notify(str(blocked), severity="error", markup=False)
            return
        self._select_with_auth_check(spec, DATABRICKS_PROVIDER)

    _init.__manta_gateway_picker__ = True
    ModelSelectorScreen.__init__ = _init
    ModelSelectorScreen.compose = _compose
    ModelSelectorScreen._catalog_model_data = staticmethod(_catalog_model_data)
    ModelSelectorScreen._apply_subset = _apply_subset
    ModelSelectorScreen._info_line_content = _info_line_content
    ModelSelectorScreen._help_text = _help_text
    ModelSelectorScreen._update_display = _update_display
    ModelSelectorScreen.action_select = _action_select
    return True


def switch_databricks_workspace(app: object, profile: str) -> None:
    """Switch the live session to Databricks ``profile`` and restart the server.

    Aligns both profile environment selectors so the next server spawn
    authenticates against the new workspace (``deepagents-code`` snapshots
    ``os.environ`` in ``server._build_server_env`` at spawn time), then triggers
    a server respawn via the app's ``/restart`` handler. The conversation thread
    is preserved across the restart.

    Best-effort: if the running ``deepagents-code`` build exposes no
    ``_restart_server_manual`` hook, the env is still updated and the user is
    told to relaunch.
    """
    import asyncio
    import os

    os.environ["MANTA_PROFILE"] = profile
    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    from manta_code.providers.discovery import clear_discovery_cache

    clear_discovery_cache()

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

    model_spec = None
    effective_model = getattr(app, "_effective_model_spec", None)
    if callable(effective_model):
        try:
            model_spec = effective_model()
        except Exception:  # noqa: BLE001 - configured pins still refresh
            pass

    async def refresh_and_restart() -> None:
        await asyncio.to_thread(_refresh_workspace_inventory, profile, model_spec)
        await restart()

    task = asyncio.create_task(refresh_and_restart())
    task.add_done_callback(_done)


def _refresh_workspace_inventory(profile: str, model_spec: str | None = None) -> None:
    """Replace Manta's previous inventory before spawning the new workspace."""
    from manta_code.config import load_config
    from manta_code.dcode import _target_inventory, databricks_provider_params, ensure_dcode_config
    from manta_code.providers.targets import export_target_policy

    cfg = load_config()
    inventory = _target_inventory(
        profile, ["-M", model_spec] if model_spec else [], refresh=True,
    )
    ensure_dcode_config(inventory.names, default_endpoint=cfg.interactive.default_endpoint)
    os.environ["MANTA_DATABRICKS_POLICY"] = export_target_policy(
        cfg, provider_params=databricks_provider_params(), inventory=inventory,
    )
    # Deep Agents caches parsed TOML in process; the picker must read the new
    # registration instead of keeping the previous workspace's models.
    from deepagents_code.model_config import clear_caches

    clear_caches()


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

        from deepagents_code.config import get_glyphs
        from deepagents_code.tui.widgets.auth import AuthManagerScreen

        from manta_code import auth
    except Exception:
        return False

    selected_handler = AuthManagerScreen.on_option_list_option_selected
    original_selected = getattr(selected_handler, "__manta_original__", selected_handler)
    mount_handler = AuthManagerScreen.on_mount
    original_mount = getattr(mount_handler, "__manta_original__", mount_handler)

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
                id="auth-manager-help",
            )

    def _on_mount(self: object) -> None:
        original_mount(self)
        try:
            workspace_list = self.query_one("#manta-workspace-options", OptionList)
            # The workspace list is short; size to content so the provider
            # list below keeps most of the modal (its upstream `1fr` CSS).
            workspace_list.styles.height = "auto"
            workspace_list.styles.max_height = 8
        except Exception:
            pass

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

        # Keep upstream's OAuth, install-on-select, service credentials, and
        # provider-specific callbacks intact as that screen evolves.
        original_selected(self, event)

    _on_option_selected.__manta_original__ = original_selected
    _on_mount.__manta_original__ = original_mount
    AuthManagerScreen.compose = _compose
    AuthManagerScreen.on_mount = _on_mount
    AuthManagerScreen.on_option_list_option_selected = _on_option_selected
    AuthManagerScreen.action_cursor_down = _action_cursor_down
    AuthManagerScreen.action_cursor_up = _action_cursor_up
    return True


def extend_recommended_models() -> bool:
    """Keep only Databricks recommendations for startup/catalog requests.

    The selector shows the complete gateway inventory. Recommendations supply
    names and explicit configured pins, without reintroducing direct-provider
    models from upstream's installable-provider shortlist.
    """
    try:
        from deepagents_code.tui.widgets import model_selector
    except Exception:
        return False

    current = getattr(model_selector, "_RECOMMENDED_MODELS", None)
    if current is None:
        return False

    specs: set[str] = set()
    try:
        from manta_code.config import interactive_endpoints, load_config

        specs.update(
            f"{DATABRICKS_PROVIDER}:{ep}" for ep in interactive_endpoints(load_config())
        )
    except Exception:  # noqa: BLE001 - config trouble loses only curation
        pass
    try:
        from manta_code.agents.defaults import merged_agents
        from manta_code.agents.registry import list_agents

        specs.update(
            defn.model
            for defn in merged_agents(list_agents())
            if defn.model and defn.model.startswith(f"{DATABRICKS_PROVIDER}:")
        )
    except Exception:  # noqa: BLE001
        pass
    model_selector._RECOMMENDED_MODELS = {
        **{spec: name for spec, name in current.items() if spec.startswith(f"{DATABRICKS_PROVIDER}:")},
        **{spec: current.get(spec, spec.split(":", 1)[-1]) for spec in specs},
    }
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

        from deepagents_code.tui.widgets.model_selector import ModelSelectorScreen
    except Exception:
        return False

    original_update_footer = ModelSelectorScreen._update_footer
    if getattr(original_update_footer, "__manta_gateway_footer__", False):
        return True

    def _update_footer(self: object) -> None:
        if self._filtered_models:
            index = min(self._selected_index, len(self._filtered_models) - 1)
            spec, _ = self._filtered_models[index]
            if spec.startswith(f"{DATABRICKS_PROVIDER}:"):
                from manta_code.model_capabilities import format_capability_card
                footer = self.query_one("#model-detail-footer", Static)
                profile = self._profiles.get(spec)
                values = profile.get("profile", {}) if isinstance(profile, dict) else getattr(profile, "profile", {})
                overrides = profile.get("overridden_keys", []) if isinstance(profile, dict) else getattr(profile, "overridden_keys", [])
                cli = getattr(self, "_cli_profile_override", None) or {}
                footer.update(
                    Content.styled(format_capability_card(spec,
                        effective_profile={**values, **cli},
                        overridden_keys=list(set(overrides) | cli.keys())), "dim")
                )
                return
        original_update_footer(self)

    _update_footer.__manta_gateway_footer__ = True
    ModelSelectorScreen._update_footer = _update_footer
    return True


def allow_blocking_server() -> bool:
    """Let the LangGraph dev server tolerate Databricks' blocking auth.

    ``deepagents-code`` runs the agent in a ``langgraph dev`` subprocess whose
    in-memory runtime arms ``blockbuster`` to raise on any synchronous I/O on
    the event loop. The Databricks SDK's ``databricks-cli`` auth is genuinely
    blocking — it resolves the ``databricks`` binary via ``os.readlink`` and
    shells out for an OAuth token — and exposes no async driver, so the first
    synchronous Databricks-authenticated request can trip ``blockbuster`` with
    ``ValueError: ... Blocking call to os.readlink``.

    ``langgraph dev`` only disables that detector when invoked with
    ``--allow-blocking`` (it sets ``LANGGRAPH_ALLOW_BLOCKING`` itself, so an
    inherited env var is overwritten — the flag is the sole seam). This wraps
    :func:`deepagents_code.client.launch.server._build_server_cmd` to append
    the flag. The
    detector is a deployment-hygiene aid for multi-tenant ASGI servers;
    disabling it is the upstream-recommended remedy (option 3) and benign for
    this single-user local dev server.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from deepagents_code.client.launch import server
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
      atomic ``resume_thread_id`` transition (the same machinery as the
      ``/threads`` picker), so switching agents continues your session instead
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
            from deepagents_code.tui.widgets.message_store import MessageType

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

    async def wrapped(
        self: object,
        agent_name: str,
        *,
        resume_thread_id: str | None = None,
        preloaded_payload: object = None,
        persist_default_agent: bool = True,
    ) -> bool:
        previous_thread = (
            _resumable_thread(self)
            if resume_thread_id is None and preloaded_payload is None and _swap_resume_enabled()
            else None
        )
        result = await original_swap(
            self,
            agent_name,
            resume_thread_id=resume_thread_id or previous_thread,
            preloaded_payload=preloaded_payload,
            persist_default_agent=persist_default_agent,
        )
        if result is False:
            return False
        if resume_thread_id is not None or preloaded_payload is not None:
            # Upstream has already adopted the saved thread model (or retained
            # the user's explicit launch model). A cross-agent thread resume
            # must not replace that choice with today's role default.
            return result
        try:
            if getattr(self, "_assistant_id", None) != agent_name:
                return result  # swap failed and rolled back; leave everything alone
            if previous_thread:
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
        return result

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
        from deepagents_code.tui.widgets.autocomplete import FuzzyFileController
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


def _session_cost_report(thread_id: str | None) -> str:
    """Plain-text spend report for the current session (the ``/cost`` command).

    The session's per-agent totals from the local ledger, the most recent
    model calls (per-call cost — the closest honest thing to "cost per
    turn"), today's total, and the daily cap when configured.
    """
    from manta_code.agents.usage import recent_calls, thread_totals, today_totals

    lines: list[str] = []
    rows = thread_totals(thread_id or "")
    if rows:
        total = sum(r.cost_usd for r in rows)
        tokens = sum(r.total_tokens for r in rows)
        complete = all(r.cost_known for r in rows)
        label = "Session spend" if complete else "Session known subtotal"
        lines.append(f"{label}: ${total:.4f}  ({tokens:,} tokens)")
        for r in rows:
            lines.append(
                f"  {r.key:<14} {r.calls:>3} call(s)  "
                f"{r.total_tokens:>10,} tok  ${r.cost_usd:.4f}"
            )
            if not r.cost_known:
                lines.append(f"    {r.completeness_label}")
        calls = recent_calls(thread_id or "", limit=5)
        if calls:
            lines.append("Recent calls (newest first):")
            for c in calls:
                cost = f"${c.cost_usd:.4f}" if c.cost_usd is not None else "$?"
                lines.append(
                    f"  {c.agent:<14} {c.model:<34} "
                    f"{c.input_tokens:>8,}→{c.output_tokens:<7,} {cost}"
                )
                details = [f"kind={c.call_kind}", f"outcome={c.outcome}"]
                if c.reasoning_tokens is not None:
                    details.append(f"reasoning={c.reasoning_tokens:,} tokens")
                if c.cache_available:
                    details.append(f"cache read/write={c.cache_read:,}/{c.cache_creation:,}")
                if c.duration_ms is not None:
                    details.append(f"latency={c.duration_ms / 1000:.2f}s")
                if c.request_id:
                    details.append(f"request={c.request_id}")
                lines.append("    " + " · ".join(details))
    else:
        lines.append(
            "No spend recorded for this session yet (the ledger writes after "
            "each model call)."
        )
    today_row = today_totals()
    today = today_row.cost_usd
    today_label = "Today" if today_row.cost_known else "Today known subtotal"
    cap = None
    try:
        from manta_code.config import load_config

        cap = load_config().budget.daily_max_usd
    except Exception:  # noqa: BLE001
        pass
    if cap:
        lines.append(f"{today_label}: ${today:.2f} of ${cap:.2f} daily budget")
    else:
        lines.append(f"{today_label} (all sessions): ${today:.2f}")
    if not today_row.cost_known:
        lines.append(f"  {today_row.completeness_label}; unknown charges are excluded from the USD cap.")
    lines.append("More: manta receipts • manta cost --by model • manta status")
    return "\n".join(lines)


def add_session_cost_command() -> bool:
    """Register an in-session ``/cost`` command (token economy, at the front).

    Every Manta cost surface lived in a second terminal; ``/cost`` brings the
    session's own spend — per-agent totals, per-call costs, today's total
    against the daily budget — into the conversation. Classified
    side-effect-free so it answers even while the agent is busy.

    Returns ``True`` when the override was applied, ``False`` otherwise.
    """
    try:
        from deepagents_code import command_registry
        from deepagents_code.app import DeepAgentsApp
        from deepagents_code.tui.widgets.messages import AppMessage, UserMessage
    except Exception:
        return False

    original_handle = getattr(DeepAgentsApp, "_handle_command", None)
    if original_handle is None:
        return False
    if getattr(original_handle, "__manta_cost__", False):
        return True

    async def wrapped(self: object, command: str) -> None:
        cmd = command.lower().strip()
        if cmd in {"/cost", "/spend"}:
            try:
                await self._mount_message(UserMessage(command))
                report = _session_cost_report(getattr(self, "_lc_thread_id", None))
                await self._mount_message(AppMessage(report))
            except Exception:  # noqa: BLE001 - reporting must never break input
                pass
            return
        await original_handle(self, command)

    wrapped.__manta_cost__ = True  # type: ignore[attr-defined]
    DeepAgentsApp._handle_command = wrapped

    # Autocomplete + busy-state classification for the new command.
    try:
        entry = command_registry.SlashCommand(
            name="/cost",
            description="Session spend: per-agent totals, per-call costs, daily budget",
            bypass_tier=command_registry.BypassTier.SIDE_EFFECT_FREE,
            hidden_keywords="tokens spend price budget receipts money usage",
            aliases=("/spend",),
        )
        if any(e.name == "/cost" for e in command_registry.COMMANDS):
            command_registry.COMMANDS = tuple(
                entry if e.name == "/cost" else e for e in command_registry.COMMANDS
            )
        else:
            command_registry.COMMANDS = (*command_registry.COMMANDS, entry)
        command_registry.SIDE_EFFECT_FREE = frozenset(
            command_registry.SIDE_EFFECT_FREE | {"/cost", "/spend"}
        )
        command_registry.QUEUE_BOUND = frozenset(
            command_registry.QUEUE_BOUND - {"/cost", "/spend"}
        )
        command_registry.ALL_CLASSIFIED = frozenset(
            command_registry.ALL_CLASSIFIED | {"/cost", "/spend"}
        )
    except Exception:  # noqa: BLE001 - autocomplete is a nicety; /cost still works
        pass
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
    """Apply Manta branding and workspace model scope, then run upstream."""
    from manta_code.accounting import install_usage_collector
    from manta_code.budget_runtime import install_budget_runtime
    from manta_code.model_capabilities import install_model_capability_bridge
    from manta_code.updates import install_runtime_update_adapter

    for name, installer in (
        ("request accounting", install_usage_collector),
        ("budget checkpoints", install_budget_runtime),
        ("Gateway model capability catalog", install_model_capability_bridge),
        ("qualified Manta updates", install_runtime_update_adapter),
        ("agent control plane", install_manta_build_hook),
    ):
        if not installer():
            raise RuntimeError(f"Required Manta integration unavailable: {name}. Run manta doctor.")
    degraded: list[str] = []
    if not apply_branding():
        degraded.append("Manta welcome banner")
    if not rebrand_model_selector_footer():
        degraded.append("Databricks model details")
    if not extend_recommended_models():
        degraded.append("Databricks model recommendations")
    if not prefer_databricks_models():
        degraded.append("Databricks workspace model picker")
    if not rebrand_auth_screen():
        degraded.append("workspace picker in /auth")
    if not align_agent_switch_model():
        degraded.append("agent-pin model alignment in /agents")
    if not add_agent_mentions_to_autocomplete():
        degraded.append("@agent autocomplete")
    if not add_session_cost_command():
        degraded.append("in-session /cost")
    if not allow_blocking_server():
        degraded.append("Databricks auth shim")
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
