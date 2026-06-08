# ADR 0005 — Reviewers are read-only by default

## Status

Accepted.

## Decision

Code reviewer and security reviewer roles cannot edit source files by default.

## Rationale

Reviewer independence is weakened if the reviewer can silently fix or mutate the code it is evaluating.

## Consequences

- Reviewers output structured findings.
- Builder performs fixes.
- Review loops are explicit and auditable.
