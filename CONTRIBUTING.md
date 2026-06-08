# Contributing

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Branch naming

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
```

## Pull request expectations

Every PR should include:

- a clear summary,
- test evidence,
- budget/cost impact if model routing changes,
- security impact if tools, shell, network, or permissions change,
- docs updates for config/schema changes.

## Review gates

- Code reviewer approval required for source changes.
- Security reviewer approval required for policy, shell, secrets, network, MCP, sandbox, or dependency changes.
- No direct commits to main.
