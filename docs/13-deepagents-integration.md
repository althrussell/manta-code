# deepagents-code Integration

Manta uses the upstream
[`deepagents-code`](https://pypi.org/project/deepagents-code/) TUI, adopted in
[ADR 0007](adr/0007-adopt-deepagents-code-tui.md), and injects its agent,
policy, economy, and task control plane through the runtime seam described in
[ADR 0008](adr/0008-owned-agent-control-plane.md). Upstream provides the
terminal UI, approval interaction, sessions, skills, and base model picker.
Manta provides the Databricks model adapter and the enforced agent behavior.

The current runtime pins `deepagents-code==0.1.86` and `deepagents==0.7.28`,
which require Python 3.12+ together. Databricks transport uses the official
`langchain-openai` adapter plus the Databricks SDK. The former
`databricks-langchain`/`databricks-openai` dependency chain was removed because
its MCP 1.x requirement conflicts with the runtime's MCP 2.x requirement.

## How it wires together

The launcher, model adapter, and runtime hooks form the integration boundary.
Manta's owned server graph entry point installs required control adapters before
upstream graph construction, independent of the selected model provider. Required
construction is atomic: an integration failure stops the graph rather than
silently starting an unenforced agent. Presentation-only adapters may warn and
degrade. Doctor distinguishes available adapter seams from a compiled graph's
effective control manifest; a preflight that has not built a graph reports the
manifest as pending.

### `src/manta_code/dcode.py` — the launcher

- `ensure_dcode_config(endpoints)` idempotently merges a `databricks` provider
  into `~/.deepagents/config.toml`:

  ```toml
  [models.providers.databricks]
  class_path = "manta_code.databricks_chat:MantaChatDatabricks"
  models = ["system.ai.gpt-6-luna", "existing-team-chat"]
  ```

  User settings in that file are preserved. Manta tracks its own inventory
  entries in `[manta.databricks_inventory]`; workspace refresh replaces owned
  entries while retaining user registrations. A default migrates automatically
  only when ownership was recorded and its value is unchanged. `deepagents-code`'s
  `create_model("databricks:<target>")` then instantiates
  `MantaChatDatabricks` directly — **no fork of its model registry**.
- `build_launch_env(profile)` aligns `MANTA_PROFILE` and
  `DATABRICKS_CONFIG_PROFILE` with the resolved profile. It also exports
  `MANTA_DATABRICKS_POLICY`: nonsecret target settings, preserved provider
  defaults, request tags, and discovered capability evidence. Credentials and
  client objects stay out of that payload. This carries the same construction
  policy to the server and background workers. Original cwd/project-root hints
  are carried separately so local config and detached execution use the intended
  project rather than the runtime server's directory.
- `build_dcode_argv(default_endpoint, passthrough)` builds
  `python -m manta_code._boot -M databricks:<target> <passthrough…>` (the
  default `-M` is injected only for a new session without an explicit model).
  Resuming does not overwrite the saved session model.
- `launch(...)` provisions config + env and `os.execvpe`s into the TUI so the
  terminal is handed over cleanly.

The config-merge, environment, and argument helpers are tested in
`tests/test_dcode.py` without launching a subprocess. The launcher also prepares
onboarding state and generated agent profiles.

### `src/manta_code/databricks_chat.py` — SDK-authenticated OpenAI transport

The stable class path `manta_code.databricks_chat:MantaChatDatabricks` now
subclasses `langchain_openai.ChatOpenAI`. It constructs the OpenAI clients
lazily and refreshes all Databricks SDK authentication headers on each request,
including OAuth and auxiliary Azure headers. Model construction and listing do
not require credentials.
Databricks requests do not fall back to `OPENAI_API_KEY` or `OPENAI_API_BASE`;
an explicit base URL must have the configured workspace's origin.

| Target / surface | Inference base | Automatic API selection |
| --- | --- | --- |
| Endpoint / `legacy` | `/serving-endpoints` | Chat Completions |
| Service / `unified` | `/ai-gateway/mlflow/v1` | Responses unless exact compatibility evidence or Chat-only metadata selects Chat |
| Service / `openai` | `/ai-gateway/openai/v1` | Responses when supported or metadata is unknown; chat for chat-only metadata |

`providers/targets.py` represents endpoint and service identity separately and
resolves the shared policy. Three-part FQNs are services by default;
`target_kind="endpoint"` disambiguates an actual dotted legacy endpoint.
`model-services/<FQN>` is a management spelling of the same service, not a
registered-model path. Exact inference identifiers are preserved.

Constructor/runtime options override target settings, Manta settings, and
preserved upstream defaults, including semantic aliases such as `api` /
`use_responses_api`. Conflicting options in one layer fail locally. Model
profile limits and tool/multimodal capabilities remain unknown unless supplied
by verified evidence or explicit overrides. Supported API metadata guides the
surface/dialect choice but does not establish those other capabilities.
Services cannot use the legacy serving route. Native Anthropic Messages, native
Gemini, and direct provider-service addressing remain optional extensions;
compatible model services use the unified gateway APIs.

Configured request tags are validated and sent consistently through sync/async,
chat/Responses, constructor, and per-call paths. Explicit values retain
precedence; unrelated per-call headers do not discard constructor tags.

Direct Python construction can explicitly select Responses:

```python
from manta_code.databricks_chat import MantaChatDatabricks

model = MantaChatDatabricks(
    model="system.ai.gpt-6-1-sol",
    use_responses_api=True,
    max_tokens=4096,
    timeout=60,
    max_retries=2,
)
```

The official adapter owns Responses serialization, tool choice, and replay.
Parsed content blocks, IDs, and opaque continuation state remain intact.
Manta's older reasoning-display repair is limited to a serialized JSON string
containing the known reasoning/text block shape. Legacy streamed cumulative
usage repair is retained only on the legacy route; gateway usage follows the
OpenAI adapter.

Exact labs-qualified Gemini services use unified Chat because the gateway's
Responses continuation rejects even raw replay. Narrow compatibility hooks keep
Chat `thoughtSignature` and Responses function-call opaque fields intact through
streaming, v1 conversion, and checkpoints. Gemini omits the unsupported
`stream_options` request field. See [qualification](15-unity-gateway-qualification.md)
for the tested identities and limitations.

The same policy applies to primary models built by Deep Agents' class path,
registry models, explicitly pinned subagents, runtime switches, and background
workers. The adapter preserves the pinned upstream constructor's distinction
between provider defaults and explicit runtime overrides; model-specific Manta
settings therefore are not overwritten by defaults passed through that factory.
`reliability.PATCH_TARGETS` verifies those private integration seams.

### Discovery and on-demand gateway inspection

`providers/discovery.py` independently enumerates legacy chat endpoints and
model services in configured schemas (default `system.ai`). SDK pagination,
including empty intermediate pages, is retained; later failures preserve partial
results. Configured targets outside those schemas receive direct metadata
lookups. Metadata denied, unsupported, and unavailable outcomes are distinct
from confirmed direct-target absence, and none verifies inference permission.
Known embedding-only targets are excluded from the coding picker.

A bounded launch deadline includes SDK initialization, listing, and direct
lookups. In-memory caching is scoped to workspace, profile/auth context, schemas,
and configured identities, and supports explicit refresh. No startup per-service
FULL detail sweep is needed. `manta gateway --target <target>` fetches detailed
routing/fallback and visible policy metadata on demand; missing visible policy
means unknown. Profile switching refreshes inventory and exported capabilities
before restarting the graph.

`manta doctor` reports metadata and credential readiness without an inference or
`current_user` call. `manta doctor --probe` performs at most two inference requests
per distinct configured launch/agent target: request one harmless echo tool call,
then replay its result and require the echoed nonce. This explicitly spends
tokens. It preserves opaque state, uses the selected dialect, disables SDK
retries for the probe, records returned usage, and reports failures without
switching protocols based on error-message text.

### Session protocol and opaque continuation state

`middleware/session_policy.py` checkpoints the effective target's API/surface
and profile/workspace identity through private LangGraph state. Isolated
subagents keep their own state and cannot overwrite the parent's. On resume,
the middleware restores the saved dialect when runtime
options do not explicitly select a new one. Both primary and Manta delegated
agents use this middleware. It rejects a target/provider, profile/workspace, or explicit dialect
change when the history contains opaque reasoning, signed tool state, or item
references that cannot safely be replayed to the new model/API. The message
explains how to continue on the original model or start a fresh thread.

The model-call wrapper order resolves Deep Agents runtime overrides, Manta
model pins and optional routing before protocol restoration and accounting.
Consequently the ledger identifies the target and dialect actually called,
including a restored session protocol. A deterministic planning delegation that
short-circuits before inference does not create a model-usage row.

### Local accounting and estimates

`accounting.py` installs a process-wide public LangChain callback collector for
main, specialist, classifier, summarizer, grader and other helper calls. The
economy middleware supplies agent/thread context; callback metadata supplies
invocation identity and latency. SQLite writes run in a worker, with a flush
barrier before budget checks. The middleware's fallback accounting and callback
observations deduplicate by invocation/request identity.

`agents/usage.py` migrates the SQLite ledger additively. Existing model names,
tokens, and recorded amounts remain intact. New rows add requested target/service,
observed backend when exposed, surface/API, request ID, usage completeness,
cache/reasoning detail, call purpose, latency, and pricing source/version. An echoed service name or
response object ID is not invented backend/request metadata.

Prices prefer exact identities and explicit overrides. Arbitrary service names
and FQNs do not inherit a vendor-family substring rate; unknown rates remain
unknown. Historical family estimates remain limited to legacy endpoints.
Cache rates are explicit instead of deriving Anthropic discounts for every
vendor, and reasoning already included in output is not charged again. The
transport normalizes streaming usage once; economy reads the merged response
without another cumulative/delta transformation.

Reports display known subtotals and completeness when any call is unpriced or
usage is missing. Historical usage completeness remains unconfirmed. Receipts
with incomplete accounting do not claim dollar savings. Unknown charges cannot
count toward client USD caps; economy warns and records that limitation.
`manta pricing` exposes rates and provenance. Budget mode defaults to
`checkpoint`: threshold checks interrupt before another agent request, including
unattended runs. Explicit `advisory` mode logs and continues. Requests already in
flight can exceed thresholds; these local observed-spend checks are separate
from Gateway budgets and billing.
Direct qualification calls use the same `record_model_response` accounting helper.

### Registry-backed agents — two tiers from one source of truth

`deepagents-code` exposes agents in **two** tiers:

1. **Profiles** — a directory `~/.deepagents/<name>/`. These are the
   entries the in-app `/agents` picker lists; selecting one restarts the session
   with that agent as the *primary* loop.
2. **Subagents** — delegation targets the orchestrator dispatches to via the
   `task` tool. Planning itself is the built-in `write_todos` tool on the main
   agent — not a separate agent type.

Manta's registry (`~/.manta/agents/`, the `AgentDef` schema in
`agents/registry.py`) is the single source of truth for **both** tiers. The
built-in plan → build → review division of labour ships out of the box, each
pinned through the versioned `ROLE_TARGETS` table in
`src/manta_code/model_defaults.py`:

| Role | Registry agent | Behavior |
| --- | --- | --- |
| Orchestration | main / `chief` | Coordinates tools and delegation |
| Planning | `planning` | Deliberate planning with read-only enforcement |
| Build / SWE | `swe` | Hands-on implementation with configured approvals |
| Review | `review` | Independent read-only review |

Run `manta agents` or `manta agents show <name>` to inspect current pins.
Default changes apply to Manta-owned built-ins and newly generated config;
saved user agent definitions continue to override the built-in of the same name.
Unowned upstream defaults are preserved and surfaced by doctor for deliberate
migration. Changing the launch default alone does not rewrite agent pins.

**Profile tier (`agents/profiles.py`).** `sync_agent_profiles()` projects every
registry agent (built-ins + user-created) into a top-level
`~/.deepagents/<name>/` so it appears in the picker. The registry owns generated
`MANTA_ROLE.md`; edit the role with `manta agents edit`. Mutable learned context
lives in `AGENTS.md` and survives refresh on `launch()` / `run_headless()`.
Managed sentinels protect unowned profiles of the same name and the base
`agent` profile. Deleted managed profiles are archived in full rather than
erased. Old mixed role/memory files receive a content-addressed backup and
provenance before migration; the launcher reports a one-time notice. The
legacy prompt-only markdown subagents this replaced are archived once, on first
launch, by `clean_legacy_subagents()`. Full managed directories, including
appended learned context, are retained under `manta-legacy-subagent-archive`. Run
`manta agents sync` to regenerate on demand.

**Subagent tier (build hook).** `enrich_kwargs()` compiles each registry agent
into an *enforced* `deepagents` `SubAgent` dict (tool policy, filesystem rules,
memory, budget) and injects them so the orchestrator can delegate to them. The
specialists reuse dcode's prepared general-purpose runtime middleware, preserving
shell/filesystem restrictions, approvals, retry/hooks, memory guards and MCP
timeouts. Effective manifests inspect the actual compiled primary and child
ToolNodes rather than treating declared capabilities as proof of installation.

**Plan requests are auto-delegated (deterministically).** deepagents gives the
orchestrator its own `write_todos` planning tool and its default prompt prefers
planning inline, so *no* orchestrator model reliably delegates "make a plan" to
the `planning` agent on its own — a prompt nudge can't beat a built-in tool.
`middleware/delegation.py` closes the gap without relying on model compliance:
on the base orchestrator (no profile selected), when a fresh human turn is a
plan request (`plan_intent()`), `PlanDelegationMiddleware` short-circuits the
model call and returns a synthesized `task(subagent_type="planning", …)` call,
so the real planning agent runs. It fires only on the first model call of the
turn (the last message is the human plan request), so after the `task` result
comes back it never re-fires — no loop. The intent match is conservative (it
skips "implement the plan…" and references to an existing plan), it's the
outermost orchestrator middleware (no accounting noise for a call that never
runs), and it's fully guarded. Disable with `MANTA_AUTODELEGATE_PLANNING=0` to
fall back to inline planning. Selecting a profile in the picker bypasses this
entirely (if you pick `planning`, it already *is* the planner).

**Top-level enforcement.** When you select a Manta agent in the picker,
`deepagents-code` passes its name to the server as
`DEEPAGENTS_CODE_SERVER_ASSISTANT_ID`. The build hook's `active_agent_name()`
reads it and applies that agent's tool-policy + memory + economy middleware to
the *primary* loop — so picking `review` makes the whole session read-only, not
just a delegated subagent. Economy is attributed to the active profile (with its
budget caps) instead of a separate `orchestrator` instance, so a call is never
double-counted in the ledger.

**Why a resolver shim is needed.** The main agent's `databricks:<target>`
model works because `deepagents-code`'s `create_model` honors the provider
`class_path`. Subagent models take a different path: `deepagents` resolves a
subagent's `model` string via `deepagents._models.resolve_model` → langchain's
`init_chat_model`, which has **no `databricks` provider** — so a pinned
`databricks:<target>` would fail with *"Unable to infer model provider"*.
Importing `manta_code.databricks_chat` installs
`_install_subagent_databricks_resolver()`, which wraps `resolve_model` so
`databricks:` specs instantiate `MantaChatDatabricks(model=<target>)` directly
(mirroring `_create_model_from_class`) and everything else defers to the
original resolver. The server subprocess imports this module to build the main
agent's `class_path` model, so the shim is in place before any subagent is
resolved.

The patch is applied in **two** places. `create_deep_agent` resolves subagent
models through `deepagents.graph`, which does a *top-level*
`from deepagents._models import resolve_model` and is imported (via
`create_cli_agent`'s import chain) **before** `create_model` runs the shim — so
rebinding only `deepagents._models.resolve_model` would miss it. The shim
therefore both patches the `_models` attribute (picked up by the function-local
importers in `middleware.subagents`/`summarization`/`rubric` and any
later-imported module) and rebinds the name on every already-imported
`deepagents` module that still points at the original (`_rebind_imported_resolvers`).

Read-only policy rejects known write/execute effects and conservatively rejects
unknown tool effects. Selecting `review` as the primary profile applies the same
policy as delegation. JavaScript tool proxies re-enter the real ToolNode
middleware, and `@agent ... &` submits through the normal task tool path, so
policy, ASK and audit apply there too. This controls agent tool dispatch;
operating-system isolation still requires an appropriate sandbox.

### Memory ownership and detached context

SQLite notes are bound at compilation to each agent's effective namespace;
read and write paths share that binding. A configured shared namespace is
deliberate. `memory=false` and launch `--no-memory` suppress runtime recall and
note tools. Existing unscoped SQLite notes are backed up, tagged
`legacy-unassigned`, retained for CLI inspection and excluded from automatic
recall. New notes carry namespace/agent/source/time provenance and pass through
the redacting store. Redaction is pattern-based, not a guarantee that arbitrary
sensitive text will be recognized.

Registry memory, profile `AGENTS.md`, skills and session checkpoints remain
distinct stores. Agent namespaces are not automatic project isolation; scoped
search, recall budgets and project-aware memory are later work.

Detached tasks store original cwd, project-root/checkout identity, profile and
workspace, effective model and agent/policy digests without serializing
credentials or arbitrary environment variables. Workers restore that context
and reject drift. Budget pauses persist as `paused_budget`; explicit
`manta task resume <id>` resumes the same saved graph checkpoint after validating
context. This milestone does not add worktree isolation, task dependencies or a
unified approval/task board.

### `src/manta_code/main.py` — argument routing

`main_entry()` + `classify_args()` route Manta's own subcommands through Typer
and send bare invocations and unknown/forwarded flags
(`-r`, `--skill`, `-M …`) straight to the launcher.
Unknown runtime subgroup help follows the same route without discovery,
profile sync or inference. `manta runtime <command>` explicitly reaches runtime
groups, including `agents` and `doctor` that Manta otherwise shadows. Use
`manta runtime --help`, `manta runtime mcp --help` or
`manta runtime plugin --help` to inspect the installed runtime.

## Authentication

`src/manta_code/auth.py` resolves the active profile (`-p/--profile` >
`MANTA_PROFILE` > `DATABRICKS_CONFIG_PROFILE` > SDK default) using unified auth.
Manta does not re-implement
auth; it only resolves the profile name and exports it as
`DATABRICKS_CONFIG_PROFILE` before exec.

## Runtime ownership

Manta owns agent definitions, enforced tool/filesystem policy, budget and usage
accounting, routing/advice, and background tasks. It injects these through the
Deep Agents build hook and middleware, while upstream owns the terminal and
agent execution runtime. The boot shim also adapts branding, authentication,
model selection, and agent switching. These integration seams are checked by
`reliability.PATCH_TARGETS` and `manta doctor` when upstream pins change.
Manta also owns release checks, update installation and restart. `/update`
checks stable Manta GitHub releases; `manta update` installs the selected tag and
its full dependency constraints. Independent automatic dcode replacement is
disabled without rewriting saved dcode preferences. Restart re-enters Manta.
Editable checkouts require deliberate Git/lockfile synchronization. See
[installation and upgrades](16-installation-and-upgrades.md).
Runtime package/extra install, uninstall and dependency-recovery paths refuse
unqualified mutations; optional integrations must be added deliberately against
the matching Manta lock. Complete release updates are serialized per installation.

## Headless / CI / SDK path (`manta run`)

`manta run "<task>"` wraps `deepagents-code`'s non-interactive one-shot mode
(`-n`) with CI-safe defaults: a **bounded `--timeout`** (default 600s), a
`--max-turns` cap (default 50), and quiet/buffered output for clean piping
(`--json text|stream-json` for machine output). The same config/onboarding/
profile sync as `launch` runs first, so Manta's control plane (enforced
agents, token economy + usage ledger) applies headlessly too. `manta run` returns
the runtime's exit code (124 on timeout) instead of replacing the process, so
scripts and CI can branch on it.

`manta watch` gives a live per-agent token/cost view by tailing the local usage
ledger — steering leverage (see where spend is going across parallel agents and
subagents) without forking the upstream TUI.

## Remaining boundaries

- Live qualification is workspace/profile dependent. Metadata visibility alone
  does not verify `EXECUTE`, tool support, a token limit, or availability in
  another workspace. Re-run `manta doctor --probe` for the intended auth context.
- Native Anthropic/Gemini dialect adapters and direct provider-service addressing
  are optional extensions; this integration supports the unified APIs and
  native OpenAI surface. Gateway billing reconciliation is a separate opt-in
  feature and does not require system-table access for ordinary inference.
- Private upstream hooks remain version-sensitive. Keep the exact runtime pins,
  dependency lock, patch-target checks, and offline wire/session contracts
  together when upgrading. `manta run` applies its bounded process timeout
  (default 600 seconds) to headless work.
