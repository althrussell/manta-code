# Sprint 4 — Context Broker

## Goal

Control context per role and reduce token waste.

## Deliverables

- repo scanner,
- context manifest,
- file selection heuristic,
- token estimates,
- role context packs,
- log offloading.

## Stories

### S4-1 Repo map

Acceptance:

- Summarizes project structure.
- Excludes build artifacts and dependency folders.

### S4-2 Context manifest

Acceptance:

- Every run writes selected files and token estimates.
- Manifest records why files were selected.

### S4-3 Role context packs

Acceptance:

- Router gets minimal context.
- Builder gets selected files.
- Reviewer gets diff-first context.

## Demo

```bash
manta context --explain "add settings page"
```
