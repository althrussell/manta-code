# System Architecture

## Architecture overview

```text
User
  ↓
Manta CLI / TUI
  ↓
Session Controller
  ↓
Cheap Router
  ↓
Budget Engine + Policy Engine
  ↓
Context Broker
  ↓
Role Pipeline
  ├─ Cheap Responder
  ├─ Opus Planner
  ├─ GPT Builder
  ├─ Test Agent
  ├─ Gemini Code Reviewer
  ├─ Gemini / Claude Security Reviewer
  └─ Release Agent
  ↓
Tool Layer
  ├─ filesystem
  ├─ git
  ├─ apply_patch
  ├─ shell runner
  ├─ test/lint runners
  ├─ dependency/security scanners
  └─ MCP tools
  ↓
State Layer
  ├─ .manta/sessions/*.jsonl
  ├─ .manta/ledger.jsonl
  ├─ .manta/context/*.json
  ├─ .manta/reports/*.md
  └─ ~/.manta/global config/memory
```

## Main components

### CLI layer

Responsible for:

- commands,
- flags,
- interactive confirmations,
- progress timeline,
- cost display,
- machine-readable output.

### Session controller

Responsible for:

- creating session IDs,
- writing event logs,
- resuming sessions,
- tracking current route/pipeline,
- handling stop conditions.

### Router

Responsible for:

- classifying intent,
- estimating complexity,
- estimating risk,
- selecting pipeline,
- assigning initial budget.

The router is deliberately cheap and should not receive large code context.

### Budget engine

Responsible for:

- loading price table,
- estimating cost,
- checking hard and soft budgets,
- recording usage,
- blocking further calls when caps are reached.

### Policy engine

Responsible for:

- shell allowlist,
- file path enforcement,
- network policy,
- git policy,
- secrets/env protections,
- approval requirements.

### Context broker

Responsible for:

- repo map,
- file selection,
- token estimation,
- context manifests,
- role-specific context packs.

### Role pipeline

Responsible for:

- invoking the correct role agents,
- passing structured outputs between roles,
- enforcing per-role permissions and budgets,
- stopping or looping based on reviewer/test output.

### Tool layer

All side-effecting work goes through tools. Tools must be policy-checked before execution.

### State layer

The state layer keeps Manta observable and resumable.

## Default flow

```text
1. User submits task.
2. Session controller creates session.
3. Router classifies task.
4. CLI displays pipeline and budget.
5. Context broker builds role-specific context packs.
6. Pipeline executes roles.
7. Budget ledger records model calls.
8. Policy engine validates risky tools.
9. Review gates approve/block.
10. Release agent summarizes.
11. Session closes with cost and outputs.
```

## Runtime boundary

Manta v1 should keep Deep Agents behind an adapter. This gives the team freedom to:

- call Deep Agents SDK directly,
- call Deep Agents Code where useful,
- swap to LangGraph for custom graphs,
- or implement custom role execution if needed.

The Manta product contract should not leak Deep Agents implementation details into every module.
