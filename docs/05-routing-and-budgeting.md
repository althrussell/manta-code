# Routing and Budgeting

## Design principle

Expensive intelligence is an escalation path, not the default.

```text
cheap router → selected pipeline → budgeted role execution
```

## Route tiers

| Route | Pipeline | Opus allowed | Default budget |
|---|---|---:|---:|
| `simple_answer` | cheap_responder | No | $0.02 |
| `trivial_code_change` | builder | No | $0.25 |
| `normal_code_change` | builder → code_reviewer | No | $1.00 |
| `complex_architecture` | planner → builder → code_reviewer | Yes | $3.00 |
| `security_sensitive` | planner → builder → code_reviewer → security_reviewer | Yes | $5.00 |

## Router signals

The router should inspect:

- user request,
- current directory,
- git status,
- lightweight repo metadata,
- session history summary.

It should not read large source files by default.

## Escalation to planner

Use planner only when:

- user asks for architecture/design/deep plan,
- task spans multiple modules,
- task is ambiguous,
- task impacts auth, infra, data, or security,
- prior builder attempt failed,
- tests fail repeatedly,
- reviewer requests replanning,
- user explicitly forces planning.

## Budget types

Manta tracks:

1. total task dollar budget,
2. per-role dollar budget,
3. per-call input token cap,
4. per-call output token cap,
5. max iterations,
6. max turns,
7. wall-clock timeout.

## Cost ledger event

```json
{
  "ts": "2026-06-08T12:00:00Z",
  "session_id": "abc123",
  "role": "builder",
  "model": "openai:gpt-builder",
  "input_tokens": 18321,
  "output_tokens": 2711,
  "estimated_cost_usd": 0.41,
  "budget_remaining_usd": 0.59,
  "route": "normal_code_change",
  "reason": "implementation call"
}
```

## Budget enforcement

Hard stops:

- task budget exhausted,
- role budget exhausted,
- iteration cap reached,
- max turn cap reached,
- estimated next call exceeds remaining budget and user has not approved escalation.

Soft prompts:

- next call would consume >50% of remaining budget,
- planner escalation proposed,
- security review proposed above route default budget.

## UX rules

Before autonomy:

```text
Route: normal_code_change
Pipeline: builder → code_reviewer
Opus: skipped
Budget: $1.00
Estimated first context: 24k tokens
```

During autonomy:

```text
builder      $0.31 used
reviewer     $0.18 used
remaining    $0.51
```

After autonomy:

```text
Total: $0.64 / $1.00
Opus tokens: 0
Pipeline: builder → reviewer → builder_fix → reviewer_final
```
