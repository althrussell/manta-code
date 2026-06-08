# Manta CLI v1 Bootstrap

Manta CLI is a budget-aware, multi-model autonomous developer CLI.

The product promise:

> Best model. Best job. Best output. No token bill shock.

Manta is not intended to be a thin chat wrapper. It is an engineering control plane that routes work through the right model and the right specialist role, under an explicit budget.

## v1 positioning

Manta v1 is repo-first:

- Cheap intent router before expensive model calls.
- Opus-style planning only when complexity or risk justifies it.
- GPT-style builder for implementation.
- Gemini-style reviewer for independent review.
- Optional security reviewer for risky changes.
- Token and cost ledger visible before, during, and after execution.
- Scoped skills, scoped permissions, and scoped context per role.

## Included in this bootstrap pack

```text
.
├── docs/                    # product, architecture, security, config, evals
├── docs/adr/                # architecture decision records
├── sprint-plans/            # sprint-by-sprint execution plan and backlog
├── src/manta_cli/           # minimal Python CLI scaffold
├── tests/                   # initial unit tests
├── configs/                 # example Manta config, model prices, policies
├── prompts/                 # role system prompts and bootstrap prompts
├── skills/                  # initial Deep Agents-style SKILL.md packs
├── examples/                # route/review/context examples
├── scripts/                 # bootstrap and local run helpers
└── .github/                 # CI and issue templates
```

## Quick start

```bash
cd manta-cli-v1-bootstrap
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
manta doctor
manta init
manta route "add a settings page and tests"
manta run "add a settings page and tests" --dry-run --max-usd 1
pytest
```

The scaffold runs in dry-run/heuristic mode without model credentials. The Deep Agents integration is intentionally isolated behind an adapter so the team can wire the current SDK cleanly during Sprint 3.

## Read first

1. `docs/00-index.md`
2. `docs/01-product-requirements.md`
3. `docs/03-system-architecture.md`
4. `docs/05-routing-and-budgeting.md`
5. `docs/08-autonomous-loop.md`
6. `sprint-plans/00-roadmap-overview.md`

## Design constraints

- Opus is never the default.
- Every session starts with a cheap route decision unless the user forces a pipeline.
- Every model call records tokens, cost, role, and reason.
- Reviewers are read-only by default.
- Security review is risk-triggered and blocks high severity findings.
- Shell is allowlisted by default.
- Git push is denied by default.
- Network access is denied unless explicitly enabled.

## Target v1 commands

```bash
manta ask "explain this error"
manta route "build this feature"
manta plan "add OAuth login"
manta run "build this feature" --auto --max-usd 3
manta review
manta security-review
manta budget
manta status
manta resume
manta diff
manta approve
manta models
manta skills
manta doctor
```

## Implementation note

This repo is a bootstrap scaffold, not a completed agent product. It gives the team:

- the architecture,
- the docs,
- the config contract,
- the policy model,
- the sprint plan,
- the role prompts,
- the starter CLI,
- and the first tests.

The main remaining implementation work is wiring real model calls and Deep Agents runtime execution behind the interfaces provided in `src/manta_cli/agents/deepagents_adapter.py`.
