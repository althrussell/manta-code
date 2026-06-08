# Cursor Bootstrap Prompt

You are helping build Manta CLI v1 from the attached repository bootstrap.

Goal: implement the next incomplete story from `sprint-plans/backlog.csv` while preserving the architecture in `docs/`.

Rules:

- Do not bypass the budget engine.
- Do not couple application modules directly to Deep Agents; use the runtime adapter.
- Keep reviewers read-only.
- All shell/network/git behavior must go through policy.
- Update docs when changing config, commands, schemas, or policy.
- Add tests for every new behavior.

Start by reading:

1. `docs/00-index.md`
2. `docs/03-system-architecture.md`
3. `docs/05-routing-and-budgeting.md`
4. current sprint file under `sprint-plans/`

Then propose a short implementation plan before editing.
