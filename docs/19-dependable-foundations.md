# Dependable foundations

Delivered in **v0.4.0**. Scope: packages **1–5 plus baseline repository
evaluations** from the [harness review](17-coding-agent-harness-review.md).
That review remains a snapshot of the earlier source. This record retains the
20–21 October foundation qualification checkpoint and describes the contracts
that now ship alongside the [visible agent team](20-visible-agent-team.md).
See the [release notes](releases/v0.4.0.md) and
[installation guide](16-installation-and-upgrades.md) for the combined release.

The early candidate installation used the Databricks package proxy and a local
wheelhouse. Its public-index-only installation limitation was superseded by
fresh GitHub-hosted CI installs of the locked dependencies on Python 3.12 and
3.14. The historical workflow artifact records the evidence available at its
original checkpoint. Keep the qualified runtime pins together with the release
lockfile; do not substitute an older runtime to make an install resolve.

## Acceptance tracker

| Package | Implemented contract and evidence | Qualification and limits |
| --- | --- | --- |
| 1. Runtime composition and policy | Required adapters and graph composition fail closed. Specialists retain the prepared runtime's restrictions and approvals; primary/child ToolNodes provide a validated tool/control manifest. Tool effects, conservative unknown-tool handling, JavaScript dispatch and background submission share policy/ASK/audit paths. Contracts: [runtime foundations](../tests/test_runtime_foundations.py), [hook](../tests/test_hook.py), [policy](../tests/test_middleware_policy.py). | Full Python 3.12/3.14 suites and independent runtime review passed. Includes real graph composition, direct/JavaScript/delegated policy and durable dispatch regressions. Manifest scope and Auto-mode limitation are described below. |
| 2. Task and memory correctness | Bound agent/shared memory namespaces; disabled memory omits runtime note tools/recall. Role generation preserves learned context. File/SQLite migration backs up legacy data. Detached manifests capture and validate cwd/project/workspace/profile/model/policy identity. Contracts: [context and memory](../tests/test_foundation_context_memory.py), [profile migration](../tests/test_agents_profiles.py), [memory](../tests/test_agents_memory.py). | Both full suites, actual two-server SQLite restart and final live labs same-task/thread continuation passed; see [workflow evidence](qualification/foundation-workflows-2026-10-20.json). Independent review passed. Project-scoped retrieval remains future work; writable-task isolation ships in the visible agent team milestone. |
| 3. Model capability catalog | Exact-target/protocol evidence drives adapter, picker, effort and context/output preflight. Unknowns and documented versus live-qualified facts remain distinct. Contracts: [model capabilities](../tests/test_model_capabilities.py), [session policy](../tests/test_session_policy.py). [Control qualification](18-model-capability-qualification.md) records the observed labs effort matrix and limitations. | Full suites include mounted picker/branding/effort regressions. Large-context/media/schema quality qualification is outside this baseline. |
| 4. Accounting and budgets | One request collector covers main/child/helpers with deduplication, reported reasoning/cache usage, call purpose and latency. Prices retain source/version; unknown cost remains visible. Explicit checkpoint/advisory policy and durable unattended pause/resume. Contracts: [accounting](../tests/test_foundation_accounting.py), [headless budget runtime](../tests/test_budget_runtime.py). | Full suites and independent review passed, including changed-limit/day decisions, rejection and completed-effect replay. Final live continuation and main-plus-rubric request reconciliation passed; [workflow evidence](qualification/foundation-workflows-2026-10-20.json). Unknown prices and single-request overshoot remain limits. |
| 5. Help, updates and runtime health | Runtime subgroup help and an explicit `manta runtime` route. Doctor separates required adapters from pending/validated compiled manifests. Manta owns stable tagged updates, constraints and restart; editable checkouts retain deliberate updates. Contracts: [updates/help](../tests/test_foundation_updates.py), [reliability](../tests/test_reliability.py). | Full suites, independent review, sdist/wheel byte/metadata validation and clean-environment wheel smoke passed. Mounted wheel tests: 30 passed; version/help/required-adapter health and dependency checks passed outside the checkout. These are the original foundation checkpoint results; the combined v0.4.0 release adds the team workflow and its qualification. |
| Repository evaluation baseline | Four disposable coding fixtures with executable acceptance outside the agent repository. A failed process or unchanged files cannot pass. Contracts: [repository graders](../tests/test_repository_evals.py); commands and artifact format in [evals guide](../evals/README.md). | All four live labs fixtures passed; [sanitized results and accepted diffs](qualification/foundation-repository-evals-2026-10-20.json). No model-quality or savings advantage is claimed. |

At the original foundation checkpoint, the integrated offline run passed
**939 tests** on **Python 3.12.8** and
**Python 3.14.5**. Each emitted one upstream `forked subagents` beta notice.
Both environments passed dependency checks; Ruff and `git diff --check` passed.
Independent runtime, accounting/budget, updater/task and documentation reviews
found no remaining source blockers. These results cover this scoped milestone;
they do not qualify every inherited dcode integration or every model capability.

The original foundation candidate packaging used
`python scripts/build_release.py --output-dir <empty-directory> --tag v0.3.0`.
That built local candidate assets without publishing or replacing the v0.3.0
tag. The separate Python 3.12 environment imported
Manta from its installed wheel, and its mounted picker/branding/capability suite
passed 30 tests. The dependency environment was freshly created from the
qualified sources described above; the final candidate wheel then replaced the
earlier candidate there without changing dependencies.

Reproduce the offline suite in a qualified environment, using isolated homes:

```bash
MANTA_HOME=/tmp/manta-foundations-test \
DEEPAGENTS_HOME=/tmp/manta-foundations-test/deepagents \
  python -m pytest -q
python -m ruff check .
```

The checkpoint test starts two real local server processes and requires loopback
access. Reproduce the paid live continuation separately:

```bash
python scripts/qualify_task_resume.py --live --profile labs \
  --output /tmp/manta-task-resume.json
```

This disposable read-only fixture requires a budget pause, process/server exit,
a new runner, the same task and thread, and an exact output marker. The actual
SQLite restart regression also proves the pre-pause read is not repeated.

## User workflows

Create and inspect a specialist from the CLI, then restart the session to use
the rebuilt registry graph:

```bash
manta agents create researcher --describe "Read-only repository researcher"
manta agents show researcher
manta agents sync
```

Inspect generated definitions before trusting natural-language drafting: it
uses local heuristics. v0.4.0 adds an in-app authoring/details workspace with
safe reload through `/agents`; see the [team guide](20-visible-agent-team.md).
Delegate inline with
`@review inspect the parser changes`, or detach with `@swe fix the parser &`.
From the shell:

```bash
manta -p labs task submit swe "Fix the parser and run its tests" --allow-asks
manta task status <id>
manta task output <id>
manta task send <id> "Preserve the public API"
manta task resume <id>
```

`--allow-asks` explicitly pre-approves that unattended task's ASK tools. Omitting
it preserves fail-closed tool approvals. Explicit tool approvals still require
a recorded decision. `resume` continues a budget-paused task using the same
task ID and actual saved checkpoint; the team milestone also adds durable
input/tool-approval decisions. Changed checkout/workspace/agent identity or a
missing checkpoint blocks continuation with a diagnosis. In v0.4.0, detached tasks use isolated
Git worktrees or directory snapshots. Inspect their structured diff, observed
tests and artifacts before explicitly integrating work; the team guide describes
the source-identity and conflict checks.

Inspect memory with `manta agents memory <agent>`. The registry owns the role
and generated `MANTA_ROLE.md`; profile `AGENTS.md` is mutable learned context.
SQLite note reads/writes share the compiled namespace. Legacy unassigned notes
remain inspectable and are excluded from automatic recall. Agent namespaces are
not automatic project scope; configure separate namespaces/agents for unrelated
projects. New note writes are pattern-redacted, with source/agent/time metadata.
Session history and skills remain separate from these notes.

The process-local runtime manifest validates tool/control composition for the primary
and inspectable inline children. It explicitly lists opaque/precompiled children
whose internals were not inspected. v0.4.0's `/agents` details additionally
show effective model controls, memory, skills, policy and budget; launch-specific
tool availability still depends on the prepared runtime. Harness policy does not
provide an OS sandbox. Otherwise-gated internal JavaScript calls in Auto mode require human approval because
there is no model-visible proposal for the classifier to review; explicit ASK
continues to apply in every mode.

Use `/model` for the active workspace's Gateway inventory and `/effort` for the
selected service/protocol's supported controls. The model card distinguishes
known limits, unknowns and qualification. Context preflight includes messages,
instructions, tool schemas and reserved output; media and opaque state can
remain unmeasured. The [capability report](18-model-capability-qualification.md)
explains exact evidence and the observed Gemini control limitation.

Use `/cost`, `manta cost`, `manta pricing` and `manta receipts` to inspect local
usage and known prices. Missing usage/rates remain unknown. Budgets default to
`checkpoint`; explicit `[budget] mode = "advisory"` continues after recording a
threshold. Checks happen between requests and cannot enforce unknown charges or
prevent a single request from exceeding a threshold. Gateway billing remains
authoritative. An agent-budget approval covers that agent's configured token/USD
budget in the thread. Daily approval is separate and applies to the displayed
UTC day. Changed budget configuration requires fresh consent.

A live GPT-6.1 main-plus-rubric run satisfied its acceptance criteria and recorded
both requests. Claude Sonnet Responses rejects forced tool choice, so dcode
ToolStrategy graders/goal criteria on that exact protocol are not qualified; use
a qualified helper such as `databricks:system.ai.gpt-6-1-sol`. This does not
restrict its qualified ordinary tool loop or effort controls.

Use `manta runtime --help` for advanced inherited commands, including goals and
rubrics, context offload, classifier controls, MCP/plugins, hooks, ACP and
sandboxes. `manta runtime doctor` reaches the upstream group, while
`manta doctor` reports Manta's own integration health. `/update` checks Manta
releases; `manta update --version <stable-tag>` installs the complete tag plus
matching constraints after exiting the session. See the
[installation guide](16-installation-and-upgrades.md).

## Further milestones

Packages **6–8** now ship in the [visible agent team](20-visible-agent-team.md):
agent authoring/details and typed effort/output controls; isolated tasks and
structured results; and a unified task/approval board with dependencies and
durable orchestration.

Package **9, measured model efficiency**, remains future work. It includes
searchable/editable/forgettable project-scoped memory, broader qualified
media/document/schema workflows and comparative model-quality, latency and cost
evaluations. The baseline fixtures establish executable repository acceptance;
they do not establish model superiority or measured savings.
