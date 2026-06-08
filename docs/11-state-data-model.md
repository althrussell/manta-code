# State and Data Model

## State locations

Project-local:

```text
.manta/
  config.toml
  sessions/
  context/
  reports/
  ledger.jsonl
  memory/
```

User-global:

```text
~/.manta/
  config.toml
  price_table.toml
  skills/
  memory/
```

## Session event

```json
{
  "ts": "2026-06-08T12:00:00Z",
  "session_id": "abc123",
  "type": "route_decision",
  "payload": {}
}
```

## Route decision

```json
{
  "intent": "code_change",
  "complexity": "medium",
  "risk": "low",
  "needs_planning": false,
  "needs_review": true,
  "needs_security_review": false,
  "pipeline": ["builder", "code_reviewer"],
  "max_budget_usd": 1.0,
  "reason": "Single feature change with tests"
}
```

## Context manifest

```json
{
  "session_id": "abc123",
  "route": "normal_code_change",
  "selected_files": [],
  "role_token_estimates": {},
  "omitted_context": [],
  "selection_reason": ""
}
```

## Review report

```json
{
  "approved": false,
  "findings": [
    {
      "file": "src/auth.ts",
      "line": 42,
      "severity": "medium",
      "category": "correctness",
      "issue": "Missing expired-token branch.",
      "required_fix": "Add explicit expired-token handling and tests."
    }
  ]
}
```

## Role result

Each role invocation returns a structured result. `usage` is populated by
model-backed runtimes and drives ledger recording; the dry-run mock runtime
leaves it `null`.

```json
{
  "role": "builder",
  "status": "completed",
  "output": {"message": "implemented change"},
  "usage": {"input_tokens": 12000, "output_tokens": 2500},
  "cost": 0.24
}
```

`status` is one of `completed`, `blocked`, `failed`, `skipped`. A `blocked`
reviewer or an exhausted budget stops the remaining pipeline (downstream roles
are recorded as `skipped`).

## Cost ledger

Append-only JSONL.

Each entry:

```json
{
  "ts": "2026-06-08T12:00:00Z",
  "session_id": "abc123",
  "role": "builder",
  "model": "openai:gpt-builder",
  "input_tokens": 12000,
  "output_tokens": 2500,
  "estimated_cost_usd": 0.24,
  "route": "normal_code_change"
}
```

## Why append-only

Append-only logs make it easier to:

- debug failed runs,
- audit cost,
- replay sessions,
- build eval datasets,
- diagnose router mistakes.
