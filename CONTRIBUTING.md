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
- test evidence (`pytest` + `ruff check .`),
- docs updates for config/schema or CLI changes,
- a note if the change affects how Manta launches or configures `deepagents-code`.

## Review gates

- At least one reviewer approval for source changes.
- No direct commits to main.
