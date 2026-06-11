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
      "databricks-gpt-5-4",
      "databricks-claude-sonnet-4-5",
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

### Registry-backed agents — two tiers from one source of truth

`deepagents-code` exposes agents in **two** tiers:

1. **Profiles** — a directory `~/.deepagents/<name>/AGENTS.md`. These are the
   entries the in-app `/agents` picker lists; selecting one restarts the session
   with that agent as the *primary* loop.
2. **Subagents** — delegation targets the orchestrator dispatches to via the
   `task` tool. Planning itself is the built-in `write_todos` tool on the main
   agent — not a separate agent type.

Manta's registry (`~/.manta/agents/`, the `AgentDef` schema in
`agents/registry.py`) is the single source of truth for **both** tiers. The
built-in plan → build → review division of labour ships out of the box, each
pinned to the right Databricks model for its role:

| Role | Agent | Model field | Notes |
| --- | --- | --- | --- |
| Orchestrator (main) | — | `databricks:databricks-gpt-oss-120b` | the `default_endpoint`; fast MoE, strong tool use |
| Planning | `planning` | `databricks:databricks-claude-opus-4-8` | turns a request into an ordered `write_todos` plan; read-only |
| Build / SWE | `swe` | `databricks:databricks-gpt-5-4` | implements changes hands-on (edit files, run tests via `execute`) |
| Review | `review` | `databricks:databricks-claude-sonnet-4-5` | read-only review that reports findings; a different vendor catches what the author model misses |

**Profile tier (`agents/profiles.py`).** `sync_agent_profiles()` projects every
registry agent (built-ins + user-created) into a top-level
`~/.deepagents/<name>/AGENTS.md` so it appears in the picker. Profiles are
**generated artifacts**: refreshed from the registry on each `launch()` /
`run_headless()` (you edit agents with `manta agents edit`, not by hand). A
`<!-- managed-by: manta-agents … -->` sentinel marks Manta-generated files, so a
refresh never clobbers a user-authored profile of the same name, never touches
the base `agent` profile, and prunes profiles for agents you've deleted. The
legacy prompt-only markdown subagents this replaced are removed once, on first
launch, by `clean_legacy_subagents()` (only when unmodified). Run
`manta agents sync` to regenerate on demand.

**Subagent tier (build hook).** `enrich_kwargs()` compiles each registry agent
into an *enforced* `deepagents` `SubAgent` dict (tool policy, filesystem rules,
memory, budget) and injects them so the orchestrator can delegate to them.

**Plan requests are auto-delegated (deterministically).** deepagents gives the
orchestrator its own `write_todos` planning tool and its default prompt prefers
planning inline, so *no* orchestrator model reliably delegates "make a plan" to
the `planning` agent on its own — a prompt nudge can't beat a built-in tool.
`middleware/delegation.py` closes the gap without relying on model compliance:
on the base orchestrator (no profile selected), when a fresh human turn is a
plan request (`plan_intent()`), `PlanDelegationMiddleware` short-circuits the
model call and returns a synthesized `task(subagent_type="planning", …)` call,
so the real planning agent runs. It fires only on the first model call of the
turn (the last message is the human plan request), so after the `task` result
comes back it never re-fires — no loop. The intent match is conservative (it
skips "implement the plan…" and references to an existing plan), it's the
outermost orchestrator middleware (no accounting noise for a call that never
runs), and it's fully guarded. Disable with `MANTA_AUTODELEGATE_PLANNING=0` to
fall back to inline planning. Selecting a profile in the picker bypasses this
entirely (if you pick `planning`, it already *is* the planner).

**Top-level enforcement.** When you select a Manta agent in the picker,
`deepagents-code` passes its name to the server as
`DEEPAGENTS_CODE_SERVER_ASSISTANT_ID`. The build hook's `active_agent_name()`
reads it and applies that agent's tool-policy + memory + economy middleware to
the *primary* loop — so picking `review` makes the whole session read-only, not
just a delegated subagent. Economy is attributed to the active profile (with its
budget caps) instead of a separate `orchestrator` instance, so a call is never
double-counted in the ledger.

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

`review`'s read-only contract is **really enforced**, not prompt-only: the build
hook compiles it as a Python `SubAgent` with Manta's `ToolPolicyMiddleware`,
which blocks `execute` and all filesystem writes in `wrap_tool_call`. Selecting
`review` as the top-level profile applies the same policy to the primary loop.

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

## Headless / CI / SDK path (`manta run`)

`manta run "<task>"` wraps `deepagents-code`'s non-interactive one-shot mode
(`-n`) with CI-safe defaults: a **bounded `--timeout`** (default 600s), a
`--max-turns` cap (default 50), and quiet/buffered output for clean piping
(`--json text|stream-json` for machine output). The same config/onboarding/
profile sync as `launch` runs first, so Manta's control plane (enforced
agents, token economy + usage ledger) applies headlessly too. `manta run` returns
the runtime's exit code (124 on timeout) instead of replacing the process, so
scripts and CI can branch on it.

`manta watch` gives a live per-agent token/cost view by tailing the local usage
ledger — steering leverage (see where spend is going across parallel agents and
subagents) without forking the upstream TUI.

## Known gaps

- `deepagents-code -n` (non-interactive one-shot) can hang on startup in some
  environments. **Mitigation:** `manta run` always passes a bounded `--timeout`
  (default 600s), so a hang fails fast with exit code 124 rather than blocking a
  scripted/CI run indefinitely; tune via `manta run --timeout`. The interactive
  TUI remains the primary surface.
- `deepagents-code` pulls `protobuf` 6.x, which conflicts with
  `databricks-vectorsearch` (not a Manta dependency). Resolve at the environment
  level only if both are needed together.
