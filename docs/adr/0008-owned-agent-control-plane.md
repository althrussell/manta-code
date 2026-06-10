# ADR 0008 — Own the agent control plane as deepagents-code middleware

## Status

Accepted. Extends ADR 0007 (Manta remains a launcher around `deepagents-code`,
not a fork) and exercises the escape hatch ADR 0007 wrote down explicitly:

> "if upstream UX proves insufficient they would be re-introduced as
> `deepagents-code` runtime middleware rather than a parallel pipeline."

This ADR scopes that re-introduction to **agents + token economy + Databricks
tools**. The interactive runtime, TUI, sessions, and HITL stay upstream.

## Context

The post-0007 "subagents" are markdown files carrying only `name`,
`description`, and `model`. `deepagents_code`'s loader
(`deepagents_code.agent.create_cli_agent`) drops every other field, so users
cannot create real agents with enforced tools/permissions, durable memory, or a
token budget. Meanwhile the underlying `deepagents` SDK already supports all of
this via the `SubAgent` TypedDict (`tools`, `permissions`, `middleware`,
`interrupt_on`, `skills`) and `create_deep_agent(store=...)`. The capability is
present; Manta just was not using it. ADR 0007 also deleted Manta's original
budget/policy control plane.

Manta's only structural advantage over Claude Code / Cursor is Databricks-native
model serving + auth. A reason to switch therefore has to be Databricks
superpowers (Unity Catalog, SQL, jobs/DABs), not a reimplementation of features
the incumbents already have.

## Decision

Manta adds an **owned control-plane layer on top of `deepagents-code`** — not a
fork — composed of:

1. **An agent registry** (`~/.manta/agents/<name>/`): a richer agent definition
   (model, tools allow/deny, filesystem permissions, approval policy, skills,
   subagents, memory, budget, Databricks scopes) plus CRUD CLI.
2. **A factory** that compiles a Manta agent definition into the SDK's full
   `SubAgent` dict — so "read-only" is *enforced* via `FilesystemPermission`,
   not merely prompted.
3. **A build hook** that wraps the module-level
   `deepagents_code.agent.create_deep_agent` binding (the same monkeypatch
   pattern already used by `_install_subagent_databricks_resolver`) to inject
   Manta's compiled agents, middleware, a persistent `store`, and Databricks
   tools — with a **fallback to vanilla launch** if the patch target is missing.
4. **Middleware** for a trust-first token economy (accounting, warn →
   approve-to-continue, cost-aware routing) and policy enforcement.
5. **Databricks-native tools** (UC catalog/lineage, SQL, jobs/DABs, system
   tables), governed by the caller's UC permissions.

The single seam is `create_deep_agent`. `create_cli_agent` calls it with
`subagents=`, `middleware=`, `tools=`, `backend=`; wrapping that one symbol lets
Manta enrich all of them in one place.

## Reliability (non-negotiable)

Because the hook monkeypatches an internal upstream symbol, reliability is a
first-class feature, not a footnote:

- Upstream `deepagents`/`deepagents-code` versions are **pinned** in
  `pyproject.toml`.
- `manta doctor` verifies every patch target still exists
  (`deepagents_code.agent.create_deep_agent`, `deepagents._models.resolve_model`,
  `deepagents_code.agent.create_cli_agent`).
- The build hook is wrapped so any failure logs a clear warning and **falls back
  to a vanilla launch** — `manta` must start every time.
- Contract tests (`tests/test_reliability.py`) pin the integration surface so an
  upstream bump that moves a symbol fails CI rather than users' launches.

## Consequences

- New modules: `manta_code/reliability.py`, `manta_code/hook.py`,
  `manta_code/agents/` (registry, factory, memory, usage, importer, defaults),
  `manta_code/middleware/` (economy, routing, policy), `manta_code/databricks_tools.py`,
  and `evals/`.
- `manta agents` grows `create`/`edit`/`delete`/`show`/`list`; new `manta cost`
  and `manta budget` surface token/cost analytics.
- Agent definitions live at `~/.manta/agents/<name>/` (TOML + `AGENTS.md`),
  distinct from the upstream `~/.deepagents/agent/agents/` markdown dir.
- Enterprise/fleet management (central registry, per-user budgets, MLflow audit,
  RBAC) is explicitly **out of scope** here and sketched as a later phase.
