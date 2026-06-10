from __future__ import annotations

import json

from manta_code.agents import importer, registry


def test_build_prompt_aggregates_claude_md_and_cursor_rules(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# House rules\nUse tabs.", encoding="utf-8")
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "style.mdc").write_text(
        "---\ndescription: style\n---\nPrefer small functions.", encoding="utf-8"
    )

    prompt, report = importer.build_imported_prompt(tmp_path)
    assert "Use tabs." in prompt
    assert "Prefer small functions." in prompt
    # Frontmatter stripped.
    assert "description: style" not in prompt
    assert report.claude_md is not None
    assert len(report.cursor_rules) == 1


def test_import_mcp_servers_merges_non_clobbering(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"linear": {"command": "x"}, "slack": {"command": "y"}}}),
        encoding="utf-8",
    )
    dest = tmp_path / "deepagents"
    dest.mkdir()
    (dest / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"slack": {"command": "existing"}}}), encoding="utf-8"
    )

    added = importer.import_mcp_servers(src, dest)
    assert added == ["linear"]  # slack kept as-is
    merged = json.loads((dest / ".mcp.json").read_text())
    assert merged["mcpServers"]["slack"]["command"] == "existing"
    assert merged["mcpServers"]["linear"]["command"] == "x"


def test_import_sources_creates_agent(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("Always write tests.", encoding="utf-8")
    dest_root = tmp_path / "manta"
    dest_cfg = tmp_path / "deepagents"

    report = importer.import_sources(
        proj, dest_root=dest_root, dest_config_dir=dest_cfg, agent_name="imported"
    )
    assert report.agent_created == "imported"
    loaded = registry.load_agent("imported", root=dest_root)
    assert "Always write tests." in loaded.system_prompt


def test_import_sources_reports_nothing_when_empty(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    report = importer.import_sources(
        proj, dest_root=tmp_path / "m", dest_config_dir=tmp_path / "d"
    )
    assert report.imported_anything is False
    assert report.agent_created is None
