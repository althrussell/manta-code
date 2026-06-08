# Codex Bootstrap Prompt

Build Manta CLI v1 according to the repository docs.

Primary objective:

Implement a budget-aware multi-model developer CLI with a cheap router, role-based pipeline, context broker, policy engine, and Deep Agents adapter.

Non-negotiables:

- Opus planner is never default.
- Every model call must be budget checked and logged.
- Side-effecting tools must be policy checked.
- Reviewers are read-only.
- Network and git push are denied by default.
- Shell is allowlisted by default.
- Tests must pass.

Work sprint-by-sprint. Use `sprint-plans/backlog.csv` as the execution backlog.
