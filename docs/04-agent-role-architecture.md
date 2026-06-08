# Agent Role Architecture

## Role principle

Build role agents, not vendor agents.

```text
Role = purpose + model + tools + permissions + context + budget + output schema
```

Bad:

```text
Claude Agent
GPT Agent
Gemini Agent
```

Good:

```text
Planner Agent     → Opus-class model
Builder Agent     → GPT/Codex-class model
Reviewer Agent    → Gemini-class model
Security Reviewer → Gemini or Claude-class model
```

## Default roles

| Role | Purpose | Default model class | Write access | Shell access |
|---|---|---:|---:|---:|
| Router | Classify task and budget | cheap fast | No | No |
| Cheap Responder | Simple Q&A | cheap fast | No | No |
| Planner | Architecture and acceptance criteria | Opus | Plan artifacts only | No |
| Builder | Implement changes | GPT/Codex | Patch only | Allowlisted |
| Test Agent | Run/summarize tests | GPT/cheap | Reports only | Allowlisted tests |
| Code Reviewer | Correctness review | Gemini | Reports only | No |
| Security Reviewer | Security review | Gemini/Claude | Reports only | Allowlisted scanners |
| Release Agent | Summary/commit/PR | cheap fast | Commit message/PR body | No |

## Router output schema

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

## Planner contract

Inputs:

- user request,
- repo map,
- project docs,
- relevant files,
- previous decisions.

Outputs:

- `task_plan.md`,
- `acceptance_criteria.md`,
- `context_manifest.json`,
- `risk_notes.md`.

Rules:

- Planner must not edit product code.
- Planner must select context for builder.
- Planner must identify risk triggers.

## Builder contract

Inputs:

- task plan or router route,
- acceptance criteria,
- selected files,
- coding standards,
- test expectations.

Outputs:

- patch/diff,
- implementation notes,
- test logs,
- unresolved issues.

Rules:

- Builder edits through patch tool only.
- Builder must not push git.
- Builder must not access network unless approved.
- Builder should stop rather than broaden context without budget approval.

## Reviewer contract

Inputs:

- final diff,
- selected file context,
- acceptance criteria,
- test output,
- coding standards.

Outputs:

- review report,
- structured required fixes,
- approve/block decision.

Rules:

- Reviewer is read-only.
- Reviewer should return concise, actionable findings.
- Reviewer must classify severity.

## Security reviewer contract

Triggered by:

- auth/security files touched,
- `.env`, secrets, config, CI, dependency files touched,
- network/client code added,
- database migrations,
- data export/import,
- shell scripts,
- user explicitly asks.

Outputs:

- security findings,
- required fixes,
- approve/block decision.

High severity blocks finalization.

## Release agent contract

Outputs:

- change summary,
- commit message,
- PR body,
- migration notes if applicable.

Release agent does not push.
