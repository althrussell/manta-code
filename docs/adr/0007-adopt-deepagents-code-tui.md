# ADR 0007 — Adopt deepagents-code as the interactive surface

## Status

Accepted. Supersedes the *implementation* of ADR 0006's interactive runtime
(the hand-built REPL); ADR 0006's intent — a Claude-Code-style interactive
coding agent on Databricks — stands and is now delivered by adopting upstream.

## Context

ADR 0006 built Manta's own streaming REPL (`interactive.py`) on top of a
hand-assembled Deep Agents agent (`agent_builder.py`, `models.py`). While
investigating whether to instead adopt LangChain's published CLI, we found:

- The `deepagents` CLI described in the launch blog was split. `deepagents-cli`
  is now **deploy tooling only**; the interactive agent moved to a separate
  package, **`deepagents-code`**.
- `deepagents-code` is a mature, full-featured Textual TUI (~1.5 MB): streaming,
  human-in-the-loop approval, sessions/threads, skills, MCP, themes, a model
  switcher, web tools, and its own budget concept. It is, in effect, a
  Claude-Code-class terminal agent.
- Its model layer is **fully config-driven**. A provider in
  `~/.deepagents/config.toml` may set `class_path = "module:ClassName"` to
  instantiate any `BaseChatModel` directly, bypassing `init_chat_model`.

This means we can adopt the entire upstream TUI and add Databricks support with
**no fork of its model layer** — a far smaller, more maintainable surface than
hand-building (and forever maintaining) an equivalent REPL.

## Decision

`manta` launches `deepagents-code`'s interactive TUI, preconfigured for
Databricks. Manta is a **branded launcher + Databricks integration**, not a
fork:

1. **Model** — Manta provisions a `databricks` provider in
   `~/.deepagents/config.toml` with
   `class_path = "databricks_langchain:ChatDatabricks"` and the configured
   endpoints. `deepagents-code`'s `create_model("databricks:<endpoint>")`
   instantiates `ChatDatabricks` directly (validated end-to-end).
2. **Auth / profile** — `-p/--profile` is mapped to the SDK env var
   `DATABRICKS_CONFIG_PROFILE`, which `WorkspaceClient` (and thus
   `ChatDatabricks`) reads for unified auth from `~/.databrickscfg`.
3. **Launch** — `manta` execs `python -m deepagents_code`, injecting
   `-M databricks:<default_endpoint>` and forwarding any unrecognized flags
   (e.g. `-r`, `-a`, `--skill`) verbatim.

Scope for this change is **defer to upstream**: we use `deepagents-code`'s
native approval/HITL and budget UX. Manta's earlier `HeuristicRouter`,
`BudgetLedger` hard-caps, and `PolicyEngine` were initially parked and then
**removed from the active codebase** in a follow-up cleanup; if upstream UX
proves insufficient they would be re-introduced as `deepagents-code` runtime
middleware rather than a parallel pipeline (see git history for the originals).

## Consequences

- New module: `dcode.py` (config provisioning, profile→env, argv build, launch).
  `main.py` gains an entry-point wrapper (`main_entry` + `classify_args`) so
  bare/unknown invocations launch the TUI while Manta subcommands (`doctor`,
  `init`) still route through Typer.
- The active source surface shrinks to `main.py`, `dcode.py`, `auth.py`, and
  `config.py`. The hand-built REPL (`interactive.py`, `agent_builder.py`,
  `models.py`) and the role-pipeline modules (`routing.py`, `budget.py`,
  `policy.py`, `pipeline.py`, `roles.py`, `context_broker.py`, `schemas.py`,
  `session.py`, `agents/`, `tools/`) were removed in the follow-up cleanup;
  recover from git history if ever needed.
- `[agent]` extra now depends on `deepagents-code`, `databricks-langchain`,
  `databricks-sdk`, and `tomli-w`.
- `manta doctor` now checks `deepagents-code`, provisions the Databricks
  provider, and validates `create_model("databricks:<endpoint>")` offline.
- Branding is light-touch (startup splash subheader via
  `DEEPAGENTS_CODE_DANGEROUSLY_OVERRIDE_STARTUP_SUBHEADER`); deeper rebranding
  would require forking the TUI and is out of scope.
- Known gap: `deepagents-code -n` (non-interactive) spins up a langgraph server
  subprocess that hung in our environment; the interactive TUI launches in
  ~0.3 s. Non-interactive mode is tracked separately.
- Dependency note: installing `deepagents-code` upgrades `protobuf` to 6.x,
  which conflicts with `databricks-vectorsearch` (not a Manta runtime
  dependency). Pin/resolve in the environment if both are required.
