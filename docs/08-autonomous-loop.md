# Autonomous Loop

## Default loop

```text
classify
  ↓
select budget
  ↓
select context
  ↓
plan if needed
  ↓
build
  ↓
test
  ↓
review
  ↓
fix if needed
  ↓
final review
  ↓
summarize
```

## Pseudocode

```python
route = router.classify(task)
budget.reserve(route.max_budget_usd)
context = broker.prepare(route)

if route.needs_planning:
    plan = planner.run(context.planner_pack)
else:
    plan = minimal_plan_from_task(task)

for iteration in range(max_iterations):
    build = builder.run(plan, context.builder_pack)
    tests = test_agent.run(build.diff)
    review = reviewer.run(build.diff, tests)

    if route.needs_security_review:
        security = security_reviewer.run(build.diff)
        if security.blocked:
            return blocked(security)

    if review.approved and tests.passed:
        return release_agent.run(build, tests, review)

    if not budget.can_continue():
        return partial_completion_report()

    plan = update_plan_with_findings(plan, review, tests)
```

## Stop conditions

Stop immediately when:

- budget exhausted,
- policy blocks tool call,
- high severity security issue found,
- required context exceeds hard token cap,
- protected files need access and user has not approved,
- same tool call repeats too many times,
- tests fail after max attempts,
- reviewer blocks after max fix loops.

## Autonomous modes

### Chat mode

No tool writes. Good for explanation.

### Approve mode

Ask before writes and shell.

### Smart approve mode

Allow safe patch/test operations, ask for risky operations.

### Auto mode

Allow configured safe operations until budget or policy stops the loop.

## v1 recommended default

```text
smart approve
```

Rationale: It gives meaningful autonomy without allowing arbitrary shell/network/git side effects.

## Recovery

Every loop step writes a session event so users can resume or diagnose.

Required event types:

- route_decision,
- budget_check,
- context_manifest,
- role_started,
- model_call,
- tool_requested,
- tool_allowed,
- tool_blocked,
- role_completed,
- review_result,
- stop_condition,
- session_completed.
