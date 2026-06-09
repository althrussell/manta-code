# Manta Code Docs Index

Manta Code is a thin, Databricks-preconfigured launcher for the
[`deepagents-code`](https://pypi.org/project/deepagents-code/) interactive coding
agent. Type `manta` and you get the `deepagents-code` TUI, wired to Databricks
Model Serving / Foundation Model API endpoints and authenticated via your
Databricks profile. See [ADR 0007](adr/0007-adopt-deepagents-code-tui.md).

## Docs

- `09-cli-ux-contract.md` — the `manta` command surface (launch, `doctor`, `init`).
- `10-config-schema.md` — `.manta/config.toml` and the deepagents-code provider config.
- `13-deepagents-integration.md` — how Manta wires Databricks into deepagents-code.

## Architecture decision records

See `docs/adr/`. ADRs 0001 and 0003–0006 describe an earlier multi-agent /
router / budget / policy architecture and are **superseded by ADR 0007**; they
are retained as historical context. ADR 0002 (do not fork Goose) still holds.

## Source

See `src/manta_code/`:

- `main.py` — CLI entry (`manta`, `doctor`, `init`) and argument routing.
- `dcode.py` — provisions the Databricks provider and launches the TUI.
- `auth.py` — Databricks profile resolution (SDK unified auth).
- `config.py` — launcher config schema and `.manta/config.toml` handling.
