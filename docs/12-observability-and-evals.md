# Observability and Evals

## Required metrics

### Cost metrics

- total cost per session,
- cost per successful task,
- cost by role,
- cost by model,
- Opus activation rate,
- budget exhaustion rate.

### Quality metrics

- task success rate,
- tests pass rate,
- reviewer block rate,
- reviewer false-positive rate,
- security finding catch rate,
- user intervention count.

### Routing metrics

- route distribution,
- router escalation rate,
- planner usage rate,
- misroute rate,
- forced model override rate.

### Runtime metrics

- wall-clock duration,
- tool calls per task,
- repeated tool call count,
- context token size by role,
- compaction frequency.

## Eval set v1

Create benchmark tasks:

1. Explain simple error.
2. Add type hints to one file.
3. Fix failing test.
4. Add one-file feature with tests.
5. Add multi-file feature.
6. Refactor without behavior change.
7. Security review auth change.
8. Detect secrets leak in diff.
9. Update dependency safely.
10. Generate PR summary.

## Eval result schema

```json
{
  "task_id": "small-bug-fix-001",
  "route": "trivial_code_change",
  "expected_route": "trivial_code_change",
  "success": true,
  "tests_passed": true,
  "review_approved": true,
  "cost_usd": 0.19,
  "opus_used": false,
  "notes": ""
}
```

## Dashboards later

v1 can use JSONL and markdown reports. Later:

- local SQLite,
- HTML report,
- LangSmith traces,
- Databricks table for team-wide evals.
