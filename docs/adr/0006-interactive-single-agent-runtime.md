# ADR 0006 — Interactive single-agent runtime on Databricks

## Status

Superseded by [ADR 0007](0007-adopt-deepagents-code-tui.md). The hand-built
interactive REPL is parked in favor of adopting the `deepagents-code` TUI.
Retained for historical context.

## Decision

`manta` (no subcommand) launches an interactive, streaming coding session backed
by a single **persistent** Deep Agents agent over one LangGraph thread, running
on Databricks-hosted models via `ChatDatabricks`. The primary "builder" agent
delegates to `planner` / `code_reviewer` / `security_reviewer` as Deep Agents
subagents. The per-role one-shot `DeepAgentsRuntime` (ADR 0001) remains for the
batch `manta run` path.

## Rationale

- The product goal is a Claude-Code-style interactive coding agent, not a batch
  CLI. A persistent agent + token streaming + human-in-the-loop is the right
  shape; per-role one-shot invocation is not.
- Databricks is the model provider: the SDK `WorkspaceClient` already gives
  unified profile auth, so we authenticate through it (default profile, with
  `-p/--profile` and `MANTA_PROFILE` / `DATABRICKS_CONFIG_PROFILE` overrides)
  rather than porting CLI auth flows.
- Manta keeps ownership of routing, budgeting, and policy by wrapping the agent:
  a per-turn router header, a `BudgetLedger` fed from streamed `usage_metadata`,
  and policy applied at interrupt-handling time.

## Consequences

- New modules: `auth.py`, `models.py`, `agent_builder.py`, `interactive.py`;
  `config.py` gains `[runtime]` and `[interactive]`; `[models]` entries become
  Databricks serving-endpoint names.
- Real files and shell come from a `CompositeBackend` routing `/` to a
  `LocalShellBackend(root_dir=cwd, virtual_mode=True)`.
- Because `InterruptOnConfig` has no per-call `when` predicate in this Deep
  Agents version, policy is enforced where interrupts are handled: allowed
  actions auto-approve, blocked actions auto-reject, approval-required actions
  prompt (overridable with `/approve-mode auto`).
- HITL requires a checkpointer; the default is an in-memory saver, so resume
  works in-process. Cross-restart (SQLite) thread persistence is a follow-up.
- Live end-to-end use requires the `[agent]` extra plus a reachable Databricks
  workspace and serving endpoint; `manta doctor` preflights these.
