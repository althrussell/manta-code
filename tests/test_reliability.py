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
