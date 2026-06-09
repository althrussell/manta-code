# Manta Code

Manta Code is a **Databricks-preconfigured launcher** for the
[`deepagents-code`](https://pypi.org/project/deepagents-code/) interactive coding
agent. Run `manta` and you get the `deepagents-code` TUI (like Claude Code),
wired to Databricks Model Serving / Foundation Model API endpoints and
authenticated with your Databricks profile.

Manta stays deliberately thin: it does not fork or wrap the agent. It registers a
Databricks model provider, resolves your profile, and hands the terminal over to
the TUI. See [ADR 0007](docs/adr/0007-adopt-deepagents-code-tui.md).

## Quick start

```bash
cd manta-code
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,agent]'

manta doctor             # check deepagents-code + Databricks model wiring/auth
manta                    # launch the interactive coding session (default profile)
manta -p e2-demo-west    # launch against a specific Databricks profile
manta -r                 # resume most recent thread (deepagents-code flag, forwarded)
```

## How it works

`manta` (no subcommand) launches the `deepagents-code` TUI, preconfigured for
Databricks:

- **Model** — Manta registers a Databricks provider in `~/.deepagents/config.toml`
  (`class_path = "databricks_langchain:ChatDatabricks"`) and launches with
  `-M databricks:<default_endpoint>` from your `.manta/config.toml`.
- **Auth** — `-p/--profile` (or `MANTA_PROFILE` / `DATABRICKS_CONFIG_PROFILE`)
  selects the `~/.databrickscfg` profile; the Databricks SDK handles unified auth.
- **Passthrough** — flags Manta doesn't define (`-r`, `-a <agent>`,
  `--skill <name>`, `-M databricks:<other-endpoint>`, …) are forwarded verbatim to
  `deepagents-code`.

In-session UX — model switcher, approval/HITL, sessions, skills, budget — is
provided entirely by `deepagents-code`.

## Commands

```bash
manta            # launch the interactive TUI
manta -p <name>  # launch with a specific Databricks profile
manta doctor     # preflight: deps, Databricks auth, model wiring
manta init       # write .manta/config.toml (launcher endpoints)
```

## Configuration

`manta init` writes `.manta/config.toml`:

```toml
[runtime]
provider = "databricks"

[interactive]
default_endpoint = "databricks-claude-sonnet-4-5"
extra_endpoints = ["databricks-meta-llama-3-3-70b-instruct"]
```

`default_endpoint` is launched on start; every endpoint is registered in the
`deepagents-code` `/model` switcher. See `docs/10-config-schema.md`.

## Layout

```text
.
├── docs/                 # CLI contract, config schema, integration, ADRs
├── src/manta_code/       # main.py (CLI), dcode.py (launcher), auth.py, config.py
├── tests/                # unit tests
├── configs/              # example .manta/config.toml
├── scripts/              # bootstrap and local-run helpers
└── .github/              # CI and issue templates
```

## Read first

1. `docs/00-index.md`
2. `docs/09-cli-ux-contract.md`
3. `docs/10-config-schema.md`
4. `docs/13-deepagents-integration.md`
5. `docs/adr/0007-adopt-deepagents-code-tui.md`

## Project history

Manta began as a budget-aware, multi-model role pipeline (router / planner /
builder / reviewers, budget engine, policy engine). That architecture is recorded
in ADRs 0001 and 0003–0006 and is **superseded by ADR 0007** — Manta now defers
orchestration to `deepagents-code`. The earlier modules have been removed from the
active codebase; the decision records remain as history.
