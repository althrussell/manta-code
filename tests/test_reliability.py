from __future__ import annotations

import pytest

from manta_code import reliability


def test_patch_targets_are_declared():
    # The hook + resolver shim depend on these; if the list is empty the guard
    # is useless.
    assert reliability.PATCH_TARGETS
    names = {(t.module, t.attribute) for t in reliability.PATCH_TARGETS}
    assert ("deepagents_code.agent", "create_deep_agent") in names
    assert ("deepagents._models", "resolve_model") in names


def test_verify_reports_missing_module_without_raising():
    bogus = (reliability.PatchTarget("manta_code._nope_xyz", "foo", "test"),)
    results = reliability.verify_patch_targets(bogus)
    assert len(results) == 1
    assert results[0].ok is False
    assert "not importable" in results[0].detail


def test_verify_reports_missing_attribute_without_raising():
    # `manta_code.reliability` exists but has no `definitely_absent` attribute.
    target = reliability.PatchTarget("manta_code.reliability", "definitely_absent", "t")
    results = reliability.verify_patch_targets((target,))
    assert results[0].ok is False
    assert "missing" in results[0].detail


def test_upstream_patch_targets_present_when_installed():
    # Contract test: when deepagents-code is installed (the `agent` extra), every
    # symbol Manta monkeypatches MUST exist. An upstream bump that moves one
    # should fail here in CI rather than in a user's launch.
    pytest.importorskip("deepagents_code")
    pytest.importorskip("deepagents")
    results = reliability.verify_patch_targets()
    broken = [r for r in results if not r.ok]
    assert not broken, "upstream moved patched symbols: " + ", ".join(
        f"{r.target.module}.{r.target.attribute} ({r.detail})" for r in broken
    )


def test_boot_patch_surfaces_are_declared():
    # ADR 0010: every _boot.py patch target is contract-tested, not just the
    # hook/resolver seams.
    names = {(t.module, t.attribute) for t in reliability.PATCH_TARGETS}
    assert ("deepagents_code.config", "_UNICODE_BANNER") in names
    assert ("deepagents_code.model_config", "get_available_models") in names
    assert ("deepagents_code.widgets.auth", "AuthManagerScreen.compose") in names
    assert (
        "deepagents_code.widgets.auth",
        "AuthManagerScreen._build_options_with_warning",
    ) in names
    assert ("deepagents_code.server", "_build_server_cmd") in names
    assert (
        "deepagents_code.widgets.model_selector",
        "ModelSelectorScreen._update_footer",
    ) in names


def test_verify_walks_dotted_attributes():
    target = reliability.PatchTarget(
        "manta_code.reliability", "PatchTarget.__init__", "t"
    )
    results = reliability.verify_patch_targets((target,))
    assert results[0].ok is True


def test_verify_reports_missing_dotted_attribute():
    target = reliability.PatchTarget(
        "manta_code.reliability", "PatchTarget.definitely_absent", "t"
    )
    results = reliability.verify_patch_targets((target,))
    assert results[0].ok is False
    assert "missing" in results[0].detail


def test_verify_attr_kind_accepts_non_callable():
    target = reliability.PatchTarget(
        "manta_code.dcode", "DATABRICKS_PROVIDER", "t", kind="attr"
    )
    results = reliability.verify_patch_targets((target,))
    assert results[0].ok is True
