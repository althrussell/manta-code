# Deep Agents Integration Plan

## Why Deep Agents

Deep Agents gives Manta a programmable harness for:

- long-running tasks,
- subagents,
- model overrides by subagent,
- tools,
- filesystem access,
- skills,
- context management,
- memory,
- human-in-the-loop interruptions.

## Integration strategy

Do not couple every Manta module directly to Deep Agents.

Use an adapter:

```text
Manta pipeline → AgentRuntime interface → DeepAgentsRuntime implementation
```

This keeps Manta free to:

- use Deep Agents SDK directly,
- drop to LangGraph for custom flows,
- call Deep Agents Code where useful,
- or create a custom runtime later.

## Adapter interface

```python
class AgentRuntime(Protocol):
    def run_role(self, role: RoleSpec, context: ContextPack) -> RoleResult:
        ...
```

## Deep Agents subagent mapping

Manta role fields map to Deep Agents subagent fields:

| Manta role field | Deep Agents field |
|---|---|
| role name | `name` |
| role purpose | `description` |
| role prompt | `system_prompt` |
| model binding | `model` |
| allowed tools | `tools` |
| role skills | `skills` |
| output schema | `response_format` |
| filesystem rules | `permissions` |
| approval gates | `interrupt_on` + Manta policy |

## Manta-specific controls not delegated to Deep Agents

Manta owns:

- budget hard caps,
- model price table,
- context broker,
- shell policy,
- network policy,
- git policy,
- tool-call audit log,
- security review activation,
- route-to-pipeline decision.

## First implementation milestone

Sprint 3 should implement a minimal `DeepAgentsRuntime` that:

- creates role-specific subagents,
- runs a builder role,
- runs a reviewer role,
- captures structured output,
- records model usage if available,
- respects Manta policy wrapper for tools.

### Status: implemented (Sprint 3, S3-1 → S3-3)

The milestone is implemented behind the adapter:

- `src/manta_cli/agents/base.py` — `AgentRuntime` protocol (the boundary).
- `src/manta_cli/agents/factory.py` — `get_runtime(dry_run=...)` selects the
  offline `MockRuntime` (default) or the `DeepAgentsRuntime`. The Deep Agents
  import is deferred until a real run, so the CLI and tests work without the
  `[agent]` extra.
- `src/manta_cli/agents/deepagents_adapter.py` — the **only** module that
  imports Deep Agents. Maps a `RoleSpec` to `create_deep_agent(model=...,
  system_prompt=...(from prompts/roles), tools=..., response_format=ReviewReport
  for reviewers)`, invokes it, aggregates `usage_metadata` into `TokenUsage`,
  and parses output into a `RoleResult` (reviewers → structured `ReviewReport`).
- `src/manta_cli/agents/tools.py` — policy-wrapped tool callables. Every
  side-effecting tool routes through `PolicyEngine`; reviewers receive
  read-only tools only (ADR 0005).
- `src/manta_cli/pipeline.py` — `MantaPipeline.run()` records each role's token
  usage into the `BudgetLedger` and stops the pipeline when a hard cap would be
  exceeded or a reviewer blocks.

Run a real pipeline with `manta run "..." --no-dry-run` (requires
`pip install -e '.[agent]'` and provider credentials). Without the extra, the
CLI prints a friendly message and falls back to the dry-run scaffold.

## Caution

Deep Agents filesystem permissions do not protect custom tools, MCP tools, or arbitrary shell execution by themselves. Manta's policy engine must wrap all side-effecting tools regardless of runtime.
