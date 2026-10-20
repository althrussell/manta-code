# CLI UX Contract

Manta launches the upstream `deepagents-code` TUI and adds its own agent,
policy, budget, usage, and task control plane through runtime hooks
([ADR 0008](adr/0008-owned-agent-control-plane.md)). The terminal UI, approval
interaction, skills, and session storage come from upstream; Manta also adds
commands such as `/cost`, `/auth`, and its agent-switching behavior.

## Primary surface: `manta` (interactive)

Running `manta` with no subcommand launches the interactive coding session.
When Databricks is configured, Manta resolves your profile, provisions a model
provider in `~/.deepagents/config.toml`, and supplies
`-M databricks:<default_endpoint>` unless you passed a model explicitly or are
resuming a saved thread. Agent role pins apply when an agent is selected.
Explicit CLI selections for other configured providers remain supported.

```bash
manta -p my-profile
manta -M databricks:system.ai.gpt-6-1-sol
```

Flags:

- `-p/--profile <name>` selects the Databricks profile, ahead of `MANTA_PROFILE`
  and `DATABRICKS_CONFIG_PROFILE`.
- Unknown flags are forwarded to `deepagents-code`, including `-r` (resume),
  `--skill <name>`, and `-M <provider:model>` (explicit model selection).

The `databricks:` provider accepts legacy endpoint names and exact three-part
Unity model-service names. The adapter selects the inference route from the
name, preserving the exact identifier. Services prefer unified Responses unless
known compatibility or metadata selects Chat Completions. Legacy endpoints
retain their legacy Chat route. See [configuration](10-config-schema.md) for
per-target options and explicit native OpenAI routing. The `/model` inventory
includes configured targets, agent pins, discovered Unity services, and legacy
chat endpoints for the active workspace. All appear by default; no recommended
filter or direct-provider API key is required to find Unity services. Search and
selection stay within the Databricks inventory. Embedding-only services are
excluded. Workspace changes refresh
Manta-owned registrations while preserving user entries and defaults.

Saved conversations retain their selected dialect. A model or API switch that
would replay opaque reasoning or tool state incompatibly fails with guidance to
continue with the original selection or start a new thread.

## Subcommands

Common commands include:

```bash
manta doctor          # dependencies, runtime hooks, model wiring, Databricks auth
manta doctor --probe  # opt-in inference against pinned models
manta init            # write .manta/config.toml
manta run "<task>"    # bounded headless execution
manta agents list     # registry, model pins, and enforced boundaries
manta status          # background tasks and spend
manta cost            # local usage ledger
manta receipts        # estimated savings and advice results
manta gateway         # Unity service and legacy endpoint gateway metadata
manta gateway --target system.ai.gpt-6-1-sol
```

Use `manta --help` and each subcommand's `--help` for the complete surface.
The [README command reference](../README.md) describes the normal workflows.

### `manta doctor`

Doctor checks `deepagents-code`, `langchain-openai`, upstream provider config,
`MantaChatDatabricks` construction, and Manta's runtime patch targets. When
Databricks is configured it checks SDK credentials and validates
agent pins against independent Unity and legacy inventories. These checks can
make network requests. Doctor does not perform inference unless `--probe` is
supplied, and constructing the model itself does not require credentials.

Metadata checks distinguish denied, unavailable, unsupported, partial, and
complete results. A configured service remains usable when metadata access is
denied; metadata visibility does not establish EXECUTE permission. Doctor also
reports an existing upstream legacy default without rewriting user-owned state.
`--probe` uses the selected target policy for a two-request tool roundtrip and
records usage. It does not retry through a different model, API, or provider on
failure. See [qualification results](15-unity-gateway-qualification.md) for the
tested services and current gateway limitations.

### `manta init`

Writes `.manta/config.toml` with the launch target and additional targets to
register in the model picker. Existing files are preserved unless
`--overwrite` is supplied. See [the config schema](10-config-schema.md).

### Headless machine-readable output

`manta run "<task>" --json text` emits one JSON result with `schema_version=1`,
`type="result"`, `success`, `exit_code`, `stdout`, and `stderr`.
`--json stream-json` emits JSONL `stdout`/`stderr` events with `text`, followed by
a result event with `success` and `exit_code`. Each channel preserves its order;
ordering across channels is unspecified. These are runtime text events.

Add `--stream` for token streaming; upstream output is buffered by default.
The CLI preserves the child exit code, including timeout 124; a subprocess start
failure emits 127.

```bash
manta run "explain this project" --json text
manta run "explain this project" --json stream-json --stream
```

## Error style

Errors identify the failed operation and a concrete next step. For example:

```text
The interactive runtime (deepagents-code) is not installed.
Install it with: uv tool install --python 3.12 --force --upgrade --constraints "https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock" "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
```

The runtime requires Python 3.12+. Runtime hook failures produce a visible
warning when Manta falls back to upstream behavior.

The recovery command uses the running Manta release's Git tag and matching
dependency constraints. It works outside a source checkout; repeat the current
release's installation command to upgrade from an earlier tag.
