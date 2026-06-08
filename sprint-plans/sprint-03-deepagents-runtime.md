# Sprint 3 — Deep Agents Runtime

## Goal

Wire the first real model-backed role execution through the Deep Agents adapter.

## Deliverables

- AgentRuntime protocol.
- DeepAgentsRuntime implementation.
- Builder role invocation.
- Reviewer role invocation.
- Structured outputs.
- Tool policy wrapper.

## Stories

### S3-1 Runtime adapter

Acceptance:

- Manta pipeline calls `AgentRuntime.run_role`.
- Deep Agents implementation is isolated to adapter module.

### S3-2 Builder role

Acceptance:

- Builder can read selected files.
- Builder can propose patch.
- Writes require policy approval or auto-safe mode.

### S3-3 Reviewer role

Acceptance:

- Reviewer receives diff and acceptance criteria.
- Reviewer returns structured approval/findings.

## Risks

- Deep Agents SDK API changes.
- Provider token usage data differs by model.
- Tool permissions need Manta wrapper, not just Deep Agents permissions.
