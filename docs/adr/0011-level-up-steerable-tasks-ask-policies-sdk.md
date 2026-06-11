# ADR 0011 — Level up: steerable tasks, ASK policies, Python SDK

## Status

Accepted. Driven by the [Omnigent competitive review](../competitive/omnigent.md):
adopt its three genuinely better ideas, reject its platform scope. Extends the
ADR 0010 control plane; everything here stays local-first and serializable so
ADR 0009's fleet layer remains additive.

*Revised after a pre-implementation design review against the installed
upstream (langchain 1.3.x / deepagents-code 0.1.7): the steering seam moved
from ``wrap_model_call`` injection to a checkpointed ``before_model`` state
update, the interactive ASK path now rides upstream's proven ``interrupt_on``
HITL instead of a custom tool-call interrupt, and the review exposed a latent
defect in the existing budget/advice pauses (custom interrupt payloads fail
upstream's ``HITLRequest`` validation, and the resume decision was never read,
so reject behaved as approve) — fixed here.*

## Non-goals (explicit, decided by the maintainer)

- **No central server, no web UI, no multi-tenant platform.** Manta stays a
  local-first coding agent; Omnigent can host Manta as a harness if remote
  surfaces are ever wanted (hypothesis, not a commitment).
- **No cross-harness abstraction.** One runtime, deep — not many, shallow.
- **No label/taint information-flow tracking yet.** Revisit for
  prompt-injection defense once the ASK tier has proven out.

## Decision 1 — Steerable background tasks

Today Manta's background tasks are fire-and-forget: submit, poll, collect.
Omnigent's child sessions accept **mid-flight input**. Manta adopts the idea
within its own architecture (no server): a **task inbox**.

- **Store**: a `task_inbox` table in the existing tasks DB — `(id, task_id,
  ts, message, consumed_at)`. Same local-SQLite, never-raises conventions.
- **Send**: `manta task send <id> "<message>"` (CLI), `manta_task_send`
  (in-session tool), and `TaskHandle.send()` (SDK). Only queued/running tasks
  accept messages; a `task_steered` event is recorded.
- **Delivery — the seam**: an `InboxMiddleware` active only when
  `MANTA_TASK_ID` is set, attached **once, at the orchestrator level** (never
  per-subagent, so a subagent can't consume the user's note). It implements
  ``before_model``: drain unconsumed rows and return
  ``{"messages": [HumanMessage("[Steering note …]")]}`` — a **checkpointed
  state update**, so the note lands in the thread history, survives
  interrupts/retries, and shapes every subsequent model call. (A
  ``wrap_model_call`` injection was rejected in review: it affects exactly one
  call, never persists, and drains rows that a `GraphInterrupt` from inner
  middleware would then lose.)
- **Consumption is per-row**: rows are marked consumed by the specific ids
  that were injected, so a message inserted between read and mark simply
  waits for the next model call instead of being silently swallowed.
- **Semantics**: steering is *guidance inside the running task* — it does not
  create new agentic turns. Messages to finished tasks are rejected with the
  terminal state. `manta task status` shows the steering count; steering
  appears in the event feed.

## Decision 2 — ASK policy tier (deny / ask / allow)

Manta's tool policy is binary (deny / allow); deepagents' native
`interrupt_on` HITL covers the approve case but **only interactively** —
headless runs auto-approve (which is why the audit layer distinguishes
`auto_approved`). Omnigent's three-verdict model (DENY > ASK > ALLOW) is
strictly better. Manta adopts ASK split across the two layers that already
work:

- **Schema**: `AgentDef.tools_ask: list[str]`.
- **Interactive sessions**: `tools_ask` is merged into the compiled
  `interrupt_on` map — upstream's proven HITL prompt (same machinery as the
  existing `approval` field; a tool listed in both prompts **once**). No
  custom interrupt from `wrap_tool_call`: review showed raising inside the
  tool node re-executes sibling tool calls' side effects on resume, and
  upstream interrupts before tools run for exactly that reason.
- **Unattended runs**: `ToolPolicyMiddleware` gains an `ask` set and **denies
  ask-gated calls by default** (fail closed) with an actionable message —
  the genuinely new piece; upstream auto-approves here. Resolution order:
  read-only → deny → allow-list → filesystem → **ask** (ask evaluates last so
  a human is never prompted for a call another rule denies anyway).
  - A per-task override — `submit_task(..., allow_asks=True)`,
    `manta task submit --allow-asks`, conveyed via `MANTA_ALLOW_ASKS` —
    lets a human grant blanket pre-approval at submission; such calls run and
    are audited `auto_approved` (the events middleware learns `tools_ask`).
- **Unattended detection** — one explicit marker: `run_headless` always
  exports `MANTA_UNATTENDED=1`, and both the policy layer and the audit layer
  treat `MANTA_UNATTENDED` / `MANTA_TASK_ID` / upstream auto-approve as
  unattended. (Review found `manta run --shell-allow-list …` runs upstream
  with auto-approve *off* while still auto-approving non-shell HITL — ASK
  would have failed open and audits recorded false human approvals.)

### Repair of the existing pauses (prerequisite uncovered by review)

The budget governor's and advisor's `interrupt()` payloads are custom dicts;
upstream's TUI validates non-`ask_user` interrupts against `HITLRequest`
(`action_requests` + `review_configs`) and rejects anything else, and the
resume value was never read (a human's *reject* resumed as if approved).
Both now emit **HITLRequest-shaped** payloads and interpret the resume
decision: budget-approve continues, budget-reject ends the turn gracefully
with a clear message; advice-reject downgrades the rest of the task to the
configured cheap default instead of continuing premium.

### Daily budget guidance

`[budget] daily_max_usd` in Manta config. The economy middleware checks the
ledger's UTC-day total (module-level cached, short TTL, bumped locally on
each write — one extra indexed SUM is negligible next to a model call) and at
the cap pauses once per thread (HITL-shaped approve-to-continue) in
interactive runs; unattended runs log + record a `budget` event and continue
(trust-first: never a silent kill, never a hard stop nobody saw).

## Decision 3 — Python SDK (`manta_code.sdk`)

Omnigent ships a client SDK; Manta's vision lists a headless/SDK path; the
task store and headless runner make it nearly free. One module, thin and
typed:

- `run(prompt, *, agent=None, timeout, max_turns, profile=None) -> RunResult`
  — synchronous one-shot through the enforced headless path; returns output
  text, exit code, and real ledger cost for the run. Cost attribution uses a
  namespaced run tag (`sdk-<uuid>`) passed via a new `env_extra` parameter on
  `dcode.run_headless` — **not** by mutating `os.environ` (concurrent `run()`
  calls must not cross-tag).
- `submit(agent, prompt, *, allow_asks=False, …) -> TaskHandle`;
  `TaskHandle.status()` / `.output()` / `.send(message)` / `.cancel()` /
  `.wait(poll_seconds=, timeout=)`. Status paths reconcile stale tasks the
  same way the CLI does, so the SDK never reports a phantom `running`.
- `tasks(state=None)`, `agents()`, `cost(by=, since_days=)` — read surfaces
  over the stores the CLI already uses. No new daemons, no new state: the SDK
  is a typed façade over `tasks/{store,executor}`, `dcode.run_headless`, and
  `agents/usage`, so CLI and SDK share one source of truth by construction.

## Consequences

- New: `task_inbox` table + per-row consumption APIs; `middleware/inbox.py`;
  `tools_ask` field compiled into both `interrupt_on` and the policy `ask`
  set; `allow_asks` task column + `MANTA_ALLOW_ASKS`; `MANTA_UNATTENDED`
  exported by every headless run; HITLRequest-shaped pauses with resume
  interpretation in economy/advice; `[budget] daily_max_usd`;
  `dcode.run_headless(env_extra=…)`; `manta task send` (+ `--allow-asks` on
  submit); `manta_task_send` tool (and its name joins the task-tool deny
  list); `src/manta_code/sdk.py`.
- `manta task status` and the event feed surface steering; ask outcomes audit
  as `approved` (interactive), `auto_approved` (pre-approved unattended), or
  `denied` (unattended default).
- Behaviour change only for agents that opt into `tools_ask` (no built-in
  uses it yet) — plus the pause repair, which turns two silently-broken
  flows into working ones.
- Future work recorded, not designed: label/taint policies; Manta as an
  Omnigent harness; org-level budget scopes (ADR 0009).
