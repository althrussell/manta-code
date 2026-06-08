# Release Plan

## Pre-alpha

Goal: prove CLI shell, routing, config, dry-run pipeline, budget ledger.

Exit criteria:

- `manta init` works.
- `manta route` works.
- `manta run --dry-run` writes session logs.
- Unit tests pass.

## Alpha 1

Goal: real model-backed cheap router and builder/reviewer loop.

Exit criteria:

- Router calls cheap model and returns structured JSON.
- Builder can edit files through patch tool.
- Reviewer can review diff and block.
- Budget ledger records estimated usage.

## Alpha 2

Goal: scoped roles and context broker.

Exit criteria:

- Planner activates only for complex/risky routes.
- Builder gets selected context only.
- Reviewer gets diff-first context.
- Context manifest is written every run.

## Beta 1

Goal: security and policy maturity.

Exit criteria:

- Shell allowlist enforced.
- Network blocked by default.
- Security reviewer triggers by risk rules.
- Protected paths enforced.
- Evals include security cases.

## v1

Goal: reliable developer CLI for controlled autonomous coding.

Exit criteria:

- 80%+ success rate on v1 eval set.
- Average normal code change stays under configured budget.
- Opus activation rate is explainable and below target threshold.
- No high-risk tool call runs without policy/approval.
- Docs and config schema are stable.
