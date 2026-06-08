# Sprint 1 — Core CLI Foundation

## Goal

Build the minimal CLI shell and local state system.

## Deliverables

- `manta init`
- `manta route`
- `manta run --dry-run`
- project config loading
- session JSONL writer
- status command

## Stories

### S1-1 Project initialization

Acceptance:

- Creates `.manta/config.toml`.
- Creates `.manta/sessions`, `.manta/context`, `.manta/reports`.
- Does not overwrite existing config without confirmation.

### S1-2 Session logging

Acceptance:

- Every run gets a session ID.
- Events are appended as JSONL.
- `manta status` can show last session.

### S1-3 Dry-run pipeline

Acceptance:

- `manta run "..." --dry-run` routes task and writes route event.
- No model credentials required.

## Risks

- Avoid overbuilding TUI before core contract is stable.
