# ADR 0003 — Use role pipeline instead of vendor agents

## Status

Accepted.

## Decision

Manta will define agents by role, not by model vendor.

```text
Router -> Planner -> Builder -> Reviewer -> Security Reviewer -> Release
```

## Rationale

Tasks need capabilities, not brands. A role-based design allows Manta to change model bindings without changing product behavior.

## Consequences

- Config must support model per role.
- Prompts and tools are role-scoped.
- Budget reports are role-scoped.
- Evals compare model-role combinations.
