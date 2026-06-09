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
  class_path = "databricks_langchain:ChatDatabricks"
  models = ["databricks-claude-sonnet-4-5", "databricks-meta-llama-3-3-70b-instruct"]
  ```

  All other user settings in that file are preserved. `deepagents-code`'s
  `create_model("databricks:<endpoint>")` then instantiates `ChatDatabricks`
  directly — **no fork of its model registry**.
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
