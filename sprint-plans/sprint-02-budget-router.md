# Sprint 2 — Budget Router

## Goal

Implement budget-first routing and cost ledger.

## Deliverables

- heuristic router baseline,
- LLM router interface,
- route-to-pipeline mapping,
- price table loader,
- cost ledger,
- `manta budget` command.

## Stories

### S2-1 Route decision schema

Acceptance:

- Router returns strict schema.
- Invalid route decisions fail closed.

### S2-2 Budget ledger

Acceptance:

- Can estimate cost from model and token counts.
- Writes JSONL events.
- Blocks when hard cap exceeded.

### S2-3 Opus escalation rules

Acceptance:

- Opus is skipped for simple/trivial/normal routes.
- Opus allowed for complex/security routes only.

## Demo

```bash
manta route "what does this error mean?"
manta route "refactor auth and add token rotation"
manta budget --last
```
