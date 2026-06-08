# ADR 0002 — Do not fork Goose for v1

## Status

Accepted for v1 bootstrap.

## Context

Goose is a strong open-source local agent product with CLI, desktop, API, MCP extensions, subagents, recipes, and security patterns.

## Decision

Study Goose and borrow product/runtime patterns, but do not fork it for v1.

## Rationale

Manta's core wedge is the budgeted role pipeline. Forking Goose risks inheriting broad product complexity before the Manta control plane is proven.

## Consequences

Positive:

- Cleaner Manta architecture.
- Faster iteration on budget/router/role model.
- Less dependency on Goose internals.

Negative:

- We must build our own CLI/session/policy plumbing.
- We do not inherit Goose's mature MCP ecosystem immediately.
