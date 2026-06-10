# Manta Code

Manta Code is a **Databricks-preconfigured launcher** for the
[`deepagents-code`](https://pypi.org/project/deepagents-code/) interactive coding
agent. Run `manta` and you get the `deepagents-code` TUI (like Claude Code),
wired to Databricks Model Serving / Foundation Model API endpoints and
authenticated with your Databricks profile.

Manta stays deliberately thin: it does not fork or wrap the agent. It registers a
Databricks model provider, resolves your profile, and hands the terminal over to
the TUI. See [ADR 0007](docs/adr/0007-adopt-deepagents-code-tui.md).

> Where this is going: a token-optimal, multi-model, multi-provider coding agent
> for work **on and off Databricks**, with addressable `@{agent}` long-running
> tasks coordinated by a chief-of-staff agent. See **[VISION.md](VISION.md)**.

## Install

Manta is not published to PyPI; install it from source.

```bash
# Use directly from GitHub
pip install 'manta-code[agent] @ git+https://github.com/althrussell/manta-code.git'
```

## Quick start

```bash
# Or clone for local development
git clone https://github.com/althrussell/manta-code.git
cd manta-code
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,agent]'

manta doctor             # check deepagents-code + Databricks model wiring/auth
manta                    # launch the interactive coding session (default profile)
manta -p my-profile      # launch against a specific Databricks profile
manta -r                 # resume most recent thread (deepagents-code flag, forwarded)
```

## How it works

`manta` (no subcommand) launches the `deepagents-code` TUI, preconfigured for
Databricks:

- **Model** — Manta registers a Databricks provider in `~/.deepagents/config.toml`
  (`class_path = "manta_code.databricks_chat:MantaChatDatabricks"`, a thin
  `ChatDatabricks` subclass that unpacks reasoning-model content) and launches
  with `-M databricks:<default_endpoint>` from your `.manta/config.toml`.
- **Auth** — `-p/--profile` (or `MANTA_PROFILE` / `DATABRICKS_CONFIG_PROFILE`)
  selects the `~/.databrickscfg` profile; the Databricks SDK handles unified auth.
- **Agents** — Manta ships three **enforced** built-in agents (`planning`,
  `swe`, `review`), each pinned to the right Databricks model for its role:

  | Role | Agent | Databricks endpoint |
  | --- | --- | --- |
  | Orchestrator (main) | — | `databricks-gpt-oss-120b` |
  | Planning | `planning` | `databricks-claude-opus-4-8` |
  | Build / SWE | `swe` | `databricks-gpt-5-5` |
  | Review | `review` | `databricks-gemini-3-1-pro` |

  Agents are defined in your registry (`~/.manta/agents/`) — the single source of
  truth — and you can create, edit, and delete your own (`manta agents create`).
  Each appears **two** ways: as a selectable top-level profile in the in-app
  `/agents` picker (selecting one — e.g. `review` — enforces its read-only / tool
  / memory / budget boundaries on the whole session), and as a delegation target
  the main agent calls via its `task` tool. Boundaries are *enforced*, not just
  prompted. Inspect with `manta agents` (table) or `manta agents show <name>`.
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
manta agents        # list agents (built-in + yours) + their models
manta agents show swe  # show one agent's full config + enforced boundaries
manta agents sync   # regenerate the in-app /agents profiles from the registry
```

## Configuration

`manta init` writes `.manta/config.toml`:

```toml
[runtime]
provider = "databricks"

[interactive]
default_endpoint = "databricks-gpt-oss-120b"
extra_endpoints = [
    "databricks-claude-opus-4-8",
    "databricks-gpt-5-5",
    "databricks-gemini-3-1-pro",
]
```

`default_endpoint` is the orchestrator model launched on start; `extra_endpoints`
registers the agent role models so all of them appear in the `deepagents-code`
`/model` switcher. See `docs/10-config-schema.md`.

## Layout

```text
.
├── docs/                 # CLI contract, config schema, integration, ADRs
├── src/manta_code/       # main.py (CLI), dcode.py (launcher), auth.py, config.py,
│                         #   databricks_chat.py (model), hook.py, agents/, _boot.py
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
