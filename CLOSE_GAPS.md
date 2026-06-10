# CLOSE_GAPS — Bringing the Manta Vision to Life

A full review of the codebase (src, middleware, agents, evals, tests, ADRs) against
[VISION.md](VISION.md). For each vision pillar: what exists today (with file
references), what's missing, and the concrete work to close the gap. Ends with a
prioritized roadmap.

> **Living tracker** (ADR 0010): items are checked off as the phase PRs land.
>
> - **Phase A landed** — provider abstraction (`manta_code/providers/`),
>   Databricks-first-not-only model discovery and `/auth`, Databricks-optional
>   launch/doctor/tools, visible degraded mode, contract tests over every
>   `_boot` patch target.
> - **Phase B landed** — task store + detached background tasks
>   (`manta_code/tasks/`), `@agent` addressing (inline + `&` background),
>   `manta task`/`manta status` CLI, event feed with approval/denial audit,
>   `chief` built-in agent with in-session task tools, doctor validation of
>   agent model pins.
> - **Phase C landed** — proactive advice engine (session signals + tiered
>   delivery: inline notes / approve-to-continue interrupts), advice ledger,
>   `manta cost --advise` offline recommendations, per-task spend attribution,
>   and a critical accounting fix: streamed chunks carried *cumulative* usage
>   that the chunk merge summed, inflating recorded input tokens by ~chunk-count
>   (now rewritten as deltas in `MantaChatDatabricks`). Plus background-task
>   robustness: cwd-safe runner, crash-proof state recording, stale-task
>   reconciliation.
> - Phase D in flight.

**TL;DR.** The foundation is genuinely strong — the reliability seam (Pillar 7),
the enforced agent registry (Pillar 4's bottom half), and token accounting
(Pillar 1's bottom half) are built and tested. The four headline differentiators
are not: **multi-provider via AI Gateway (P2), proactive model advice (P3),
`@{agent}` long-running tasks (P4 top half), and the chief of staff (P5)** range
from stub to absent. The single most load-bearing gap is that the code assumes
**Databricks is the only provider, everywhere** — lifting that assumption unlocks
P2, P3, and the "works off Databricks" promise at once.

---

## Scorecard

| # | Vision pillar | Status | Summary |
|---|---|---|---|
| 1 | Token economy first-class | 🟡 **~60%** | Accounting, ledger, cache/scaffold splits, trust-first budget caps all built. No spend *optimization*, no live advice, accounting is passive. |
| 2 | Multi-provider via AI Gateway | 🔴 **~10%** | Databricks serving endpoints only. No gateway client, no provider abstraction; non-Databricks providers are actively *removed* from the UI. |
| 3 | Proactive model advice | 🔴 **~5%** | One keyword-list escalation heuristic. No session analysis, no recommendations surfaced to the user, no outcome learning. |
| 4 | Addressable agents + long-running tasks | 🟡 **~45%** | Registry/CRUD/enforcement/memory/budgets: done and enforced. `@{agent}` addressing, background tasks, detach/poll/collect: absent. |
| 5 | Chief of staff | 🔴 **~15%** | Deterministic plan delegation + a cost `watch` table exist. No orchestrator persona, no in-flight observability, no output aggregation. |
| 6 | Databricks-native, optional | 🟡 **~50%** | Native power is real (UC/SQL/lineage/jobs tools). "Optional" is false today: auth, models, and launch all hard-require a Databricks profile. |
| 7 | Reliability as a feature | 🟢 **~90%** | Pinned versions, `doctor` preflight, contract tests, fall-back-to-vanilla launch. Gap: degradation is *silent* to the user at runtime. |

---

## Pillar 1 — Token economy as a first-class citizen

### What exists (solid)

- **Per-call accounting** to a local SQLite ledger (`~/.manta/.state/usage.db`):
  agent, model, thread, input/output tokens, cost —
  `middleware/economy.py:136-293`, `agents/usage.py:193-258`.
- **Cache-hit vs miss split**: input decomposed into cache-read / cache-creation /
  uncached, each priced at its own rate (Anthropic economics: 0.1×/1.25×) —
  `agents/usage.py:101-134`.
- **Scaffolding vs net-new split**: system prompt + tool/skill/memory schemas
  estimated and reconciled against the provider's real input total —
  `middleware/economy.py:93-116`.
- **Trust-first budgets**: per-agent `budget_max_tokens`/`budget_max_usd`
  (`agents/registry.py:112-113`); soft warning at 80%, hard cap pauses via
  LangGraph `interrupt()` and asks to continue — never a silent kill
  (`middleware/economy.py:185-233`). Exactly matches the vision's
  warn → approve-to-continue language.
- **Reporting CLI**: `manta cost` (by agent/model/task, `--breakdown` for
  scaffold ratio), `manta budget`, `manta watch` — `main.py:591-799`.
- **Eval harness proving the economics**: cost-aware solver must match premium
  quality (within 2%) at lower cost to "win" — `evals/harness.py:106-130`.

### Gaps

| Gap | Detail |
|---|---|
| **G1.1 Accounting is passive, not optimizing** | The vision says "spends the minimum that still does the job." Today Manta *measures* waste but never *reduces* it: no context trimming, no cache-alignment strategy (stable system-prompt prefixes, reused tool schemas), no scaffold-overhead remediation. |
| **G1.2 No spend insight surfaced in-session** | All reporting is out-of-band CLI. The TUI session never shows running spend, cache-hit rate, or scaffold ratio. |
| **G1.3 Task granularity is weak** | The ledger's `task` column is effectively the agent name (`economy.py:264`); "per task" accounting promised in the vision can't drill below the agent. |
| **G1.4 Pricing is a hardcoded table** | `usage.py:66-78` substring-matches ~10 models. Unknown models get estimated costs silently marked `*`. No way to pull real rates from a gateway. |
| **G1.5 Eval harness is canned** | Solvers replay a static answer bank (`evals/solvers.py:22-49`); no live end-to-end run measures real Manta sessions, so "can prove it" is only proven in miniature. |

### Work to close

1. **Cache-economy optimizer** (highest ROI): keep scaffolding (system prompt,
   tools, memory header) byte-stable across turns so provider prompt caching
   actually hits; add a `manta cost --advise` report ("your scaffold is 62% of
   spend; trim these 3 tools / pin this prompt").
2. Thread a real **task identifier** (delegation target + turn) through
   `record_usage()` so per-task drill-down works.
3. Surface a **session economy line** in the TUI (running $ / cache-hit % via the
   existing middleware — it already has the numbers).
4. Make pricing **pluggable** (config-file overrides now; gateway-fed rates when
   P2 lands).
5. Add one **live eval mode** (`python -m evals --live`) that runs the real
   headless `manta run` path against the benchmark and records actual ledger rows.

---

## Pillar 2 — True multi-model, multi-provider via AI Gateway

### What exists

- Models are Databricks serving endpoints, auto-discovered (`auth.py:117-138`),
  merged into `~/.deepagents/config.toml` as one custom provider
  (`dcode.py:97-158`) and resolved via `MantaChatDatabricks`
  (`databricks_chat.py:133-211`).
- Cross-vendor models *do* run today (Claude/GPT/Gemini built-ins pin different
  endpoints — `agents/defaults.py:31-101`) — but **only because Databricks serves
  them**. That is the seed of the multi-provider story, not the story itself.

### Gaps

| Gap | Detail |
|---|---|
| **G2.1 No AI Gateway integration** | The vision's centerpiece. No gateway URL, no unified auth, no rate-limit/usage-policy passthrough, no "new model available the day the gateway exposes it." Endpoints are called directly. |
| **G2.2 Providers actively removed** | `_boot.py:158-187` overrides `get_available_models()` to return *only* Databricks, and `_boot.py:242-413` replaces the upstream multi-provider API-key auth screen with a Databricks profile picker. Upstream already supports Anthropic/OpenAI/Google — Manta deletes that capability rather than brokering it. |
| **G2.3 `databricks:` prefix assumed everywhere** | Model pins (`registry.py:83-84`), the subagent resolver (`databricks_chat.py:166-211`), routing factories (`routing.py:183-207`), and constants in `dcode.py:40`/`_boot.py:36` all hardcode one provider scheme. |
| **G2.4 No live model switching per role** | Vision: "pinned per agent, and switchable live." Pins exist; there is no in-session affordance to change an agent's model without editing TOML and resyncing. |

### Work to close

1. **Introduce a provider abstraction** (`providers/` package): a `ModelRef`
   (`provider:model`) + resolver registry. Port the Databricks resolver into it;
   re-enable upstream's native anthropic/openai/google providers through the same
   interface instead of deleting them in `_boot.py`.
2. **Gateway mode**: when the workspace exposes AI Gateway routes, discover and
   list them alongside (eventually instead of) raw serving endpoints; route
   chat calls through the gateway so auth, rate limits, and usage tracking are
   centralized. Config: `[runtime] gateway = "..."` in `.manta/config.toml`.
3. **Degrade `_boot.py` from "restrict" to "prefer"**: Databricks models first
   and default, other configured providers still selectable (this is also the
   key to Pillar 6's "optional, not required").
4. **Live per-agent model switch**: `manta agents set-model <name> <ref>` +
   in-app pathway; `ModelPinMiddleware` (`routing.py:116-160`) already resolves
   per-call, so a registry change can take effect next turn.

---

## Pillar 3 — Proactive model advice

### What exists

- A single keyword heuristic: 12 hardcoded hints ("plan", "debug", "refactor",
  …) escalate one call to a premium endpoint — `routing.py:25-55`, wired in
  `hook.py:149-224`. Tested, deterministic, and falls back safely. That's it.

### Gaps

| Gap | Detail |
|---|---|
| **G3.1 No advice engine** | Nothing watches "how a session is actually going." No detection of premium-on-boilerplate, repeated failures on a cheap model, or large cost-sensitive tasks — the three canonical examples in the vision. |
| **G3.2 No surface for advice** | Even if computed, there is no channel to show "switch models?" in-session, and no one-keystroke accept. The budget `interrupt()` (`economy.py:192-233`) is the only existing pause mechanism. |
| **G3.3 No outcome tracking** | Escalation decisions are never scored, so there is nothing for "closed-loop routing that learns" to learn from. The ledger has cost but not outcomes (retries, failures, user edits/rejections). |

### Work to close (build on what's there)

1. **Session signal collector**: extend `TokenEconomyMiddleware` to keep
   per-thread rolling stats it already touches — consecutive same-model calls,
   spend rate, tool-error/retry counts, model tier of each call.
2. **Rule-based advisor v1** (deterministic, like everything else in Manta):
   - N cheap-model failures/retries on the same step → "escalate to <strong>".
   - Premium model emitting low-complexity output (short tool-arg edits, bulk
     transforms) for M consecutive calls → "drop to <cheap>".
   - Projected task cost > budget × threshold → present the trade-off.
3. **Delivery mechanism**: reuse the existing `interrupt()` approve-to-continue
   pattern for high-stakes advice; for low-stakes, append a one-line advisory to
   the model/system channel and log to the ledger. User always decides.
4. **Log advice + outcome** (`advice` table next to `usage`): what was
   recommended, accepted?, and the next-N-calls cost delta — the dataset that
   later makes routing closed-loop.
5. Replace the keyword list as the *only* router once 1–2 exist; keep it as the
   cold-start prior.

---

## Pillar 4 — Addressable agents and long-running tasks

### What exists (the strongest area after reliability)

- **Real registry**: `~/.manta/agents/<name>/` (TOML + AGENTS.md), full CRUD CLI
  (`manta agents create/edit/delete/show/list/sync/import/memory`) —
  `agents/registry.py:139-241`, `main.py:269-568`.
- **Enforced boundaries at the tool-call layer** — exactly as the vision demands:
  read-only, allow/deny lists, per-path filesystem globs (first-match-wins), all
  checked in `ToolPolicyMiddleware.wrap_tool_call` *before* the handler runs —
  `middleware/policy.py:105-189`. Approval gates compile to deepagents'
  native HITL `interrupt_on` (`agents/factory.py:60`).
- **Durable memory** per agent with mandatory secret redaction (17 patterns) —
  `agents/memory.py:39-127`; recall middleware injects notes read-only.
- **Per-agent budgets** enforced (see Pillar 1).
- **Three built-in specialists** (planning/swe/review) with sensible enforcement
  defaults — `agents/defaults.py:31-101`.

### Gaps

| Gap | Detail |
|---|---|
| **G4.1 No `@{agent}` addressing** | No syntax in the TUI or CLI to direct a message at a named agent. Closest: selecting a profile via `/agents`, or the model choosing to call the `task` tool. |
| **G4.2 No long-running tasks** | All delegation is synchronous and inline — the orchestrator blocks on every subagent call. Nothing supports "hand it work, keep working while it runs." `manta run` (`main.py:697-750`) is one-shot, foreground, timeout-bounded. |
| **G4.3 No task lifecycle** | No task IDs, no queue, no persistence, no status polling, no result retrieval, no cancel. |
| **G4.4 No approval/denial audit log** | HITL decisions vanish; ADR 0009's audit story will need them. |

### Work to close

1. **Task store** (`~/.manta/.state/tasks.db`, same pattern as usage/memory):
   id, agent, prompt, state (queued/running/done/failed/cancelled), result ref,
   ledger linkage.
2. **Background executor**: detached `manta run`-style subprocess per task —
   the headless path (`dcode.py:379-423`) already builds bounded, enforced runs;
   it needs a daemonized variant writing into the task store.
3. **CLI surface**: `manta task submit <agent> "..."` / `list` / `status` /
   `output` / `cancel`.
4. **`@{agent}` in-session**: a lightweight middleware on the orchestrator that
   recognizes a leading `@name` in the human message and (a) deterministically
   delegates to that subagent inline, or (b) with `--bg`/`&`, submits a
   background task and returns its id immediately.
   `PlanDelegationMiddleware` (`delegation.py:132-197`) is the proven template —
   same synthesized-tool-call, one-fire-per-turn, fail-open pattern.
5. **Audit approvals**: record every HITL approve/deny in the ledger.

---

## Pillar 5 — The Chief of Staff

### What exists

- **Deterministic plan delegation**: regex plan-intent detection routes "make a
  plan" to the planning agent without burning a model call —
  `delegation.py:51-197`. This is the first concrete instance of the vision's
  "routes work deterministically where it matters."
- **`manta watch`**: a live cost/activity table per agent (`main.py:754-799`) —
  spend observability, but not task observability.

### Gaps

| Gap | Detail |
|---|---|
| **G5.1 No chief-of-staff agent** | The base orchestrator is upstream's vanilla agent with middleware attached. There is no named persona whose job is delegate → monitor → collect → report. |
| **G5.2 No in-flight observability** | Nobody can see what each agent is doing *now* — current task, recent tool calls, status. The ledger is written per-call but nothing reads it live per task. |
| **G5.3 No output aggregation** | Results from delegated work come back only as inline tool results in one conversation. With background tasks (G4.2) absent, there's nothing to collect yet — these two gaps unlock together. |
| **G5.4 Only planning is routed** | Review/research/bulk-transform delegation is left to the model's judgment. |

### Work to close (sequenced after Pillar 4's task store)

1. **Event stream**: have the policy/economy middleware (which already see every
   tool call and model call) append lightweight events (agent, task-id, tool,
   status) to the task store.
2. **`manta watch` v2 / `manta status`**: join tasks × events × ledger — per
   agent: current task, last tool call, tokens/$ so far, state. This is the
   "single pane of glass" with no new instrumentation needed beyond (1).
3. **Chief-of-staff agent definition**: a built-in `chief` profile whose tools
   are the task store (submit/status/collect) + delegation — so "ask the chief"
   works in-session, and the same data backs the CLI view.
4. **Extend deterministic routing** beyond planning: review-intent → `review`
   (the regex + one-fire pattern in `delegation.py` generalizes directly).
5. **Result collection**: `manta task output <id>` and a chief-of-staff tool
   that pulls completed task outputs into the current session.

---

## Pillar 6 — Databricks-native power, optional not required

### What exists

- The native power is real: read-only-validated SQL, UC catalog/schema/table
  browsing, lineage via system tables, jobs (run gated behind approval) —
  `databricks_tools.py:43-290`; per-agent tool scoping via `databricks_tools`
  list in the registry.
- Conservative SQL write-protection (`is_read_only_sql`, `databricks_tools.py:43-59`).

### Gaps

| Gap | Detail |
|---|---|
| **G6.1 Databricks is required, not optional** | The vision: "You can point it at any stack… When you're not [on Databricks], none of it gets in your way." Today: auth *is* a Databricks profile (`auth.py:56-107`), models *are* serving endpoints, `manta doctor` and launch assume a workspace. Off Databricks, Manta doesn't degrade — it doesn't work. |
| **G6.2 Tools injected unconditionally** | `hook.py:227-234` adds UC/SQL tools to every orchestrator; off-workspace they'd just error at call time. |

### Work to close

1. **Make the Databricks layer detect-and-enable**: no profile / no workspace →
   skip endpoint discovery, skip Databricks tools, fall through to other
   configured providers (depends on P2 work item 3). `manta doctor` reports
   "Databricks: not configured (optional)" instead of failing.
2. Gate `build_databricks_tools` on a resolvable, authenticated client.

---

## Pillar 7 — Reliability as a feature

### What exists (best-in-class for the codebase)

- Exact upstream pins (`deepagents-code==0.1.7`, `deepagents==0.6.7`) in
  `pyproject.toml`; three patch targets verified by `manta doctor`
  (`reliability.py:40-112`); contract tests fail CI when upstream moves
  (`tests/test_reliability.py`); every enrichment is guarded and **falls back to
  a vanilla launch** (`hook.py:298-332`). The ADR 0008 reliability contract is
  implemented as written.

### Gaps

| Gap | Detail |
|---|---|
| **G7.1 Silent degradation** | If the seam breaks at runtime, Manta launches vanilla with only a log-file warning — the user gets no agents/budgets/tools and no visible explanation. "Falls back cleanly" is met; "shows you everything" is not. |
| **G7.2 Three monkeypatch surfaces beyond the verified seam** | `_boot.py` patches banners, model lists, the auth screen, and the server command builder — none covered by `verify_patch_targets()` or contract tests. |

### Work to close

1. **Visible degraded mode**: when the hook falls back, set an env/state flag and
   print one line at startup ("⚠ Manta control plane inactive — running vanilla;
   run `manta doctor`").
2. **Extend contract tests** to the `_boot.py` patch targets (banner attrs,
   `get_available_models`, `AuthManagerScreen` methods, `_build_server_cmd`).

---

## Cross-cutting debt worth noting

- **Headless/SDK path** (vision "Next"): `manta run` is solid for CI one-shots,
  but there is no importable Python API (`manta_code.sdk`) to build/run an
  enforced agent programmatically. Becomes cheap once the task store exists.
- **Enterprise/fleet** (ADR 0009) is design-only by intent; the local-first
  shapes above (task store, advice log, approval audit) should stay
  serializable so publish/central-ledger/MLflow layers bolt on without rework.
- **Policy hardening**: filesystem globs don't resolve symlinks
  (`policy.py:105-125`), and the `databricks_tools` per-agent subset deserves an
  explicit enforcement test.
- **One-way profile sync**: hand-edits to generated `~/.deepagents/<name>/`
  profiles are overwritten on next sync — fine, but undocumented.

---

## Recommended roadmap

Ordered for dependency and differentiation-per-effort:

| Phase | Theme | Items | Why first |
|---|---|---|---|
| **A. Foundation lift** | Provider abstraction + visible reliability | P2.1, P2.3, P6.1–6.2, P7.1–7.2 | Unblocks every other pillar; turns "Databricks-only" into "Databricks-first"; small, mostly refactoring. |
| **B. Tasks + chief of staff v1** | The agent story | P4.1–4.5, P5.1–5.3 | `@agent` + background tasks + `manta status` is the most visible vision payoff and is built almost entirely from existing patterns (headless run, delegation middleware, SQLite stores). |
| **C. Advice engine v1** | The economy story | P3.1–3.4, P1.1–1.3 | Needs Phase A's model-switch plumbing and Phase B's signals; rule-based first, learning later. |
| **D. Gateway + live evals** | The brokering story | P2.2, P2.4, P1.4–1.5, P5.4–5.5 | Gateway integration lands on top of the provider abstraction; live evals then prove the cost/quality claims end-to-end. |
| **E. Enterprise (ADR 0009)** | Fleet | publish/pull, central ledger, MLflow audit, RBAC | Explicitly later; phases A–D keep its constraints. |
