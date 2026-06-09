# ADR 0001 — Use Deep Agents SDK as the initial harness

## Status

Superseded by [ADR 0007](0007-adopt-deepagents-code-tui.md). Manta adopts the
`deepagents-code` TUI rather than building on the raw Deep Agents SDK. Retained
for historical context.

## Context

Manta needs model-agnostic orchestration, subagents, skills, context management, filesystem tools, and human-in-the-loop controls.

## Decision

Use Deep Agents SDK behind a Manta adapter for the initial role runtime.

## Consequences

Positive:

- Faster path to subagents and long-running workflows.
- Model-agnostic provider support through LangChain ecosystem.
- Skills and context management primitives are available.

Negative:

- Deep Agents API changes can impact us.
- We must still build budget, policy, and context broker ourselves.
- Shell/MCP security cannot be delegated entirely to Deep Agents permissions.

## Guardrail

Keep the adapter boundary strict:

```text
Manta pipeline -> AgentRuntime -> DeepAgentsRuntime
```
