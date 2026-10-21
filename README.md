# Manta

**A token-smart coding agent with a real team behind it.** Manta runs the
right model for every job, tells you when to switch, records reported model
usage — and lets you hand long-running work to named agents you
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
   specialists escalate when the role warrants it. A shared request collector
   records main, specialist and helper calls in a local ledger, including
   reported reasoning/cache usage, latency and call purpose. An advisor
   recommends model switches based on how the session is *actually going*, and
   `manta receipts` estimates savings when prices are known, with the
   counterfactual labelled as such. In-session, `/cost` shows the
   conversation's known spend, price completeness, and the daily budget.
2. **Real agents, real boundaries.** `planning`, `swe`, `review`, and `chief`
   (plus any agent you create) have model pins, tool allow/deny lists,
   per-path filesystem rules, approval gates, budgets, and durable memory —
   enforced through the compiled runtime's tool dispatch. Read-only roles deny
   known write/execute effects and tools with unknown effects; policy also
   applies when JavaScript dispatches tools. Managed tasks checkpoint human
   decisions; other unattended runs fail closed unless explicitly pre-approved. This is an agent
   tool boundary; use a sandbox for operating-system isolation.
3. **Work you can walk away from.** `@swe land the refactor &` returns a task
   id immediately; the work runs detached, survives your session, accepts
   mid-flight steering (`manta task send`), notifies your desktop when it
   lands, and reports into `/tasks`. Detached tasks use separate Git worktrees
   or directory snapshots. Review their diff, tests, artifacts and spend before
   explicitly integrating changes. The `chief` agent fans work out and collects it for you.

Use `/agents` to author specialists and `/tasks` (or `/team`) to assign work,
resolve input/approval requests, and review changes. Detached tasks disable
shell execution by default; configure an agent's `task_shell_allow_list` to
enable its test commands. See the [visible agent team guide](docs/20-visible-agent-team.md)
for the complete workflow and isolation/recovery contract.

And underneath: **reliability as a feature**. Manta enriches the upstream
[`deepagents-code`](https://pypi.org/project/deepagents-code/) runtime through
a contract-tested integration. Missing required controls stop construction with
a diagnosis; optional presentation adapters can degrade with a warning.
`manta doctor` checks adapter health and distinguishes it from validation of a
compiled graph. `manta doctor --probe` exercises a bounded two-request tool loop
for each configured target in the selected workspace.

## Install

**v0.4.1 fixes Gemini 4 Argon coding-tool requests and streamed tool
signatures**, retaining v0.4.0’s [dependable foundations](docs/19-dependable-foundations.md)
and [visible agent team](docs/20-visible-agent-team.md): author specialists,
assign isolated tasks, answer durable approval requests, and review changes
before integrating them. See the [patch release notes](docs/releases/v0.4.1.md)
for exact qualified targets. Measured model efficiency remains the next milestone;
this release makes no comparative model-quality or savings claim.

**v0.4.1 requires Python 3.12+** and is distributed through
[GitHub Releases](https://github.com/althrussell/manta-code/releases).
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then install the tagged release with its qualified dependency constraints:

```bash
uv python install 3.12
uv tool install --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.4.1/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.1"
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
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.4.1/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.1"
```

A tag-pinned installation stays on that tag when you run `uv tool upgrade`.
Since v0.4.0, Manta provides `manta update --check` and
`manta update --version vMAJOR.MINOR.PATCH` to check and install a complete Manta
release with its matching constraints. `/update` supplies the same workflow
inside the session; editable checkouts retain deliberate Git/lockfile updates.
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
manta task resume <id>                 # explicitly continue a budget-paused task
manta receipts                         # what did this week cost — and save?
```

## In the session

Everything below happens inline, without leaving the conversation.

**Talk to your team**

| Type | What happens |
| --- | --- |
| `@swe fix the failing tests` | Deterministic delegation to that agent — its model pin, permissions, and budget apply. Autocomplete suggests agents after `@` at the start of a message. |
| `@swe fix the failing tests &` | Same, but **detached**: you get a task id back instantly, keep working, and your desktop pings when it lands. |
| `make a plan for the migration` | A conservative intent matcher routes fresh planning requests to the read-only `planning` agent (opus); `MANTA_AUTODELEGATE_PLANNING=0` turns it off. |
| "remember that we never use mocks" | The agent saves it with `manta_remember` (redacted, durable) and future sessions start warm. |
| "check on task ab12 / cancel it / tell it to skip the docs" | The orchestrator and `chief` carry the task tools (`manta_task_status`, `manta_task_send`, …) — manage background work conversationally. |

**Slash commands** (Manta's, on top of the runtime's)

| Command | What it shows |
| --- | --- |
| `/cost` (or `/spend`) | **This session's spend**: per-agent totals, the most recent model calls with per-call cost, today's total vs your daily budget. Works even while the agent is busy. |
| `/agents` | Create, clone, edit, persistently disable, inspect and test specialists. Apply reload safely, switch the primary agent or assign an isolated task. |
| `/tasks` (or `/team`) | Inline and detached tasks, parent/child links, dependencies, logs, input/approvals, steering, cancellation, retry, results and explicit integration/discard. |
| `/model` | Lists all discovered Unity Gateway services and legacy chat endpoints for the active workspace, plus configured targets. Search exact model names and press Enter to switch using Databricks authentication; direct-key providers are excluded. |
| `/effort` | Selects the exact service/protocol's supported effort values. The model card shows known context/output limits, capability evidence and unknowns. |
| `/auth` | Databricks workspace picker on top, provider API keys below; `Tab` moves between the sections. |
| `/update` | Checks stable Manta releases and shows the complete tagged install action. |

Plus the runtime's own: `/clear` (fresh thread), `/threads` (resume any
conversation), `/tokens`, `/copy`, `/editor`, `/skill:<name>`, `@file` to
inject file contents, `!cmd` for shell, `Shift+Tab` to toggle auto-approve —
`/help` lists everything.

**Guardrails you'll meet inline**: approval prompts before `swe` writes or
executes; a checkpoint before another request once an observed agent or daily
budget is exhausted; advisory notes ("> **Manta advice:** …") appended to answers when
you're burning premium tokens on routine work or a cheap model keeps failing.
Checkpoint mode also pauses unattended tasks, preserving their thread for
`manta task resume`. Explicit advisory mode logs the limit and continues.
These are checks between requests, so a request may exceed a limit; unreported
usage or unknown prices cannot establish a hard dollar ceiling.

**Use the inherited runtime**: `/goal` and `/rubric` manage completion criteria;
`/offload` and `/context-doctor` help inspect context pressure; `/auto` exposes
classifier controls, and `/btw` asks a side question. `/help` documents the
installed runtime's exact syntax. From the shell, `manta runtime --help`,
`manta runtime mcp --help` and `manta runtime plugin --help` expose advanced
commands, including groups shadowed by Manta. Skills, hooks, ACP and remote
sandboxes remain runtime features with their own configuration and constraints.

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
`manta_remember` and recall them next session. Notes use the compiled agent's
configured namespace; shared namespaces are explicit, and `memory=false` or
`--no-memory` disables runtime recall and note tools. These namespaces are
agent-scoped, so use separate agents/namespaces for unrelated projects.

Role instructions are generated into `~/.deepagents/<agent>/MANTA_ROLE.md`;
the neighboring `AGENTS.md` holds editable learned context and survives sync.
Old mixed files are backed up before migration, and deleted managed profiles
are archived. Legacy SQLite notes remain inspectable but are marked
legacy/unassigned and excluded from automatic recall. See the
[foundation guide](docs/19-dependable-foundations.md) for ownership and limits.

For the complete create → assign → answer → review → integrate
workflow, CLI/SDK examples, context choices and qualification commands, see the
[visible agent team guide](docs/20-visible-agent-team.md). Inline delegation
shares the parent checkout; detached tasks isolate changes. Forked detached
tasks receive an explicit visible conversation text snapshot.

## Command reference

```bash
manta                       # interactive session (passthrough: -r, -a <agent>, -M <model>, …)
manta run "task"            # one-shot headless run (CI-safe: bounded, enforced)
manta doctor [--probe]      # preflight; --probe live-tests every pinned model
manta runtime <command>    # upstream commands/help, including shadowed groups
manta update [--check] [--version vMAJOR.MINOR.PATCH] # qualified Manta releases
manta init                  # write .manta/config.toml

manta agents                # list agents + enforcement   (show/create/edit/delete/sync/import)
manta agents set-model <name> <model>      # next session or /restart; workspace-validated
manta agents memory <name>  # inspect an agent's durable memory

manta task submit <agent> "..." [--allow-asks]   # detached background task
manta task list|status|output|send|cancel|resume|retry # the task lifecycle
manta task review <id>                     # observed diff, tests, artifacts, spend
manta task respond <id> --pending-id <token> --decision approve # exact displayed request
manta task integrate <id>                  # explicitly apply the reviewed diff
manta task discard <id>                    # remove the isolated task checkout
manta status                # chief-of-staff pane: tasks, events, audit, spend

manta cost [--by agent|model|task] [--breakdown] [--advise]   # (in-session: /cost)
manta budget | manta watch  # spend windows, live refresh
manta receipts              # spend vs all-premium counterfactual + advisor stats
manta pricing               # effective exact-target rates and provenance
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
daily_max_usd = 25.0        # optional: checked between requests
mode = "checkpoint"        # checkpoint (default) | advisory

[pricing."my-finetune"]     # optional: price private endpoints
input  = 2.0                # USD per 1M tokens
output = 8.0
source = "workspace contract" # optional provenance for your verified rates
as_of = "2026-10-20"
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
[qualification results](docs/15-unity-gateway-qualification.md). The
[model control qualification](docs/18-model-capability-qualification.md) records
exact effort/context evidence; the
[foundation tracker](docs/19-dependable-foundations.md) records implementation
and qualification for this milestone.

Env toggles: `MANTA_AGENT_ADDRESSING=0`, `MANTA_ADVICE=0`,
`MANTA_SWAP_RESUME=0`, `MANTA_NOTIFY=0`, `MANTA_HOME=<dir>`.

## How it's built

Manta is an **owned control plane on top of `deepagents-code`** — not a fork
(ADR 0007/0008). One contract-tested seam injects the compiled agents,
policy/economy/advice middleware, and Databricks tools. Pinned upstream
versions, compiled tool manifests and `manta doctor` check the integration.
Required policy/accounting/checkpoint adapters fail closed; optional UI failures
are reported. Everything is local-first:
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
