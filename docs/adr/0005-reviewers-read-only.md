# ADR 0005 — Reviewers are read-only by default

## Status

Superseded by [ADR 0007](0007-adopt-deepagents-code-tui.md). The reviewer roles
are parked along with the rest of the role pipeline. Retained for historical
context.

## Decision

Code reviewer and security reviewer roles cannot edit source files by default.

## Rationale

Reviewer independence is weakened if the reviewer can silently fix or mutate the code it is evaluating.

## Consequences

- Reviewers output structured findings.
- Builder performs fixes.
- Review loops are explicit and auditable.
