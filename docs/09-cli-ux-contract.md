# CLI UX Contract

Manta is a thin, Databricks-preconfigured launcher for the `deepagents-code`
interactive coding agent (ADR 0007). The CLI surface is intentionally small:
launch the TUI, plus two housekeeping subcommands.

## Primary surface: `manta` (interactive)

Running `manta` with no subcommand launches the **interactive coding session**
(like Claude Code) — the upstream `deepagents-code` TUI, preconfigured for
Databricks. Manta resolves your profile, provisions a Databricks model provider
in `~/.deepagents/config.toml`, and execs the TUI with
`-M databricks:<default_endpoint>`.

```text
$ manta -p my-profile
# → launches the deepagents-code TUI, authenticated via the my-profile
#   profile, default model databricks:databricks-claude-sonnet-4-5
```

Flags:

- `-p/--profile <name>` — Databricks profile (also `MANTA_PROFILE` /
  `DATABRICKS_CONFIG_PROFILE`). Maps to `DATABRICKS_CONFIG_PROFILE` for the SDK.
- Any flag Manta does not define is **forwarded** to `deepagents-code`, e.g.
  `manta -r` (resume), `manta -a <agent>`, `manta --skill <name>`,
  `manta -M databricks:<other-endpoint>` (overrides the injected default).

In-session UX — model switcher (`/model`), approval/HITL, sessions/threads,
skills, themes — is provided entirely by `deepagents-code`. Manta does not wrap
or intercept it.

## Subcommands

```bash
manta doctor   # check deps, Databricks auth, and model wiring
manta init     # write .manta/config.toml (launcher endpoints)
```

### `manta doctor`

Offline preflight (no model calls beyond a model-wiring instantiation check):

```text
Manta Code 0.1.0
                            Preflight
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Check                ┃ OK  ┃ Detail                          ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ deepagents-code      │ yes │ interactive runtime             │
│ databricks-langchain │ yes │                                 │
│ dcode config         │ yes │ ~/.deepagents/config.toml       │
│ model wiring         │ yes │ ChatDatabricks                  │
│ databricks auth      │ yes │ you@example.com                 │
└──────────────────────┴─────┴─────────────────────────────────┘
Status: OK
```

### `manta init`

Writes `.manta/config.toml` with the launcher settings (default endpoint and the
extra endpoints to register in the `deepagents-code` `/model` switcher). See
`docs/10-config-schema.md`.

## Error style

Errors are actionable and tell you the next step:

```text
The interactive runtime (deepagents-code) is not installed.
Install it with: pip install -e '.[agent]'
```
