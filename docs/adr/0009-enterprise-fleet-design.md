# ADR 0009 — Enterprise / fleet management (design only)

## Status

Proposed (design only). This ADR records the *intended* shape of Manta's
enterprise/fleet story so the solo-user control plane built in ADR 0008 does not
paint us into a corner. **Nothing here is implemented yet**; it is a target the
Phase 0–6 work should remain compatible with.

## Context

ADR 0008 delivered a solo power-user control plane: a local agent registry
(`~/.manta/agents/<name>/`), enforced tools/permissions, durable per-agent memory
(`~/.manta/.state/memory.db`), a token economy + local usage ledger
(`~/.manta/.state/usage.db`), and a headless `manta run` path. Everything is
**local and single-user** by design.

A team or org needs more: shared agents that are versioned and governed, budget
caps that hold across many users, an audit trail of what agents did and what data
they touched, and identity-based access control. Building that prematurely would
have slowed the wedge (Databricks-native superpowers) and the proof-of-value
(eval harness), so it was deliberately deferred. This ADR captures the design.

The guiding constraint is the same as ADR 0008: **Databricks-native is the
differentiator.** The fleet layer should be built *on Databricks primitives*
(Unity Catalog, Lakebase, MLflow, Databricks identity), not a bespoke control
plane, so it inherits the governance customers already trust.

## Decision (intended)

Five capabilities, each mapped to a Databricks primitive and each a additive
layer over the existing local control plane — never a rewrite of it.

### 1. Central agent registry (shared, versioned)

- **Where:** a Unity Catalog schema (e.g. `manta.agents`) holding agent
  definitions as rows/volumes, or a Lakebase table for low-latency reads.
- **What:** the same `AgentDef` schema (ADR 0008) serialized to the catalog, with
  a version column and an owner. `manta agents publish <name>` writes the local
  def upstream; `manta agents pull <name>` materializes it into the local
  registry. The local registry stays the runtime source of truth (so offline and
  air-gapped use keep working); the catalog is the *distribution* mechanism.
- **Why UC:** grants/ownership/lineage come for free; sharing an agent is a UC
  GRANT, not a new ACL system.

### 2. Per-user / per-team budget caps

- **Where:** budget policy rows in the same catalog/Lakebase, keyed by
  Databricks principal (user/group).
- **What:** the Phase 4 `TokenEconomyMiddleware` already accumulates per-thread
  cost and pauses (approve-to-continue) at a cap. The fleet version reads the
  *effective* cap for the caller's principal (org override > team > personal) and
  publishes usage to a central ledger table in addition to the local one. Caps
  remain trust-first: pause-for-approval, never silent work loss.

### 3. Audit + observability via MLflow Tracing

- **Where:** MLflow Tracing (already a first-class Databricks GenAI primitive).
- **What:** wrap agent runs in MLflow traces capturing tokens, cost, tool calls,
  approvals granted/denied, and — critically for the Databricks user's top fear —
  *which UC objects were read/written*. The local usage ledger is the offline
  fallback; MLflow is the org-visible, queryable audit surface.
- **Why:** reuses the platform's audit/observability rather than inventing a log
  format; ties cost back to the same traces SAs already inspect.

### 4. RBAC + SSO via Databricks identity

- **What:** authentication is already the Databricks profile (ADR 0007/0008).
  The fleet layer adds *authorization*: who may publish/pull which agents, whose
  budget policy applies, who may grant approvals. These map to Databricks
  groups/UC privileges — no parallel identity store.
- **Hard boundary stays UC:** an agent can never read/write data the calling
  principal can't; org policy can only *narrow* that, never widen it.

### 5. Agent publish / pull (sharing workflow)

- `manta agents publish` / `pull` (sketched in 1) plus a `manta agents search`
  over the central registry, so a team standardizes on vetted agents (e.g. a
  blessed read-only "prod-reviewer") instead of everyone hand-rolling one.

## Compatibility requirements on the current code (so we don't regret 0008)

- `AgentDef` must stay serialization-stable and round-trippable (it is: TOML +
  `AGENTS.md`), so it can move to a catalog row without a schema break.
- The usage ledger writer (`manta_code.agents.usage`) must keep a clean
  `record_usage` seam so a second (remote) sink can be added without touching the
  middleware.
- The economy middleware must keep budget resolution injectable (caps are passed
  in, not hard-coded), so an org policy resolver can replace the local one.
- Memory namespacing (`("memories", <ns>)`) must stay principal-agnostic locally
  but allow a principal/team prefix later without migrating existing rows.

## Consequences

- No code in this ADR; it is a guardrail for reviewers ("does this change make the
  fleet story harder?") and a roadmap entry.
- Explicit non-goals for now: a hosted Manta service, a web UI, cross-cloud
  federation. These are revisited only after the wedge + eval prove adoption.

## Alternatives considered

- **Bespoke control-plane service** (Postgres + custom RBAC + custom audit):
  rejected — it re-implements what UC/MLflow/Databricks identity already provide
  and abandons the "Databricks-native is the differentiator" thesis.
- **Git-based agent sharing** (commit `~/.manta/agents` to a repo): a fine
  *stopgap* and works today, but lacks governance, budget enforcement, and audit;
  recorded as the interim option, not the destination.
