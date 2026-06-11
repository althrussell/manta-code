# Manta

**A token-smart coding agent with a real team behind it.** Manta runs the
right model for every job, tells you when to switch, accounts for every
token — and lets you hand long-running work to named, *enforced* agents you
can steer mid-flight, coordinated by a chief of staff.

Databricks is Manta's home and power source — models brokered through
gateway-governed serving endpoints, Unity Catalog and Jobs tools bounded by
your permissions — but never a cage: point it at any stack, configure any
provider, and it stays just as sharp. ([VISION.md](VISION.md))

```text
▗▌  ▄▄▄  ▐▖
▝▜██▀▀▀██▛▘
  ▝▄ ▀ ▄▘
```

## Why Manta

The incumbents proved the interactive coding-agent UX. Manta bets on the three
things they treat as afterthoughts:

1. **The token economy is the product.** Routine turns run on a cheap model;
   specialists escalate only when the role warrants it. Every call lands in a
   local ledger with cache-hit and scaffolding-vs-work splits, an advisor
   recommends model switches based on how the session is *actually going*, and
   `manta receipts` shows what cheap-by-default saved you — with the honest
   counterfactual labelled as such.
2. **Real agents, real boundaries.** `planning`, `swe`, `review`, and `chief`
   (plus any agent you create) have model pins, tool allow/deny lists,
   per-path filesystem rules, approval gates, budgets, and durable memory —
   **enforced at the tool-call layer**, not requested in a prompt. A read-only
   reviewer *cannot* write files, full stop. Ask-gated tools pause for a human
   interactively and **fail closed** in unattended runs.
3. **Work you can walk away from.** `@swe land the refactor &` returns a task
   id immediately; the work runs detached, survives your session, accepts
   mid-flight steering (`manta task send`), notifies your desktop when it
   lands, and reports into one pane (`manta status`). The `chief` agent fans
   work out and collects it for you.

And underneath: **reliability as a feature**. Manta enriches the upstream
[`deepagents-code`](https://pypi.org/project/deepagents-code/) runtime through
a contract-tested seam (18 verified patch targets); if anything moves, Manta
falls back to a clean vanilla launch and *says so*. `manta doctor --probe`
even live-tests every pinned model in the real agent loop before you bet a
session on it.

## Install

Requires Python 3.12+. For Databricks features: the
[Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install)
and a workspace login.

```bash
pip install "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.1.0"
databricks auth login     # once — or skip it and use your own provider keys
manta doctor              # everything green?
```

No Databricks? Manta still runs: configure Anthropic/OpenAI/Google keys in
`/auth` and the Databricks features simply stay dormant. Want to confirm your
workspace's models behave before betting a session on them?
`manta doctor --probe` live-tests every pinned model (~a cent per model).

## Sixty seconds

```bash
manta                                  # launch the interactive session
```

Then, in the session:

```text
@review look at src/api.py and give me findings     # deterministic delegation
@swe fix the failing parser tests &                 # detached background task
make a plan for the cache refactor                  # auto-routed to `planning`
```

And from any shell, even after closing the session:

```bash
manta status                           # tasks × events × spend, one pane
manta task send <id> "prefer the adapter approach"  # steer the running task
manta task output <id>                 # collect the result
manta receipts                         # what did this week cost — and save?
```

## The agents

| Agent | Model (pin) | Boundary |
| --- | --- | --- |
| *(orchestrator)* | `databricks-gpt-oss-120b` | cheap by default |
| `chief` | `databricks-gpt-5-4` | read-only; delegates, tracks, collects |
| `planning` | `databricks-claude-opus-4-8` | read-only; plans, never edits |
| `swe` | `databricks-gpt-5-4` | read-write; approval-gated writes/exec |
| `review` | `databricks-claude-sonnet-4-5` | read-only; a different vendor than the coder, on purpose |

Create your own with `manta agents create <name> --describe "..."` — model
pin, tools, filesystem rules, ask-gates, budget, and memory all live in
`~/.manta/agents/<name>/`, the single source of truth. Switch any agent's
model live with `manta agents set-model` (validated against your workspace's
real endpoints). Agents remember: they save durable learnings with
`manta_remember` and recall them next session.

## Command reference

```bash
manta                       # interactive session (passthrough: -r, -a <agent>, -M <model>, …)
manta run "task"            # one-shot headless run (CI-safe: bounded, enforced)
manta doctor [--probe]      # preflight; --probe live-tests every pinned model
manta init                  # write .manta/config.toml

manta agents                # list agents + enforcement   (show/create/edit/delete/sync/import)
manta agents set-model <name> <model>      # live re-pin, workspace-validated
manta agents memory <name>  # inspect an agent's durable memory

manta task submit <agent> "..." [--allow-asks]   # detached background task
manta task list|status|output|send|cancel        # the task lifecycle
manta status                # chief-of-staff pane: tasks, events, audit, spend

manta cost [--by agent|model|task] [--breakdown] [--advise]
manta budget | manta watch  # spend windows, live refresh
manta receipts              # spend vs all-premium counterfactual + advisor stats
manta gateway               # the AI Gateway surface: providers + governance per endpoint
```

Python, with the same enforcement:

```python
from manta_code import sdk

result = sdk.run("explain the failing test", timeout=300)   # real ledger cost attached
handle = sdk.submit("swe", "land the refactor", allow_asks=False)
handle.send("skip the docs for now")
print(handle.wait(timeout=1800).state, handle.output())
```

CI recipe — the read-only `review` agent on every PR:
[`examples/ci/manta-pr-review.yml`](examples/ci/manta-pr-review.yml).

## Configuration

`manta init` writes `.manta/config.toml` (project) — `~/.manta/config.toml`
(user) merges underneath:

```toml
[interactive]
default_endpoint = "databricks-gpt-oss-120b"   # the cheap session default
extra_endpoints  = ["databricks-claude-opus-4-8", "databricks-gpt-5-4",
                    "databricks-claude-sonnet-4-5"]

[budget]
daily_max_usd = 25.0        # optional: pause-for-approval at the daily cap

[pricing."my-finetune"]     # optional: price private endpoints
input  = 2.0                # USD per 1M tokens
output = 8.0
```

Env toggles: `MANTA_AGENT_ADDRESSING=0`, `MANTA_ADVICE=0`,
`MANTA_SWAP_RESUME=0`, `MANTA_NOTIFY=0`, `MANTA_HOME=<dir>`.

## How it's built

Manta is an **owned control plane on top of `deepagents-code`** — not a fork
(ADR 0007/0008). One contract-tested seam injects the compiled agents,
policy/economy/advice middleware, and Databricks tools; pinned upstream
versions plus `manta doctor` keep the seam honest, and any failure degrades
to a vanilla launch with a visible warning. Everything is local-first:
agents, memory, the usage ledger, tasks, and audit events live in
`~/.manta/` as SQLite/TOML — the enterprise fleet layer (ADR 0009) bolts on
later without a rewrite.

```text
src/manta_code/
├── main.py            # CLI (doctor, agents, task, status, cost, receipts, …)
├── dcode.py, _boot.py # launcher + upstream patch surface (banner, /auth, models)
├── hook.py            # THE seam: enriches create_deep_agent
├── agents/            # registry, factory, defaults, memory, usage ledger
├── middleware/        # policy (enforce), economy (account/budget), advice,
│                      # routing, delegation, address (@agent), inbox (steering)
├── tasks/             # store, executor, runner, events, notify, tools
├── providers/         # model-ref registry + AI Gateway discovery
└── sdk.py             # the Python API
docs/adr/              # 0001–0012: every decision, including the dead ends
```

**Read first:** [VISION.md](VISION.md) →
[CLOSE_GAPS.md](CLOSE_GAPS.md) (the living tracker) →
[docs/adr/](docs/adr/) (0008 control plane, 0010 gap close-out, 0011
steerable tasks/ASK/SDK, 0012 pilot readiness).

## Project history

Manta began as a budget-aware, multi-model role pipeline (ADRs 0001–0006),
became a thin launcher (ADR 0007), then grew back its control plane the right
way — as enforced middleware on the upstream runtime rather than a parallel
pipeline (ADR 0008 onward). The decision records keep the whole story,
including the dead ends.

## License

Apache-2.0. See [SECURITY.md](SECURITY.md) for reporting and
[CONTRIBUTING.md](CONTRIBUTING.md) to get involved.
