# deepagents-code Integration

As of [ADR 0007](adr/0007-adopt-deepagents-code-tui.md), Manta's interactive
surface **is** the upstream
[`deepagents-code`](https://pypi.org/project/deepagents-code/) TUI. Manta does
not build, fork, or wrap the agent — it provisions a Databricks model provider,
resolves the Databricks profile, and launches the TUI. This keeps Manta tiny and
lets it inherit `deepagents-code`'s approval/HITL, budget, sessions, skills, and
model switcher for free.

## How it wires together

Everything lives in two modules:

### `src/manta_code/dcode.py` — the launcher

- `ensure_dcode_config(endpoints)` idempotently merges a `databricks` provider
  into `~/.deepagents/config.toml`:

  ```toml
  [providers.databricks]
  class_path = "manta_code.databricks_chat:MantaChatDatabricks"
  models = [
      "databricks-gpt-oss-120b",
      "databricks-claude-opus-4-8",
      "databricks-gpt-5-5",
      "databricks-gemini-3-1-pro",
  ]
  ```

  All other user settings in that file are preserved. `deepagents-code`'s
  `create_model("databricks:<endpoint>")` then instantiates
  `MantaChatDatabricks` directly — **no fork of its model registry**.
- `src/manta_code/databricks_chat.py` — `MantaChatDatabricks` is a thin
  `ChatDatabricks` subclass. Some reasoning endpoints (e.g. Qwen "thinking"
  models) return the assistant turn as a serialized list of `reasoning` + `text`
  content blocks; stock `ChatDatabricks` `json.dumps`-es that into the message,
  so the TUI renders raw JSON. The subclass drops the private `reasoning` blocks
  and keeps the visible `text` (streamed and non-streamed). It runs inside the
  LangGraph server subprocess, so the agent's own message history is clean too.
- `build_launch_env(profile)` sets `DATABRICKS_CONFIG_PROFILE` so the Databricks
  SDK (and therefore `ChatDatabricks`) authenticates against the right
  `~/.databrickscfg` profile. This is how `-p/--profile` reaches the model.
- `build_dcode_argv(default_endpoint, passthrough)` builds
  `python -m deepagents_code -M databricks:<endpoint> <passthrough…>` (the
  default `-M` is only injected if the user didn't pass their own).
- `launch(...)` provisions config + env and `os.execvpe`s into the TUI so the
  terminal is handed over cleanly.

The pure helpers (config merge, env build, argv build) are unit-tested in
`tests/test_dcode.py` without launching a subprocess; only `launch()` has side
effects.

### `src/manta_code/subagents.py` — planning / SWE / review agents

`deepagents-code` already implements multi-agent delegation: the main agent gets
a `task` tool and dispatches to child agents defined as markdown files under
`~/.deepagents/<agent>/agents/<name>/AGENTS.md` (YAML frontmatter +
system-prompt body). Planning itself is the built-in `write_todos` tool on the
main agent — not a separate agent type.

`ensure_manta_subagents()` provisions three opinionated defaults so `manta` has
a plan → build → review division of labour out of the box, each pinned to the
right Databricks model for its role:

| Role | Agent | Model field | Notes |
| --- | --- | --- | --- |
| Orchestrator (main) | — | `databricks:databricks-gpt-oss-120b` | the `default_endpoint`; fast MoE, strong tool use |
| Planning | `planning` | `databricks:databricks-claude-opus-4-8` | turns a request into an ordered `write_todos` plan; read-only |
| Build / SWE | `swe` | `databricks:databricks-gpt-5-5` | implements changes hands-on (edit files, run tests via `execute`) |
| Review | `review` | `databricks:databricks-gemini-3-1-pro` | read-only review that reports findings; a different vendor catches what the author model misses |

Each subagent's `SubagentSpec.model` becomes a `model:` line in the rendered
AGENTS.md frontmatter (`provider:endpoint` format), which `deepagents-code`'s
`_parse_subagent_file` reads to run that subagent on its pinned endpoint.
Provisioning is **once, then hands-off** — a
`~/.deepagents/.state/manta_subagents_provisioned` marker means later user edits
and deletions are never clobbered, and a pre-existing file of the same name is
left untouched. `launch()` calls it alongside `ensure_dcode_config()`.

**Why a resolver shim is needed.** The main agent's `databricks:<endpoint>`
model works because `deepagents-code`'s `create_model` honors the provider
`class_path`. Subagent models take a different path: `deepagents` resolves a
subagent's `model` string via `deepagents._models.resolve_model` → langchain's
`init_chat_model`, which has **no `databricks` provider** — so a pinned
`databricks:<endpoint>` would fail with *"Unable to infer model provider"*.
Importing `manta_code.databricks_chat` installs
`_install_subagent_databricks_resolver()`, which wraps `resolve_model` so
`databricks:` specs instantiate `MantaChatDatabricks(model=<endpoint>)` directly
(mirroring `_create_model_from_class`) and everything else defers to the
original resolver. The server subprocess imports this module to build the main
agent's `class_path` model, so the shim is in place before any subagent is
resolved.

The patch is applied in **two** places. `create_deep_agent` resolves subagent
models through `deepagents.graph`, which does a *top-level*
`from deepagents._models import resolve_model` and is imported (via
`create_cli_agent`'s import chain) **before** `create_model` runs the shim — so
rebinding only `deepagents._models.resolve_model` would miss it. The shim
therefore both patches the `_models` attribute (picked up by the function-local
importers in `middleware.subagents`/`summarization`/`rubric` and any
later-imported module) and rebinds the name on every already-imported
`deepagents` module that still points at the original (`_rebind_imported_resolvers`).

`review`'s read-only contract is **prompt-enforced**: markdown subagents can set
`name`/`description`/`model` but not `tools`/`permissions`, so hard tool
sandboxing would require the Python `SubAgent(permissions=...)` path (a future
launcher extension), not markdown.

### `src/manta_code/main.py` — argument routing

`main_entry()` + `classify_args()` route Manta's own subcommands (`doctor`,
`init`) through Typer and send bare invocations and unknown/forwarded flags
(`-r`, `--skill`, `-M …`) straight to the launcher.

## Authentication

`src/manta_code/auth.py` resolves the active profile (`-p/--profile` > env >
default) using the Databricks SDK's unified auth. Manta does not re-implement
auth; it only resolves the profile name and exports it as
`DATABRICKS_CONFIG_PROFILE` before exec.

## Scope: defer to upstream

Per ADR 0007, Manta defers in-session orchestration to `deepagents-code`: its
native approval/HITL and budget UX apply. Manta's earlier router / budget
hard-caps / policy engine are removed from the active codebase; if they are ever
needed, they would be re-introduced as `deepagents-code` runtime middleware
rather than a parallel pipeline.

## Known gaps

- `deepagents-code -n` (non-interactive one-shot) can hang on startup in some
  environments; the interactive TUI is the supported surface.
- `deepagents-code` pulls `protobuf` 6.x, which conflicts with
  `databricks-vectorsearch` (not a Manta dependency). Resolve at the environment
  level only if both are needed together.
