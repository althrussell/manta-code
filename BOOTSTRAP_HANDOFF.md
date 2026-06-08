# Bootstrap Handoff

## What this zip contains

This zip is intended to be unpacked as a repo and used immediately by a small engineering team.

It includes:

- a minimal runnable CLI scaffold,
- product and architecture specs,
- sprint plans,
- backlog,
- role prompts,
- skill packs,
- config examples,
- tests,
- CI workflow.

## First 3 commands

```bash
pip install -e '.[dev]'
manta doctor
pytest
```

## First implementation target

Start with Sprint 1:

```text
sprint-plans/sprint-01-core-cli.md
```

The current scaffold already does part of Sprint 1 in dry-run mode. Tighten it, then move to Sprint 2 budget router.

## Where to wire real models

```text
src/manta_cli/agents/deepagents_adapter.py
```

Do not spread provider-specific code around the repo.

## Product rule to protect

Never let Manta become “expensive agent by default.” The cheap router and budget ledger are the product spine.
