# Manta

**A token-smart coding agent with a real team behind it.** Manta runs the
right model for every job, tells you when to switch, accounts for every
token — and lets you hand long-running work to named, *enforced* agents you
can steer mid-flight, coordinated by a chief of staff.

Databricks is Manta's home and power source — models brokered through
Unity Gateway model services or serving endpoints, Unity Catalog and Jobs tools
bounded by your permissions — but never a cage: point it at any stack, configure any
provider, and it stays just as sharp. ([VISION.md](VISION.md))

```text
▗▌  ▄▄▄  ▐▖
▝▜██▀▀▀██▛▘
  ▝▄ ▀ ▄▘
```

## Why Manta

The incumbents proved the interactive coding-agent UX. Manta bets on the three
things they treat as afterthoughts:

1. **The token economy is the product.** Roles select qualified model services;
   specialists escalate when the role warrants it. Every call lands in a
   local ledger with cache-hit and scaffolding-vs-work splits, an advisor
   recommends model switches based on how the session is *actually going*, and
   `manta receipts` estimates savings when prices are known, with the
   counterfactual labelled as such. In-session, `/cost` shows the
   conversation's known spend, price completeness, and the daily budget.
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
a contract-tested seam (28 verified patch targets); if anything moves, Manta
falls back to a clean vanilla launch and *says so*. `manta doctor --probe`
even live-tests every pinned model in the real agent loop before you bet a
session on it.

## Install

**v0.3.0 requires Python 3.12+** and is distributed through
[GitHub Releases](https://github.com/althrussell/manta-code/releases).
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then install the tagged release with its qualified dependency constraints:

```bash
uv python install 3.12
uv tool install --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
uv tool update-shell
manta --version
```

Open a new terminal if `manta` is not yet on your PATH. For Databricks, install
the [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install)
and log in to the workspace you intend to use:

```bash
databricks auth login --host https://YOUR-WORKSPACE --profile my-workspace
manta -p my-workspace doctor
manta -p my-workspace
```

To upgrade an older tagged install, explicitly select the new tag and replace
the tool environment:

```bash
uv tool install --force --upgrade --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
```

A tag-pinned installation stays on that tag when you run `uv tool upgrade`.
Existing user model pins and defaults are preserved; see the
[installation and upgrade guide](docs/16-installation-and-upgrades.md) for pip,
release assets, migration, and troubleshooting, and the [changelog](CHANGELOG.md)
for release notes.

No Databricks? Configure your other provider keys in `/auth`. To qualify the
selected workspace models, run `manta -p my-workspace doctor --probe`; it makes
two inference requests per distinct configured target and incurs model charges.

## Sixty seconds

```bash
manta                                  # launch — opens as the chief of staff
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

## In the session

Everything below happens inline, without leaving the conversation.

**Talk to your team**

| Type | What happens |
| --- | --- |
| `@swe fix the failing tests` | Deterministic delegation to that agent — its model pin, permissions, and budget apply. Autocomplete suggests agents after `@` at the start of a message. |
| `@swe fix the failing tests &` | Same, but **detached**: you get a task id back instantly, keep working, and your desktop pings when it lands. |
| `make a plan for the migration` | Plan requests auto-route to the read-only `planning` agent (opus) — no keyword magic needed, and `MANTA_AUTODELEGATE_PLANNING=0` turns it off. |
| "remember that we never use mocks" | The agent saves it with `manta_remember` (redacted, durable) and future sessions start warm. |
| "check on task ab12 / cancel it / tell it to skip the docs" | The orchestrator and `chief` carry the task tools (`manta_task_status`, `manta_task_send`, …) — manage background work conversationally. |

**Slash commands** (Manta's, on top of the runtime's)

| Command | What it shows |
| --- | --- |
| `/cost` (or `/spend`) | **This session's spend**: per-agent totals, the most recent model calls with per-call cost, today's total vs your daily budget. Works even while the agent is busy. |
| `/agents` | Switch agents — your conversation follows you, the new agent's model pin is applied truthfully, `Ctrl+S` sets your default. |
| `/model` | Opens on Manta's role models and configured targets; `Ctrl+R` includes discovered Unity services and legacy chat endpoints for the active workspace. |
| `/auth` | Databricks workspace picker on top, provider API keys below; `Tab` moves between the sections. |

Plus the runtime's own: `/clear` (fresh thread), `/threads` (resume any
conversation), `/tokens`, `/copy`, `/editor`, `/skill:<name>`, `@file` to
inject file contents, `!cmd` for shell, `Shift+Tab` to toggle auto-approve —
`/help` lists everything.

**Guardrails you'll meet inline**: approval prompts before `swe` writes or
executes; a pause-to-continue if an agent hits its budget cap or your daily
budget; advisory notes ("> **Manta advice:** …") appended to answers when
you're burning premium tokens on routine work or a cheap model keeps failing.

## The agents

| Agent | Model (pin) | Boundary |
| --- | --- | --- |
| *(orchestrator)* | `system.ai.gpt-6-luna` | general coordination |
| `chief` | `system.ai.gpt-6-luna` | read-only; delegates, tracks, collects |
| `planning` | `system.ai.claude-opus-5-5` | read-only; plans, never edits |
| `swe` | `system.ai.gpt-6-1-sol` | read-write; approval-gated writes/exec |
| `review` | `system.ai.claude-sonnet-5-5` | read-only; a different vendor than the coder, on purpose |

Create your own with `manta agents create <name> --describe "..."` — model
pin, tools, filesystem rules, ask-gates, budget, and memory all live in
`~/.manta/agents/<name>/`, the single source of truth. Switch any agent's
model for the next session or `/restart` with `manta agents set-model` (verified
against your workspace's Unity services and legacy endpoints). Agents remember:
they save durable learnings with
`manta_remember` and recall them next session.

## Command reference

```bash
manta                       # interactive session (passthrough: -r, -a <agent>, -M <model>, …)
manta run "task"            # one-shot headless run (CI-safe: bounded, enforced)
manta doctor [--probe]      # preflight; --probe live-tests every pinned model
manta init                  # write .manta/config.toml

manta agents                # list agents + enforcement   (show/create/edit/delete/sync/import)
manta agents set-model <name> <model>      # next session or /restart; workspace-validated
manta agents memory <name>  # inspect an agent's durable memory

manta task submit <agent> "..." [--allow-asks]   # detached background task
manta task list|status|output|send|cancel        # the task lifecycle
manta status                # chief-of-staff pane: tasks, events, audit, spend

manta cost [--by agent|model|task] [--breakdown] [--advise]   # (in-session: /cost)
manta budget | manta watch  # spend windows, live refresh
manta receipts              # spend vs all-premium counterfactual + advisor stats
manta gateway               # Unity services and legacy endpoint gateway metadata
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
default_endpoint = "system.ai.gpt-6-luna"
extra_endpoints = ["system.ai.claude-opus-5-5", "system.ai.gpt-6-1-sol",
                   "system.ai.claude-sonnet-5-5"]

[databricks]
discovery_schemas = ["system.ai"]
service_api = "auto"

[databricks.request_tags]
application = "manta-code"

[budget]
daily_max_usd = 25.0        # optional: pause-for-approval at the daily cap

[pricing."my-finetune"]     # optional: price private endpoints
input  = 2.0                # USD per 1M tokens
output = 8.0
```

Databricks targets can also be exact Unity Catalog model-service names:

```bash
manta -M databricks:system.ai.gpt-6-1-sol
```

Exact service names use the workspace's Unity Gateway. Discovery merges Unity
services and legacy chat endpoints independently, with profile-scoped caching.
Automatic policy prefers supported Responses, uses Chat for Chat-only or exactly qualified services,
and supports explicit native OpenAI routes. New services can be selected without
adding their names to Manta. The shared policy applies to sessions, delegated
agents, routing, and background tasks.

Existing user pins, defaults, and explicit `-M` selections take precedence. Only
Manta-owned inventory/defaults migrate; resumed threads retain their effective
protocol. Incompatible switches with opaque reasoning/tool state require a fresh
thread. Unknown service prices remain unknown and dollar reports show coverage.
See the [configuration reference](docs/10-config-schema.md),
[integration guide](docs/13-deepagents-integration.md), and
[qualification results](docs/15-unity-gateway-qualification.md).

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

The runtime pins `deepagents-code==0.1.86` with `deepagents==0.7.28` and
requires Python 3.12+. Databricks models use the official
`langchain-openai` adapter with refreshable Databricks SDK authentication.
The Databricks LangChain/OpenAI bridge packages are no longer dependencies;
their current MCP requirements conflict with this Deep Agents runtime.

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
