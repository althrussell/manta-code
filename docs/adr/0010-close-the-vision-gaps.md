# ADR 0010 — Close the vision gaps: providers, tasks, advice, gateway

## Status

Accepted. Implements the gap-closure plan recorded in [CLOSE_GAPS.md](../../CLOSE_GAPS.md)
(itself derived from [VISION.md](../../VISION.md)). Extends ADR 0008's control
plane; stays inside ADR 0007's constraint (launcher + middleware over
`deepagents-code`, never a fork). ADR 0009 (enterprise/fleet) remains design-only;
everything here keeps its shapes serializable so the fleet layer bolts on later.

## Context

CLOSE_GAPS.md scored the codebase against the seven vision pillars: the
reliability seam, the enforced agent registry, and token accounting are built;
the four headline differentiators — multi-provider brokering, proactive model
advice, `@{agent}` long-running tasks, and the chief of staff — range from stub
to absent. The single most load-bearing defect is that Databricks is assumed to
be the *only* provider everywhere (auth, model resolution, the `/auth` screen,
launch), which contradicts both Pillar 2 ("never married to one lab") and
Pillar 6 ("Databricks is never the place you're trapped").

This ADR records the decisions taken to close those gaps, made 2026-06-11.

## Decisions

### Scope and delivery

- **Four phases, A–D**, delivered as **stacked, sequential PRs** (one branch per
  phase), implemented back-to-back. Enterprise/fleet (ADR 0009) stays out of
  scope.
- Every phase ships with unit/contract tests **and a live smoke run** (real
  workspace, real TUI) before its PR opens. Historically, the `_boot.py` class
  of regression only shows up live.
- CLOSE_GAPS.md is the living tracker: items are checked off as phases land.

### Phase A — Databricks-first, not Databricks-only

1. **Provider abstraction** (`manta_code/providers/`): a `ModelRef`
   (`provider:model`) plus a resolver registry. The existing
   `MantaChatDatabricks` resolver becomes the first registered provider; the
   `databricks:` prefix stops being a hardcoded assumption in routing, pins,
   and the subagent resolver.
2. **`_boot.py` degrades from "restrict" to "prefer"**: `get_available_models()`
   lists Databricks endpoints **first** and keeps them the default, but no
   longer deletes upstream's anthropic/openai/google providers. Off-Databricks
   use is real, not theoretical.
3. **`/auth` screen = profile picker + provider keys**: the Databricks profile
   picker stays the primary, top-listed section; upstream's provider API-key
   entries are restored below it. One screen, Databricks-first ordering,
   nothing stripped.
4. **Databricks becomes detect-and-enable**: no profile / no workspace → skip
   endpoint discovery, skip Databricks tool injection, fall through to other
   configured providers. `manta doctor` reports "Databricks: not configured
   (optional)" instead of failing.
5. **Visible degraded mode**: when the build hook falls back to a vanilla
   launch, Manta prints one startup line saying so and pointing at
   `manta doctor`. Falling back stays non-negotiable; falling back *silently*
   ends.
6. **Contract tests extend to every `_boot.py` patch target** (banner attrs,
   `get_available_models`, `AuthManagerScreen` methods, `_build_server_cmd`),
   not just the three in `reliability.py`.

### Phase B — `@{agent}`, long-running tasks, chief of staff v1

1. **Task store**: `~/.manta/.state/tasks.db` (same SQLite pattern as
   usage/memory) — id, agent, prompt, state
   (queued/running/done/failed/cancelled), result, ledger linkage.
2. **Execution model: detached subprocess per task.** Each submitted task
   spawns a detached, bounded, headless `manta run`-style process that writes
   state/results to the task store. No daemon: nothing to install, supervise,
   or secure; tasks survive the parent session; the enforced headless path is
   reused as-is.
3. **Addressing syntax**: `@swe land this refactor` delegates deterministically
   **inline** (synthesized `task` tool call — the `PlanDelegationMiddleware`
   pattern: one-fire-per-turn, fail-open). A trailing **`&`** (or `--bg` on the
   CLI) submits it as a **background task** and immediately returns a task id.
4. **CLI**: `manta task submit <agent> "..." | list | status | output | cancel`.
5. **Observability**: policy/economy middleware (which already see every call)
   append lightweight events to the task store; `manta status` joins
   tasks × events × ledger into the single pane of glass.
6. **Chief of staff**: a built-in `chief` agent whose tools are the task store
   (submit/status/collect) plus delegation — the same data backs the CLI view.
7. **Approval audit**: every HITL approve/deny is recorded (ADR 0009's audit
   trail needs this later).

### Phase C — proactive advice, tiered delivery

1. **Session signals** collected in `TokenEconomyMiddleware` (it already
   touches every model call): consecutive same-model calls, spend rate,
   tool-error/retry counts, tier of each call.
2. **Rule-based advisor v1** (deterministic, like the rest of Manta):
   escalate-on-repeated-failure, downgrade-on-boilerplate-streak,
   present-the-trade-off on projected budget blowout. The keyword router
   remains only as the cold-start prior.
3. **Tiered delivery — the decision**: low-stakes advice is an **inline
   advisory note** (logged, never blocking); high-stakes advice reuses the
   existing **`interrupt()` approve-to-continue** pattern. The user always
   decides; loops are never nagged.
4. **Advice ledger**: each recommendation, whether it was accepted, and the
   next-N-calls cost delta — the dataset that later makes routing closed-loop.

### Phase D — AI Gateway brokering, live evals

1. **Gateway integration is built against a live workspace**: routes discovered
   via the SDK from the user's configured profile, verified with real calls —
   not built to docs and mock-verified.
2. Gateway-backed provider plugs into the Phase A abstraction
   (`[runtime] gateway` in `.manta/config.toml`); serving-endpoint discovery
   remains the fallback.
3. **Live model switching**: `manta agents set-model <name> <ref>` plus an
   in-app pathway; `ModelPinMiddleware` already resolves per-call, so registry
   changes take effect next turn.
4. **Pricing becomes pluggable**: config-file overrides now, gateway-fed rates
   when available.
5. **Live eval mode** (`python -m evals --live`): the benchmark runs through
   the real headless path and records real ledger rows, so the cost/quality
   claim is proven end-to-end, not just in the canned harness.
6. Verification spend is not cost-gated ("unbounded within reason") — the
   ledger itself is the spend record.

## Alternatives considered

- **Persistent task daemon** — more capable (queueing, global concurrency,
  restart recovery) but adds a long-lived process to install and secure;
  rejected in favor of detached subprocesses until usage proves the need.
- **Gateway-only multi-provider** (never expose direct API keys) — cleanest
  governance story but ships nothing multi-provider until Phase D; rejected.
- **Interrupt-for-every-advice** — maximum control, but nagging; rejected for
  the tiered model.
- **CLI-only `@agent`** (defer TUI parsing) — lower risk, but the headline UX
  of the vision; rejected.

## Consequences

- New packages/modules: `manta_code/providers/`, `manta_code/tasks/` (store +
  executor), advice engine in `middleware/economy.py` or a sibling,
  `manta task` / `manta status` CLI surfaces.
- `_boot.py`'s patch surface is now contract-tested in full; its behavior
  changes from removing upstream capability to reordering it.
- The `databricks:` scheme survives as one provider among several; existing
  agent definitions keep working unchanged.
- The local-first stores (tasks, advice, approvals) stay serializable so ADR
  0009's publish/central-ledger/MLflow layers are additive, not rewrites.
