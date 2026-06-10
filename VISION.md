# Manta — Vision

## The one-line bet

**A best-in-class coding agent that always makes the best possible use of every
token — running the right model from any provider for the job, advising you when
to switch, and letting you hand long-running work to named agents coordinated by
a chief-of-staff that shows you everything.**

Manta is for builders working **on and off Databricks**. Databricks is its home
and its power source — model serving, AI Gateway, and lakehouse-native tools —
but the product is not Databricks-only. You can point it at any stack, any
codebase, any provider, and it stays just as sharp.

---

## North star

The incumbents (Claude Code, Cursor) proved the interactive coding-agent UX.
Manta does not try to out-clone them. It bets on three things they treat as
afterthoughts and makes them the product:

1. **Token economy as a first-class citizen** — every action accounts for its
   cost and is engineered to spend the *fewest tokens that still do the job
   well*.
2. **True multi-model, multi-provider** — never married to one lab. The best
   model for *this* step, whoever makes it, brokered through a gateway.
3. **Real, addressable, governable agents** — not prompt personas, but durable
   workers you can name, task, watch, and trust.

Everything generic (the TUI, sessions, HITL) is borrowed from the upstream
runtime so all the investment lands on the differentiators above.

---

## Pillars

### 1. Always the best use of tokens — whatever it's doing

Token efficiency is the prime directive, not a billing footnote. Manta:

- **Accounts transparently** — cost and tokens per agent, per model, per task,
  with cache-hit vs. miss and *scaffolding (skills, defaults, system prompts)
  vs. net-new* breakdowns, so you can see exactly where tokens go.
- **Spends the minimum that still does the job** — cheap-by-default routing,
  premium only when the step genuinely warrants it; context kept lean; caching
  exploited wherever the provider supports it.
- **Is trust-first, never destructive** — warn → approve-to-continue, never a
  silent hard kill that loses work. The economy is there to *inform and protect*
  the user, not to fight them.

The goal: whatever you ask, on whatever stack, Manta does it for the lowest
defensible token cost — and can prove it.

### 2. True multi-model, multi-provider — powered by an AI Gateway

Manta is provider-agnostic by design. Through **AI Gateway** it brokers a single,
governed surface over many providers (Anthropic, OpenAI, Google, open models,
and Databricks-served models alike) — with unified auth, rate limiting, usage
tracking, and cost controls.

- **Best model for every job**, not one lab's lineup. A planning step, a coding
  step, an independent review, and a cheap bulk transform can each run on the
  model that's genuinely best for it — across vendors, in one session.
- **The right model for the role** is pinned per agent, and **switchable live**.
- New frontier models become available the day the gateway exposes them — no
  Manta release required.

### 3. Proactive model advice — it tells you when to switch

Manta watches how a session is actually going and **recommends model changes
based on usage**, for example:

- "You're burning premium tokens on boilerplate — drop to a cheaper model for
  this loop."
- "This debugging step keeps failing on the fast model; escalate to a stronger
  reasoner."
- "This task is large and cost-sensitive; here's the cheaper/stronger trade-off."

The user always decides; Manta makes the smart move obvious and one keystroke
away. Over time this becomes closed-loop: routing that learns from outcomes, not
just keywords.

### 4. Addressable agents and long-running tasks — `@{agent}`

Agents are real workers, not modes. You can:

- **Address a specific agent** with `@{agent}` and hand it a **long-running
  task** ("@research crawl these docs and summarize", "@swe land this refactor
  and run the suite"), then keep working while it runs.
- **Create, edit, and delete** your own agents — each with its own model, tools,
  enforced permissions (read-only, allow/deny, per-path filesystem, approval),
  durable memory, and budget. The registry is the single source of truth.
- **Trust the boundaries** — they're *enforced* at the tool-call layer, not just
  asked for in a prompt. A read-only agent simply cannot write or run commands.

### 5. The Chief of Staff — see everything, collect everything

A primary **chief-of-staff agent** orchestrates the others and is your single
pane of glass:

- **Full observability** — see what every agent is doing, in flight: their
  tasks, tool calls, token spend, and status.
- **Output aggregation** — pull results back from any delegated or long-running
  agent through the chief of staff, without context-switching into each one.
- **Smart delegation** — routes work to the right specialist (planning, build,
  review, research, …) deterministically where it matters, and reports back.

The chief of staff turns "a bag of agents" into a coordinated team with a clear
front door.

### 6. Databricks-native power — a superpower, not a requirement

When you *are* on Databricks, Manta is uniquely strong: Unity Catalog
catalog/lineage, governed SQL execution, jobs/DAB/pipeline deployment, and
system tables — all bounded by your UC permissions, and model access brokered
through AI Gateway. When you're **not**, none of it gets in your way. Databricks
is the place Manta is most powerful, never the place you're trapped.

### 7. Reliability as a feature

Manta enriches an upstream runtime through a single, well-guarded seam. If that
seam ever breaks, Manta **falls back to a clean launch and still starts every
time**. Upstream versions are pinned; a preflight verifies every integration
point; contract tests fail CI — not users — when something moves.

---

## Trajectory

- **Origin** — a budget-aware, multi-model role pipeline (router / planner /
  builder / reviewer). Recorded in ADRs 0001–0006.
- **ADR 0007** — stop maintaining a parallel pipeline; become a thin,
  reliable launcher over the upstream interactive runtime.
- **ADR 0008 (current)** — re-introduce the valuable parts (creatable enforced
  agents, the token economy, native tools) as an *owned control plane on top of*
  the runtime, not a fork.
- **Next** — the vision in this document: provider-agnostic model brokering via
  AI Gateway, proactive model advice, `@{agent}` long-running tasks, and the
  chief-of-staff observability layer — plus enterprise/fleet management (central
  registry, per-user budgets, audit trails, RBAC) and a headless/CI/SDK path so
  agents run the same enforced way in automation as in the terminal.

---

## In one sentence

**Manta aims to be the coding agent you reach for by default — anywhere, on any
stack — because it always spends your tokens wisely, always runs the best model
for the job from any provider, tells you when to change, and lets you command a
team of real agents through a chief of staff that shows you everything.**
