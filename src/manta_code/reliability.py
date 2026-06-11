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
        module="deepagents_code.model_config",
        attribute="get_available_models",
        purpose="model discovery wrapped Databricks-first by manta_code._boot",
    ),
    PatchTarget(
        module="deepagents_code.widgets.auth",
        attribute="AuthManagerScreen.compose",
        purpose="/auth screen recomposed (workspace picker + provider keys)",
    ),
    PatchTarget(
        module="deepagents_code.widgets.auth",
        attribute="AuthManagerScreen._build_options_with_warning",
        purpose="upstream provider-key list reused by Manta's /auth compose",
    ),
    PatchTarget(
        module="deepagents_code.widgets.auth",
        attribute="AuthPromptScreen",
        purpose="upstream API-key prompt pushed from Manta's /auth screen",
    ),
    PatchTarget(
        module="deepagents_code.model_config",
        attribute="get_credential_env_var",
        purpose="provider env-var lookup used by Manta's /auth screen",
    ),
    PatchTarget(
        module="deepagents_code.widgets.model_selector",
        attribute="ModelSelectorScreen._update_footer",
        purpose="/model footer wrapped for profile-less Databricks endpoints",
    ),
    PatchTarget(
        module="deepagents_code.widgets.model_selector",
        attribute="_RECOMMENDED_MODELS",
        purpose="/model default view extended with Manta's curated endpoints",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.server",
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
        module="deepagents_code.widgets.message_store",
        attribute="MessageType",
        purpose="agent-output gating for swap continuity",
        kind="attr",
    ),
    PatchTarget(
        module="deepagents_code.widgets.autocomplete",
        attribute="FuzzyFileController._get_fuzzy_suggestions",
        purpose="@ completion extended with Manta agent names",
    ),
    PatchTarget(
        module="deepagents_code.widgets.autocomplete",
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
