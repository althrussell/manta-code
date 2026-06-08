# ADR 0004 — Budget engine uses hard caps

## Status

Accepted.

## Decision

Manta will enforce hard budget caps per task and per role.

## Rationale

The product promise requires no token bill shock. Cost display alone is insufficient; the loop must stop when the next action would exceed budget.

## Consequences

- Every model call must be estimated before execution.
- Every model call must be recorded after execution.
- Users can approve explicit escalation.
- Some tasks will end in partial completion reports.
