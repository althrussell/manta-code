"""Verify the upstream symbols Manta monkeypatches still exist.

Manta's control plane (ADR 0008) is layered onto ``deepagents-code`` by
monkeypatching a few internal symbols rather than forking:

- ``deepagents_code.agent.create_deep_agent`` — wrapped by
  :mod:`manta_code.hook` to inject Manta agents, middleware, store, and tools.
- ``deepagents_code.agent.create_cli_agent`` — the CLI agent builder whose
  ``create_deep_agent`` call is the seam we hook.
- ``deepagents._models.resolve_model`` — wrapped by
  :mod:`manta_code.databricks_chat` so subagent ``databricks:<endpoint>`` pins
  resolve to Manta's provider.

Pinning the upstream versions (see ``pyproject.toml``) is the first line of
defence; this module is the second. :func:`verify_patch_targets` is called by
``manta doctor`` (so users see breakage before launch) and by
``tests/test_reliability.py`` (so an upstream bump that moves a symbol fails CI
rather than a user's launch). The build hook itself degrades gracefully — see
:func:`manta_code.hook.install_build_hook` — so a missing target never blocks
``manta`` from starting; it only loses the Manta control-plane enrichment.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PatchTarget:
    """A single upstream symbol Manta depends on being able to monkeypatch.

    ``attribute`` may be dotted (``Class.method``) to probe an attribute on a
    class. ``kind`` selects the check: ``"callable"`` (default) requires a
    callable symbol; ``"attr"`` only requires the symbol to exist (used for
    the banner string constants).
    """

    module: str
    attribute: str
    purpose: str
    kind: str = "callable"


#: Every internal upstream symbol Manta patches or reads by reflection — the
#: hook/resolver seams plus every ``_boot`` patch surface (ADR 0010 closed the
#: gap where ``_boot``'s targets were unverified). Keep this list in sync with
#: the modules referenced in each ``purpose``.
PATCH_TARGETS: tuple[PatchTarget, ...] = (
    PatchTarget(
        module="langchain_openai.chat_models.base",
        attribute="BaseChatOpenAI._create_chat_result",
        purpose="capture Unity Chat tool and content signatures before upstream normalization",
    ),
    PatchTarget(
        module="langchain_openai.chat_models.base",
        attribute="BaseChatOpenAI._convert_chunk_to_generation_chunk",
        purpose="capture Unity Chat stream signatures before upstream normalization",
    ),
    PatchTarget(
        module="langchain_openai.chat_models.base",
        attribute="_convert_responses_chunk_to_generation_chunk",
        purpose="context-scoped preservation of Unity function-call encrypted continuation state",
    ),
    PatchTarget(
        module="deepagents_code.agent",
        attribute="create_deep_agent",
        purpose="build hook (manta_code.hook) injects agents/middleware/store/tools",
    ),
    PatchTarget(
        module="deepagents_code.agent",
        attribute="create_cli_agent",
        purpose="CLI agent builder whose create_deep_agent call is the hook seam",
    ),
    PatchTarget(
        module="deepagents._models",
        attribute="resolve_model",
        purpose="subagent provider:model resolver (manta_code.databricks_chat)",
    ),
    PatchTarget(
        module="deepagents_code.config",
        attribute="create_model",
        purpose="explicit constructor-option provenance for shared Databricks target policy",
    ),
    PatchTarget(
        module="deepagents_code.config",
        attribute="_create_model_from_class",
        purpose="class-path construction provenance, including first import of Databricks adapter",
    ),
    PatchTarget(
        module="deepagents_code.config",
        attribute="_UNICODE_BANNER",
        purpose="splash banner constant rebranded by manta_code._boot",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.config",
        attribute="_ASCII_BANNER",
        purpose="splash banner constant rebranded by manta_code._boot",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.welcome",
        attribute="WelcomeBanner._build_banner",
        purpose="current TUI welcome rendering rebranded by manta_code.branding",
    ),
    PatchTarget(
        module="deepagents_code.app",
        attribute="DeepAgentsApp.TITLE",
        purpose="Manta app title used by the interactive header",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code._invocation",
        attribute="invoked_name.cache_clear",
        purpose="refresh terminal command identity after setting Manta invocation",
    ),
    PatchTarget(
        module="deepagents_code.model_config",
        attribute="get_available_models",
        purpose="model discovery scoped to Databricks by manta_code._boot",
    ),
    PatchTarget(
        module="deepagents_code.model_catalog",
        attribute="get_available_models",
        purpose="model catalog's imported discovery function wrapped by manta_code._boot",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.auth",
        attribute="AuthManagerScreen.compose",
        purpose="/auth screen recomposed (workspace picker + provider keys)",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.auth",
        attribute="AuthManagerScreen._build_options_with_warning",
        purpose="upstream provider-key list reused by Manta's /auth compose",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.auth",
        attribute="AuthManagerScreen.on_option_list_option_selected",
        purpose="upstream auth provider/OAuth handling retained by Manta's /auth screen",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.model_selector",
        attribute="ModelSelectorScreen._update_footer",
        purpose="/model footer wrapped for profile-less Databricks endpoints",
    ),
    *(
        PatchTarget(
            module="deepagents_code.tui.widgets.model_selector",
            attribute=f"ModelSelectorScreen.{method}",
            purpose="workspace-only /model catalog, filtering, and selection",
        )
        for method in (
            "__init__",
            "compose",
            "_catalog_model_data",
            "_apply_subset",
            "_update_display",
            "_info_line_content",
            "_help_text",
            "action_select",
            "_select_with_auth_check",
        )
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.model_selector",
        attribute="_RECOMMENDED_MODELS",
        purpose="/model optional recommendations scoped to Databricks targets",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.client.launch.server",
        attribute="_build_server_cmd",
        purpose="server cmd wrapped to add --allow-blocking (Databricks auth)",
    ),
    PatchTarget(
        module="deepagents_code.app",
        attribute="DeepAgentsApp._restart_server_for_agent_swap",
        purpose="/agents swap wrapped to apply the Manta agent's model pin",
    ),
    PatchTarget(
        module="deepagents_code.app",
        attribute="DeepAgentsApp._switch_model",
        purpose="upstream model switch reused for agent-pin alignment",
    ),
    PatchTarget(
        module="deepagents_code.app",
        attribute="DeepAgentsApp._resume_thread",
        purpose="upstream thread resume reused for conversation continuity on /agents swap",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.message_store",
        attribute="MessageType",
        purpose="agent-output gating for swap continuity",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.app",
        attribute="DeepAgentsApp._handle_command",
        purpose="/cost in-session spend command (wrapped dispatch)",
    ),
    PatchTarget(
        module="deepagents_code.command_registry",
        attribute="COMMANDS",
        purpose="/cost autocomplete entry appended",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.command_registry",
        attribute="SlashCommand",
        purpose="/cost command definition",
    ),
    PatchTarget(
        module="deepagents_code.command_registry",
        attribute="BypassTier.SIDE_EFFECT_FREE",
        purpose="/cost remains available while the model is busy",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.autocomplete",
        attribute="FuzzyFileController._get_fuzzy_suggestions",
        purpose="@ completion extended with Manta agent names",
    ),
    PatchTarget(
        module="deepagents_code.tui.widgets.autocomplete",
        attribute="FuzzyFileController.on_text_changed",
        purpose="@ completion position check for agent addressing",
    ),
)


@dataclass(frozen=True)
class PatchTargetResult:
    """Result of probing one :class:`PatchTarget`."""

    target: PatchTarget
    ok: bool
    detail: str


def verify_patch_targets(
    targets: tuple[PatchTarget, ...] = PATCH_TARGETS,
) -> list[PatchTargetResult]:
    """Probe each patch target, reporting whether the symbol still exists.

    Pure and side-effect-free: it imports the upstream module and checks the
    attribute is present and callable. Missing module, missing attribute, and
    non-callable attribute are all reported as ``ok=False`` with a human-readable
    detail rather than raising — callers decide how loud to be.
    """
    results: list[PatchTargetResult] = []
    for target in targets:
        try:
            module = importlib.import_module(target.module)
        except Exception as exc:  # noqa: BLE001 - report, don't crash doctor
            results.append(
                PatchTargetResult(target, False, f"module not importable: {exc}")
            )
            continue
        # Dotted attributes (``Class.method``) walk into the class.
        symbol: object = module
        missing = False
        for part in target.attribute.split("."):
            symbol = getattr(symbol, part, None)
            if symbol is None:
                missing = True
                break
        if missing:
            results.append(
                PatchTargetResult(
                    target,
                    False,
                    f"missing {target.module}.{target.attribute} (upstream moved it?)",
                )
            )
            continue
        if target.kind == "callable" and not callable(symbol):
            results.append(
                PatchTargetResult(
                    target, False, f"{target.attribute} is not callable"
                )
            )
            continue
        results.append(PatchTargetResult(target, True, "present"))
    return results


def all_targets_ok(
    targets: tuple[PatchTarget, ...] = PATCH_TARGETS,
) -> bool:
    """Return ``True`` when every patch target is present and callable."""
    return all(result.ok for result in verify_patch_targets(targets))
