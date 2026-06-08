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

## Caution

Deep Agents filesystem permissions do not protect custom tools, MCP tools, or arbitrary shell execution by themselves. Manta's policy engine must wrap all side-effecting tools regardless of runtime.
