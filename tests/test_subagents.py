from __future__ import annotations

from pathlib import Path

import pytest

from manta_code import subagents


def test_render_markdown_has_frontmatter_and_pinned_model():
    md = subagents.render_subagent_markdown(subagents.PLANNING)
    assert md.startswith("---\n")
    assert "name: planning\n" in md
    assert "description: " in md
    # PLANNING pins a Databricks endpoint, so the model line is present.
    assert 'model: "databricks:databricks-claude-opus-4-8"\n' in md


def test_render_markdown_omits_model_when_unset():
    spec = subagents.SubagentSpec(name="x", description="d", body="b", model=None)
    md = subagents.render_subagent_markdown(spec)
    # Without a pinned model the line is omitted (subagent inherits parent model).
    assert "model:" not in md


def test_all_manta_subagents_pin_a_databricks_model():
    for spec in subagents.MANTA_SUBAGENTS:
        assert spec.model is not None
        assert spec.model.startswith("databricks:"), spec.name


def test_generated_markdown_parses_with_deepagents_parser(tmp_path):
    parser = pytest.importorskip("deepagents_code.subagents")
    for spec in subagents.MANTA_SUBAGENTS:
        path = tmp_path / f"{spec.name}.md"
        path.write_text(subagents.render_subagent_markdown(spec), encoding="utf-8")
        parsed = parser._parse_subagent_file(path)
        assert parsed is not None, f"{spec.name} failed to parse"
        assert parsed["name"] == spec.name
        assert parsed["description"] == spec.description
        assert parsed["system_prompt"]
        assert parsed["model"] == spec.model


def test_ensure_writes_all_subagents_to_correct_paths(tmp_path):
    marker = tmp_path / ".state" / "marker"
    written = subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)

    expected = {
        tmp_path / "agent" / "agents" / spec.name / "AGENTS.md"
        for spec in subagents.MANTA_SUBAGENTS
    }
    assert set(written) == expected
    for path in expected:
        assert path.is_file()
    assert marker.exists()


def test_ensure_is_idempotent_via_marker(tmp_path):
    marker = tmp_path / ".state" / "marker"
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)
    # Second run: marker present -> nothing written.
    written = subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)
    assert written == []


def test_ensure_respects_user_deletion(tmp_path):
    marker = tmp_path / ".state" / "marker"
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)
    review = tmp_path / "agent" / "agents" / "review" / "AGENTS.md"
    review.unlink()
    # Marker already exists, so a deleted subagent is NOT recreated.
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)
    assert not review.exists()


def test_ensure_does_not_clobber_preexisting_file(tmp_path):
    marker = tmp_path / ".state" / "marker"
    swe = tmp_path / "agent" / "agents" / "swe" / "AGENTS.md"
    swe.parent.mkdir(parents=True)
    swe.write_text("custom user content", encoding="utf-8")

    written = subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)

    assert swe.read_text(encoding="utf-8") == "custom user content"
    assert swe not in written  # the pre-existing file was skipped


def test_render_double_quotes_escape(tmp_path):
    spec = subagents.SubagentSpec(
        name="x", description='has "quotes" and \\backslash', body="prompt body"
    )
    parser = pytest.importorskip("deepagents_code.subagents")
    path = tmp_path / "x.md"
    path.write_text(subagents.render_subagent_markdown(spec), encoding="utf-8")
    parsed = parser._parse_subagent_file(path)
    assert parsed is not None
    assert parsed["description"] == 'has "quotes" and \\backslash'


def test_user_subagents_dir_default(monkeypatch):
    path = subagents.user_subagents_dir(base_dir=Path("/tmp/dx"))
    assert path == Path("/tmp/dx/agent/agents")


def test_discover_subagents_reads_provisioned(tmp_path):
    marker = tmp_path / ".state" / "marker"
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)

    infos = subagents.discover_subagents(
        base_dir=tmp_path, project_root=tmp_path / "no-project"
    )
    by_name = {i.name: i for i in infos}
    assert set(by_name) == {"planning", "swe", "review"}
    assert by_name["planning"].model == "databricks:databricks-claude-opus-4-8"
    assert by_name["swe"].model == "databricks:databricks-gpt-5-5"
    assert by_name["review"].model == "databricks:databricks-gemini-3-1-pro"
    # Description round-trips (quotes unescaped), source + raw are populated.
    assert by_name["review"].source == "user"
    assert by_name["review"].description.startswith("Read-only code review")
    assert by_name["review"].raw.startswith("---\n")


def test_discover_subagents_empty_when_unprovisioned(tmp_path):
    assert (
        subagents.discover_subagents(
            base_dir=tmp_path, project_root=tmp_path / "none"
        )
        == []
    )


def test_discover_subagents_project_overrides_user(tmp_path):
    marker = tmp_path / ".state" / "marker"
    subagents.ensure_manta_subagents(base_dir=tmp_path, marker_path=marker)

    project_root = tmp_path / "proj"
    project_review = project_root / ".deepagents" / "agents" / "review" / "AGENTS.md"
    project_review.parent.mkdir(parents=True)
    project_review.write_text(
        '---\nname: review\ndescription: "project override"\n'
        'model: "databricks:custom-endpoint"\n---\n\nProject body.\n',
        encoding="utf-8",
    )

    by_name = {
        i.name: i
        for i in subagents.discover_subagents(
            base_dir=tmp_path, project_root=project_root
        )
    }
    assert by_name["review"].source == "project"
    assert by_name["review"].model == "databricks:custom-endpoint"


def test_parse_frontmatter_unescapes_quotes():
    fm, body = subagents._parse_frontmatter(
        '---\nname: x\ndescription: "has \\"quotes\\""\n---\n\nbody text\n'
    )
    assert fm["name"] == "x"
    assert fm["description"] == 'has "quotes"'
    assert body == "body text"
