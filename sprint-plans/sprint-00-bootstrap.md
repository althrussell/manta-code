# Sprint 0 — Bootstrap

## Goal

Create the project foundation so development can begin with shared architecture, docs, config, and CI.

## Deliverables

- Repo structure.
- PRD and architecture docs.
- ADRs.
- Config examples.
- Initial Python package.
- CI workflow.
- Issue templates.

## Stories

### S0-1 Create repo scaffold

Acceptance:

- `src/manta_cli` exists.
- `pyproject.toml` exists.
- `manta doctor` command works after install.

### S0-2 Add docs baseline

Acceptance:

- Docs index exists.
- PRD, architecture, routing, context, security, and sprint docs exist.

### S0-3 Add CI baseline

Acceptance:

- GitHub Actions runs tests and ruff.

## Demo

```bash
pip install -e '.[dev]'
manta doctor
pytest
```
