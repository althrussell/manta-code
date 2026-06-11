# Config Schema

Manta's config is intentionally small: it configures which Databricks endpoints
the `deepagents-code` TUI launches with and registers. All in-session behavior
(approval, budget, sessions, skills) is owned by `deepagents-code` and its own
config at `~/.deepagents/config.toml`.

## Config locations

Project config:

```text
.manta/config.toml
```

User config:

```text
~/.manta/config.toml
```

Precedence:

```text
CLI flags > project config > user config > defaults
```

## Schema

Endpoint names are **Databricks Model Serving / Foundation Model API endpoint
names**. Manta wires the selected endpoint into `deepagents-code` as the
`databricks` provider (`ChatDatabricks`), authenticated with the active profile.

```toml
[runtime]
provider = "databricks"   # fixed; Manta is Databricks-only

[interactive]
# Orchestrator endpoint `manta` launches with (passed as `databricks:<endpoint>`).
default_endpoint = "databricks-gpt-oss-120b"
# Subagent role models, also registered in the deepagents-code `/model` switcher.
extra_endpoints = [
    "databricks-claude-opus-4-8",
    "databricks-gpt-5-4",
    "databricks-claude-sonnet-4-5",
]
```

`manta init` writes this file with sensible defaults; `manta init --overwrite`
regenerates it.

## Authentication

Manta authenticates to Databricks through the SDK `WorkspaceClient` (unified
auth). Profile selection precedence:

```text
-p/--profile flag > MANTA_PROFILE > DATABRICKS_CONFIG_PROFILE > default profile
```

The resolved profile is exported as `DATABRICKS_CONFIG_PROFILE` before launching
the TUI, so `ChatDatabricks` (via the SDK) picks it up for unified auth from
`~/.databrickscfg`. `manta doctor` validates the profile and the model wiring.

## deepagents-code config (`~/.deepagents/config.toml`)

Manta idempotently merges a Databricks provider into this file (see `dcode.py`):

```toml
[providers.databricks]
class_path = "manta_code.databricks_chat:MantaChatDatabricks"
models = [
    "databricks-gpt-oss-120b",
    "databricks-claude-opus-4-8",
    "databricks-gpt-5-4",
    "databricks-claude-sonnet-4-5",
]
```

Everything else in that file (approval policy, budget, theme, sessions) is owned
by `deepagents-code`; Manta does not manage it.
