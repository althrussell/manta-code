# Coding agent harness review and uplift plan

Review date: **20 October 2026**. Source snapshot: **`18a5c3a`**, branch
`codex/fix-gateway-picker-branding`. Installed runtime: **deepagents-code
0.1.86**, **deepagents 0.7.28**. This snapshot includes the proposed Gateway
picker and Manta branding fixes in [PR #23](https://github.com/althrussell/manta-code/pull/23);
it is ahead of the published v0.3.0 release. This document preserves the original
assessment and implementation plan; the uplift was not implemented in that
snapshot. See [the dependable-foundations record](19-dependable-foundations.md)
for implementation and validation of packages 1–5 plus the repository baseline.

## Assessment

Manta has a credible coding-agent foundation and substantial functionality from
dcode. It already supports creating named agents, assigning inline and detached
tasks, persistent sessions, durable notes, model routing, approvals, native
Databricks tools, skills, MCP, and plugins. The largest gaps are **correctness,
composition, and discoverability**, followed by durable task orchestration.

It does **not yet consistently expose or qualify the full power of the models
it lists**. Gateway discovery supplies protocol metadata, while verified context
limits are generally missing and Manta's capability profiles are not projected
into the picker and reasoning-effort controls. More autonomy would
also increase the impact of incomplete policy coverage, missing task context,
and fragmented memory. Fix those foundations before expanding autonomous fan-out.

Keep the current architecture: borrow the coding runtime from dcode and own
Manta's agents, Gateway integration, economy, and task control plane. Much of the
uplift is better integration and product presentation, rather than building a
second agent runtime.

### Direct answers

| Question | Answer today |
| --- | --- |
| Can we create agents? | **Yes.** CLI create/edit/delete/import, model pins, prompts, skills, tool policies, memory and budget fields. `/agents` primarily selects an agent; it is not an authoring interface. `--describe` drafts from keyword heuristics rather than a model. |
| Can we give them tasks? | **Yes.** `@swe ...` delegates inline; `@swe ... &`, task CLI commands, conversation tools and the Python SDK submit detached work. We have status, output, cancel, steering and notifications. |
| Is this a coordinated agent team? | **Partly.** There is a chief and real delegation, but detached jobs have no durable project manifest, dependency scheduler, task approval inbox or structured integration step. |
| Do we have memory? | **Yes, in two separate systems.** Manta SQLite notes and dcode's AGENTS.md/skills memory coexist. Namespace binding and managed-file overwrites make the combined behavior unreliable. Session history is a third, distinct form of persistence. |
| Are we surfacing dcode? | **Only partly.** Goals/rubrics, context offload, classifier controls, plugins, hooks, ACP and sandboxes are largely reachable but poorly explained or integrated. Some normal runtime help is broken. |
| Are we using powerful models fully? | **Transport is ahead of the product.** Streaming, tool replay, opaque state and media/schema payloads are supported, but effort, actual context windows, per-agent settings and feature qualification need work. |

Examples of existing interfaces:

```bash
manta agents create researcher --describe "Read-only repository researcher"
manta agents show researcher
manta agents memory researcher
manta task submit swe "Fix the parser and run the tests" -p labs
manta task status <id>
manta task send <id> "Keep the public API compatible"
```

In-session, `@review inspect the parser changes` and `@swe fix the parser &`
are already supported. Inspect a generated agent before use: natural-language
drafting does not establish that its effective tools and permissions match the
description. The profile option is deliberately on `task submit` above because
the global-option defect described below still exists.

## Capability inventory

“Inherited” means the installed dcode implementation is reachable through Manta;
it does not mean every combination has been exercised interactively in this review.

| Harness capability | Current position | Needed uplift |
| --- | --- | --- |
| Repository reads, search, edits, command execution | Inherited tools and execution backends | Consistent tool policy, effect classification and execution containment |
| Agent registry and delegation | Manta native, implemented | In-app authoring, effective capability details, reliable reload, optional context fork |
| Parallel and background work | Inline dcode dispatch plus Manta detached processes | Unified task view, durable context, isolation, concurrency and dependencies |
| Approvals | Manual, classifier-backed Auto and broad auto-approval modes inherited; Manta ASK layer | Consistent primary/child/JS/background behavior, durable pending approvals |
| Session persistence | Inherited thread browser and resume; Manta agent-switch continuity | Qualify interruptions and handoffs; distinguish conversation resume from file rollback |
| Memory | SQLite notes plus inherited AGENTS.md and skills | Correct scope/binding, one user-facing contract, editable and relevant retrieval |
| Goals and acceptance loops | Inherited `/goal` and `/rubric`, helper/grader models | Expose and qualify on Gateway; attach verified outcomes to tasks |
| Context management | Inherited `/context`, `/context-doctor`, `/offload` and offload model | Actual service limits, effective helper model, output reservation and accounting |
| Model selection and effort | Workspace Gateway picker in current snapshot; upstream `/effort` | Capability bridge, working effort choices, per-agent options and model cards |
| Images, documents, structured output | Strong adapter fixtures; inherited image/video composer | Qualified modalities, PDF input UX, schema-constrained headless output |
| Skills, MCP, OAuth, plugins and hooks | Inherited; agent skill paths supported | Coherent help, trust/setup diagnostics and consistent tool policy |
| Editor and remote execution | ACP and sandbox flags forwarded upstream | Document and test supported combinations; interpreter and remote sandbox have compatibility limits |
| Diff review and rollback | Coding tools can inspect diffs; sessions persist | First-class change review, file checkpoints and task worktrees are missing |
| Browser and language-server tools | No native Manta integration found | Optional MCP/plugin recipes; add only where coding evals establish value |
| Web research | Search depends on Tavily configuration; missing-search warning suppressed | Display availability and offer a configured search connector |
| Cost and routing advice | Manta ledger, receipts, per-task attribution and advice | Complete helper-call coverage, exact-service prices, useful reasoning/cache/latency views |
| Updates and diagnostics | Manta releases/locks, doctor and contract checks; upstream updater remains active | Own update/restart paths and unify runtime diagnostics |
| Coding quality evaluation | Unit/transport tests and small answer benchmark | Real repository edits with hidden tests, outcome and economy comparisons |

## Findings and evidence

Evidence labels distinguish **reproduced** offline behavior, **source-confirmed**
coverage defects, and **integration risks** that still need composed runtime tests.
P0 denotes a blocker for stronger enforced-boundary or unattended-autonomy claims;
P1 denotes core correctness and usability work; P2 denotes subsequent enrichment.

### F1 — P0: policy coverage is behind the installed tool runtime

**Source-confirmed.** Read-only policy enumerates a small set of tool names; the
new filesystem delete operation is absent. Arbitrary MCP/custom tools are not
classified by side effect. Filesystem scope checks cover only selected known
tools. Programmatic tool dispatch has a separate path that does not automatically
run the normal Manta interception layer. These are coverage defects, not evidence
of a destructive action performed during this review.

The README's absolute claim that a reviewer cannot write is stronger than the
current implementation supports. Tool-name checks also do not provide an OS
sandbox or a complete filesystem/command capability boundary.

**Change:** use one effective tool manifest with effect metadata, conservative
handling for unknown effects, canonical project/path scope and backend
containment. Apply the same decision and audit contract to direct, programmatic,
delegated and background execution. Reuse upstream metadata/classification where
appropriate; tool annotations alone are not a security boundary.

Sources: [policy](../src/manta_code/middleware/policy.py#L34),
[agent policy claims](../src/manta_code/agents/registry.py#L87),
[defaults](../src/manta_code/agents/defaults.py#L32).
Upstream references: `langchain_quickjs` host dispatch and middleware contract;
`deepagents.middleware.filesystem` tool inventory in the locked environment.

### F2 — P0: specialist composition and fallback can omit required controls

**Integration risk:** dcode decorates its agents with retry behavior, approvals,
cost tracking, hooks, shell restrictions, MCP timeouts and memory guards before
Manta inserts/replaces specialists. Manta compiles a fresh middleware list at a
later seam. Global filesystem-tool restrictions are also applied upstream before
that insertion. The effective behavior of all Manta specialists therefore needs
qualification against global runtime options; symbol-presence tests are insufficient.

**Source-confirmed:** individual policy-building exceptions are swallowed, and
the overall enrichment wrapper can continue after failure. Enrichment mutates
kwargs as it proceeds, so “clean vanilla fallback” is not an atomic guarantee.
Startup patch warnings do not establish that every required runtime control
was installed successfully.

**Change:** compose specialists through a supported upstream preparation path,
or a narrowly versioned adapter that preserves its lifecycle. Build atomically.
Required policy must either be present or prevent the governed task from running;
optional presentation/telemetry may degrade with a visible status. A general
vanilla launch should not masquerade as an enforced named agent.

Sources: [factory](../src/manta_code/agents/factory.py#L124),
[late insertion](../src/manta_code/hook.py#L402),
[skipped policy](../src/manta_code/hook.py#L231),
[fallback](../src/manta_code/hook.py#L457).
Upstream: `deepagents_code.agent._subagent_cli_middleware` and
`_inject_fs_tools_into_subagents` in 0.1.86.

### F3 — P1: effort profiles are disconnected and default context limits are missing

**Reproduced offline.** Five representative services, covering every current
role default, returned an empty upstream picker profile and no available effort choices:
`system.ai.gpt-6-luna`, `system.ai.gpt-6-1-sol`,
`system.ai.claude-opus-5-5`, `system.ai.claude-sonnet-5-5`, and
`system.ai.gemini-4-argon`. Even an explicit Manta profile with reasoning levels
and a 12,000-token input limit reached the adapter but not the upstream effort UI.
Explicit input limits do reach the runtime's context/compaction machinery; the
UI projection defect and the missing default context metadata are separate gaps.

Discovery currently supplies API types; curated evidence covers selected
protocol/tool constraints. Verified context and reasoning profiles are generally
absent. Without a model input limit, the installed summarization middleware uses
a **170,000-token trigger**, observed for all five tested services. That fallback
can exceed a smaller model's window or underuse a larger one.

**Change:** extend the existing target evidence into one shared capability source
for transport, picker, effort, context management and helper models. Key evidence
by exact target, workspace and protocol, with provenance, date and
verified/override/unknown state. Never guess capabilities or prices from a model
family name. Discovery should still list newly available services immediately;
advanced affordances should state what has actually been qualified.

Sources: [discovery](../src/manta_code/providers/discovery.py#L106),
[curated evidence](../src/manta_code/providers/targets.py#L93),
[adapter profile](../src/manta_code/providers/targets.py#L200).
Upstream: `model_config.py`, `reasoning_effort.py` and
`deepagents.middleware.summarization` in the installed versions.

### F4 — P1: agent definitions omit model controls and some tool fields are inert

**Source-confirmed.** An agent pins a model string but has no typed per-agent
reasoning effort, generation-output limit or response schema. Two agents using
the same service cannot express different effort in their definitions.
`databricks_tools` is saved and displayed but is not compiled into runtime tool
selection. Most specialists inherit parent tools; explicitly selecting Manta's
task tool group instead replaces the custom inherited set, with surprising
effects on memory/native tools.

**Change:** resolve each definition to an inspectable effective manifest:
model/API/options, tool groups and exact tools, policy, memory scope, skills,
budget and context mode. Validate every configurable field has a runtime consumer.
Keep per-response output limits distinct from total task budgets.

Sources: [definition](../src/manta_code/agents/registry.py#L80),
[tool compilation](../src/manta_code/agents/factory.py#L94),
[global native bundle](../src/manta_code/hook.py#L426).

### F5 — P1: memory namespaces diverge and profile sync can erase learnings

**Reproduced offline with temporary stores/files.** Recall honors an agent's
configured `memory_namespace`; `manta_remember` writes using the primary session
agent name. A configured `repo-shared` namespace was recalled while the write
went to `audit`. Delegated agents inheriting the tool use the primary agent's
namespace. Disabling recall with `memory=False` does not remove an inherited
memory-write tool.

Separately, dcode treats the profile AGENTS.md as a memory source. Manta generates
that same managed file from the registry on synchronization. An appended learning
was erased on the next sync. Upstream `/remember` writes files/skills independently
of SQLite redaction, so the redaction claim applies only to Manta's note path.

**Change:** separate generated role instructions from mutable learning files;
preserve existing content during migration. Bind read/write tools to the runtime
agent and one explicit memory scope. Give `/remember`, model tools and CLI actions
one documented authority, with clear handling for externally edited files.

Sources: [recall namespace](../src/manta_code/agents/memory.py#L180),
[write namespace](../src/manta_code/agents/memory.py#L321),
[profile sync](../src/manta_code/agents/profiles.py#L149).

### F6 — P1/P2: durable notes need project scope and user control

**Source-confirmed.** SQLite memory is globally namespaced by agent, without
repository/workspace scope. Recall appends up to 20 unqueried notes on model
calls; it has no relevance ranking, recall token budget, deduplication, expiry or
record provenance. CLI supports inspect/add/clear rather than search/edit/forget
individual notes. Conventions from different repositories can mix.

**Change:** explicit user/project/agent/workspace scopes, stable note IDs,
provenance and individual edit/forget/export actions. Start with SQLite FTS and a
bounded relevant recall pack. Add embeddings only if retrieval evaluations
justify them. Show what was recalled, why and how many tokens it consumed.

Sources: [retrieval](../src/manta_code/agents/memory.py#L191),
[prompt injection](../src/manta_code/agents/memory.py#L248),
[CLI](../src/manta_code/main.py#L702).

### F7 — P1: detached tasks do not reliably capture their execution context

**Reproduced with a mocked CLI invocation:** `manta -p labs task submit ...`
does not forward the global profile; the child command has an independent
default. Placing `-p labs` on `task submit` is the current workaround.

**Source-confirmed:** task records omit cwd, repository, branch/worktree,
workspace/profile, parent/session identity and an effective agent/model/policy
snapshot. Processes inherit cwd; in-app submission can originate from a runtime
scaffold directory. If the directory disappears, the runner falls back to home.
This does not reliably establish the user's intended project.

**Change:** resolve child option > root option > environment, and persist a
nonsecret execution manifest. Record the original user cwd and resolve the actual
project root explicitly. Fail
with an actionable blocked state when project/workspace identity cannot be
restored. Never silently substitute an unrelated directory.

Sources: [profile forwarding](../src/manta_code/main.py#L1123),
[record](../src/manta_code/tasks/store.py#L41),
[spawn](../src/manta_code/tasks/executor.py#L124),
[cwd fallback](../src/manta_code/tasks/runner.py#L36).

### F8 — P1/P2: tasks need isolation, durable continuation and verifiable results

**Source-confirmed.** Manta has no worktree manager for parallel writable tasks.
Current states are queued/running/done/failed/cancelled. There is no durable
waiting-for-input/approval state, terminal-task continuation, dependency graph or
bounded scheduler. “Done” means process exit zero; the stored result is the last
8,000 characters of raw logs, rather than a tested artifact/change receipt.

**Change:** isolated worktree per writable task, explicit review/integration,
structured result and test evidence, then blocked/approval/retry/resume states,
bounded concurrency and dependencies. Preserve user changes and require an
explicit integration action. Add file checkpoints/rollback as a separate concept
from thread resume. Reuse upstream remote sandboxes where appropriate.

Sources: [states](../src/manta_code/tasks/store.py#L34),
[result capture](../src/manta_code/tasks/runner.py#L26),
[completion](../src/manta_code/tasks/runner.py#L100).

### F9 — P1: accounting and budget semantics are incomplete for these models

**Source-confirmed:** exact Unity-service price defaults are empty. Unknown
charges are excluded from client USD caps and warnings are emitted. Unattended
budget exhaustion logs an event and continues; it is advisory rather than a hard
spend stop. This differs from the bounded-run impression in parts of the docs.

**Integration risk:** Manta replaces `/cost` with its ledger, whose middleware
captures main/specialist agent calls. Summarization and criteria/grader helpers
can be built outside that hook, while dcode has separate helper accounting.
Complete cost coverage needs a reconciliation test. Do not claim measured savings
from an incomplete ledger or treat model output as a billing record.

**Change:** one request event collector for main, child, classifier, summarizer
and grader calls, with request identity and deduplication. Add dated exact-service
prices or workspace overrides; show price/usage completeness. Offer explicit
advisory versus pause/checkpoint budget policy with token/turn/time fallbacks.
Keep Gateway billing authoritative. Surface reported reasoning/cache tokens and
latency, without claiming access to private model reasoning.

Sources: [service prices](../src/manta_code/agents/usage.py#L88),
[unattended cap](../src/manta_code/middleware/economy.py#L300),
[unknown charges](../src/manta_code/middleware/economy.py#L387),
[/cost replacement](../src/manta_code/_boot.py#L1145).

### F10 — P1/P2: dcode's power is reachable through a fragmented interface

**Reproduced:** `manta mcp --help` exits 2 with “No such command 'mcp'”, although
the normal MCP command route is forwarded. Manta intercepts help globally;
some groups such as agents/doctor intentionally shadow upstream equivalents.

**Source-confirmed:** `/agents` lacks create/clone/edit/detail/test actions.
Creating an agent mid-session can update address completion without rebuilding
the compiled delegation graph. Named Manta agents do not expose upstream's
optional conversation-fork mode. The Ctrl+T panel covers upstream JS-dispatched
subagents; Manta's detached task store is a separate surface.

**Change:** unified help and runtime diagnostics; an in-app capability page;
agent detail/editor with effective manifests and a supported reload; one task
view across inline/detached jobs. Expose existing `/goal`, `/rubric`, `/offload`,
`/context-doctor`, `/auto`, `/btw`, plugins, hooks, ACP and sandbox workflows.
Retain Manta branding and the Gateway-only `/model` experience. Direct-key
provider configuration should stay an explicit separate workflow.

Sources: [argument classification](../src/manta_code/main.py#L75),
[authoring](../src/manta_code/agents/authoring.py#L88),
[static graph](../src/manta_code/hook.py#L128),
[current guide](../README.md#in-the-session).

### F11 — P1: updates must belong to the Manta release contract

**Reproduced read-only:** upstream settings report auto-update enabled and an
upgrade command targeting `deepagents-code`. The current checkout is classified
as installation method `other`, for which automatic installation is refused.
No update was executed and there is no evidence this local lock was changed.

**Static release risk:** supported uv paths target dcode's distribution rather
than Manta's tag/constraints. A successful upstream startup update re-enters
`python -m deepagents_code`, bypassing Manta's boot UI adapters. Databricks
provider import may still install the server hook; this is not proof all Manta
controls disappear.

**Change:** own `/update`, `--update`, startup checks and restart. Report Manta
and bundled runtime versions separately, choose a Manta release and its complete
qualified dependency set, then re-enter Manta's boot path. Test tagged pip/uv and
editable installations without actually updating during unit tests.

Upstream evidence: `deepagents_code.update_check` install-method detection,
upgrade requirement and restart code in `deepagents_code.main` (0.1.86).
Manta's intended contract: [installation guide](16-installation-and-upgrades.md).

### F12 — P2: multimodal and structured output need usable, qualified controls

**Source/fixture-confirmed:** transport tests preserve images, PDF/file payloads,
JSON-schema settings, streaming and opaque reasoning/signature blocks. The
inherited composer supports image/video input, but its path loader does not
provide the corresponding PDF path. Manta's `--json` serializes run output/events;
it does not constrain generated output to a user-supplied schema. Missing
capability profiles also weaken modality-aware validation.

**Change:** qualify exact services for image/document/schema behavior, show
supported input types and size/context estimates, and add explicit document
attachment and output-schema workflows. Preserve signatures and checkpoint
dialects. Existing provider-visible reasoning rendering should be retained;
it is not a missing feature. Native vendor API support and cache options should
be driven by concrete Gateway requirements, not added speculatively.

Sources: [transport fixtures](../tests/test_databricks_transport.py#L47),
[headless JSON](../src/manta_code/main.py#L935),
[session compatibility](../src/manta_code/middleware/session_policy.py#L79).

### F13 — P1: existing evaluations do not establish coding-agent quality

**Source-confirmed.** The benchmark has five answer-generation tasks, graded
mostly through keywords and code fences. Live mode uses the real runtime but
the same graders; nonzero execution with stdout can still produce a gradable
answer. These checks are useful smoke tests, not evidence of successful repo
editing, testing, recovery or economical routing.

**Change:** add temporary fixture repositories with hidden acceptance tests for
bug fixes, refactors, frontend behavior and Databricks bundles. Exercise delegation,
memory, approvals, context pressure and interrupted resume. Treat failed runs as
failed, distinguish partial outcomes, record diff/test receipts, and compare
models/efforts on the same held-out tasks over repeated trials. Report completion,
regressions, latency and complete/unknown cost separately.

Sources: [tasks](../evals/tasks.py#L34), [graders](../evals/graders.py#L32),
[live solver](../evals/live.py#L76), [CI](../.github/workflows/ci.yml).

## Ordered implementation plan

Each work package should be independently reviewable. Run the evaluation track
from the start so product changes have outcome evidence. No calendar estimate is
assigned before the first integration tests establish the actual upstream seam.

| Order | Work package | Dependencies | Acceptance criteria |
| --- | --- | --- | --- |
| 1 — P0 | **Runtime composition and policy contract** | Locked upstream baseline | Required controls fail closed with a visible diagnosis. Primary, selected, inline, programmatic and background agents share policy/ASK/audit semantics. Harmless fake-tool tests cover all tool effects and unknown tools, global shell/filesystem options, hooks, cancellation and error paths. Effective manifests match actual graphs. |
| 2 — P1 | **Task and memory correctness** | 1 for effective agent identity | Global/child/env profiles resolve predictably. Every job records and restores original cwd, project/workspace and nonsecret model/policy identity. Agent/shared/disabled memory behavior matches configuration. Learned notes survive sync/restart; migration backs up and preserves existing files and SQLite data. |
| 3 — P1 | **Shared model capability catalog** | Existing target policy; independent of UI work | Catalog drives adapter and UI consistently. Qualified services offer actual effort choices and context/output limits; unknowns remain explicit. Input plus reserved output fits the verified context limit before dispatch. Switch/resume tests preserve effective settings and opaque state. Catalog invalidates correctly across workspace/protocol changes and distinguishes transport support from live qualification. |
| 4 — P1 | **Accounting and budget contract** | 1, 3 | One recorded event per request across main/child/helpers; all views reconcile. Unknown usage/pricing is visible. Budget mode is explicit and unattended pause/checkpoint works without losing output. Exact-service price provenance or overrides are inspectable. |
| 5 — P1 | **Manta help, updates and runtime health** | 1; can overlap 2–4 | Runtime subgroup help works. Doctor identifies missing required/optional controls. Update/check/restart paths use Manta release/constraints and Manta boot for pip, uv and editable installs. Fresh tagged-install smoke test preserves logo and Gateway-only picker. |
| 6 — P1 | **Agent workspace inside the TUI** | 1–3 | `/agents` can create/clone/edit/disable/test, show effective model/effort/tools/policy/memory/budget and reload safely. Disabled built-ins remain disabled after reload. Newly created agents dispatch without restart mismatch. Existing CLI/SDK share the same resolved definition. Isolated versus forked context is explicit. |
| 7 — P1/P2 | **Isolated tasks and structured results** | 1, 2, 4 | Writable Git tasks use separate worktrees; non-Git work has an explicit isolated-directory/sandbox or serialized execution policy. Parallel changes never silently share the caller's checkout. Results include status, final answer, changed files, diff, artifacts, test commands/outcomes and spend completeness. User can review, integrate or discard while preserving unrelated local work. |
| 8 — P2 | **Unified task board and durable orchestration** | 6, 7 | One view covers inline and detached work, parent/child links, logs, steering, cancel and results. Persist waiting-input/approval, interrupted, retry and resume states. Concurrency and nesting are bounded; dependencies cannot form cycles. Restart and pending-approval tests lose no task identity. |
| 9 — P2 | **Useful memory and model affordances** | 2–4, 6 | Scoped searchable/editable/forgettable memories with provenance and recall token limits. Document/schema inputs validate against qualified capabilities. Display effective effort, context, helper models, reported reasoning/cache usage and truncation. No cross-project recall by default. |
| Continuous | **Coding outcome qualification and guides** | Starts before 1; expands with each package | Hermetic repository tasks gate regressions. Mounted TUI tests cover picker/branding/agents/effort/task state/resume. Project-isolated live Gateway tests cover tool replay, interruption/resume, delegation and a goal/rubric loop. Publish model/effort quality/latency/cost evidence and task-oriented guides. |

Memory migration must be idempotent and retain original provenance. Legacy
unscoped notes should remain visibly legacy/unassigned until the user assigns a
scope; preserving data must not silently introduce cross-project recall. Agent
disable needs persistent enabled state or tombstones, since deleting a user
override currently restores the built-in definition.

Recommended milestone boundaries:

1. **Dependable foundation:** packages 1–5 plus baseline repository evaluations.
   Establish the enforcement, memory, execution and release contracts before
   encouraging larger unattended teams.
2. **Visible agent team:** packages 6–8. A user can create a specialist, inspect its
   capabilities, assign work, see progress, resolve a block and review its changes
   entirely within Manta.
3. **Model-aware efficiency:** package 9 and comparative evaluations. Demonstrate
   that higher effort, long context, caching or delegation improve completion or
   cost on measured tasks before making them automatic routing decisions.

## Design decisions to preserve

- **One owned control plane over dcode.** Prefer upstream extension seams and
  behavioral contract tests over expanding unrelated monkeypatches.
- **Gateway first.** List workspace-visible Unity services without requiring
  direct vendor keys. Metadata visibility does not verify inference EXECUTE
  grants. Keep discovery, invocation access and advanced feature qualification
  distinct in model details.
- **One effective agent manifest.** Reuse it for CLI, TUI, SDK, inline children
  and detached jobs; expose declared versus effective settings.
- **Separate role instructions, session history and durable knowledge.** Their
  ownership and deletion rules differ. Memory has scope and provenance.
- **Task completion is evidence.** Exit zero, a confident answer and a passing
  keyword grader do not establish a successful code change.
- **Reuse runtime power.** Expose goals/rubrics, offload, Auto classifier, skills,
  MCP, hooks, ACP and sandboxes before introducing competing implementations.
- **Keep the local product focused.** Fleet administration, a central server,
  a web dashboard, speculative native vendor clients and mandatory vector storage
  are not prerequisites for this uplift.

## Documentation and release work

Replace absolute README boundary/accounting/budget claims with the tested
contract as the relevant fixes land. Publish guides for “create a specialist,”
“delegate a coding task,” “review and integrate results,” “what Manta remembers,”
and “use model effort and context.” Document inherited advanced commands and
config ownership, including generated files and release/update behavior.

[CLOSE_GAPS.md](../CLOSE_GAPS.md) is historical: its header records landed phases
while its body still describes several of those features as absent. Its old
percentages and dependency statements are not a current scorecard. Use this
snapshot and subsequent implementation acceptance records for the uplift.

Before the next foundation release, require a fresh tagged installation on the
supported Python matrix, dependency consistency, tests/lint/package checks,
mounted TUI smoke coverage and the isolated live Gateway qualification described
above. Qualify both primary and helper models. Attach reproducible evidence to
the release and document unknown service capabilities rather than guessing them.

## Validation boundaries

This review used source/dependency inspection, **71 passing focused existing
agent/task/memory tests**, and benign offline/temp/mock probes of namespace
binding, profile synchronization, CLI profile forwarding, tool manifests,
capability projection, compaction defaults, runtime help and updater detection.
It performed no inference, actual upgrades or destructive policy probes.

The preceding picker/branding change recorded **759 passing tests on Python
3.12 and 3.14**, plus lint checks; that is prior baseline evidence, not a full
suite rerun for this document. Transport fixtures and the earlier labs
qualification remain useful evidence, but do not substitute for the proposed
coding benchmark or composed interactive-stack tests. Upstream source references
in this report refer to the installed locked versions and must be revisited
when those dependencies change.
