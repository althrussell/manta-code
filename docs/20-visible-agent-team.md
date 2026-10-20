# Visible agent team

This milestone implements packages **6–8** of the
[harness review](17-coding-agent-harness-review.md), stacked on the
[dependable foundations](19-dependable-foundations.md). Both milestones ship
in **v0.4.0**, together with the Gateway picker and Manta branding fixes. Follow
the [installation and upgrade instructions](16-installation-and-upgrades.md).

## Create a specialist and assign work

1. Start Manta in the intended project: `manta -p labs`.
2. Open `/agents`. Choose **Create**, or select an existing agent and **Clone**.
   Set the role, model, reasoning effort, response output limit and inline
   conversation mode. A blank model inherits the session model. An explicit
   Gateway model uses a reference such as `databricks:system.ai.gpt-6-1-sol`.
3. Review the advanced JSON for tool groups, allow/deny/ask rules, filesystem
   rules, approvals, memory namespace, skills, budgets and detached shell policy.
   Save the definition. **Validate** compiles its controls offline; it makes no
   inference request and does not establish Gateway EXECUTE access.
4. Choose **Apply reload**. Definitions reload only while the runtime is idle.
   Closing after edits also requests a reload. Concurrent edits or a failed
   reload leave a diagnosis and block new graph runs until you inspect and
   reload the saved definitions.
5. Choose **Assign task**, enter a result and verification instructions, then
   assign it. **Test agent** opens the same form with a harmless read-only
   inspection prompt. Assigning it makes a real model call and a visible task.

**Select** changes the primary session agent; **Base session** returns to the
base `agent` profile. **Disable** persists an override, including for built-ins.
Reloading does not silently bring disabled agents back. Clone creates a separate
memory namespace by default; editing role instructions preserves learned notes.

Details show the resolved definition, pinned/inherited model, capability
evidence, effort/output settings, policy, memory and budget. Tool availability
also depends on the actual launch configuration. Metadata visibility does not
establish inference access. Typed controls validate against the current model
adapter/profile before a request.

### Enable test commands for detached coding tasks

Detached shell execution is disabled by default. Enable the commands a
specialist needs in its advanced JSON, for example:

```json
{"task_shell_allow_list": ["python", "pytest", "git"]}
```

These are dcode command allow-list entries; absolute executable paths work too.
`[]` disables detached shell execution. `["all"]` explicitly enables unrestricted
commands. The frozen task policy overrides inherited shell configuration.
Interactive shell availability follows session launch settings. Read-only,
deny and explicit approval rules still apply. Allow-lists and worktrees are
harness controls, not an OS sandbox.

From a shell, put partial settings in a JSON file and apply them with
`manta agents edit swe --file ./agent-settings.json`. Inspect with
`manta agents show swe --json`, then `manta agents validate swe`.

## Use the team board

Open `/tasks` or `/team`. The board lists inline and detached tasks with agent,
state, execution kind, parent and dependency links. It refreshes automatically
and has **Details**, **Logs**, **Diff** and **Evidence** tabs. At 80×24, action
rows scroll horizontally.

**Assign task** accepts an enabled agent, prompt, optional parent, dependencies
and context. Detached context defaults to the task prompt alone. **Fork** copies
an explicit visible conversation text snapshot; it is not a live shared thread
or a copy of signed/private provider state.

| Kind | Conversation | Filesystem | Controls |
| --- | --- | --- | --- |
| Inline `task` delegation | Definition chooses isolated prompt or forked parent history. | Shares the parent's checkout. Conversation isolation does not isolate files. | Steer the child; decide its exact live request; **Stop parent** stops the parent run and children. Resume opens the parent conversation. |
| Detached task | Isolated prompt, or explicitly supplied visible text snapshot. | Separate Git worktree or directory snapshot. | Steer, cancel, retry, answer persisted input/approval, review, integrate or discard. |

Inline rows retain their identity across checkpoint pauses. Controls act through
their owning parent conversation; another session cannot approve an unrelated
widget or signal an assumed parent PID. **Retry isolated** creates a new detached
task from the saved inline prompt, only from its owning parent in the original
checkout. It does not resume the old inline tool call.

### Decisions and recovery

**Send** delivers steering before the next model request. **Respond** answers
the displayed question. **Approve/Reject** decides the displayed approval
group. Mixed groups require an exact interrupt-ID response mapping, with guidance
in Details. An input answer is never guessed from approval. Decisions bind to
the saved request ID and content; if another client changes it, refresh first.

Detached input, tool approval and budget pauses retain task/thread IDs and
durable checkpoints across runner/server restarts. Continuation uses the stored
checkpoint, rather than replaying the original prompt. Explicit approval rules
still pause when `--allow-asks` pre-approves policy ASK rules.
Mixed hook results and skill-trust denials are retained with the exact saved
group, without rerunning completed hooks or granting new skill trust.

Cancel preserves the workspace. Failed attempts without checkpoints can retry
in the same isolated workspace with an incremented attempt. Retry may rerun
work and is distinct from checkpoint continuation. A dead runner becomes
**interrupted**; resume requires a usable recorded continuation. Otherwise
review before retrying/discarding. Changed agent/model/policy, project identity
or reviewed runtime configuration blocks continuation with a diagnosis.

Dependencies start only after prerequisites succeed. Their summaries are
reference material; patches are not automatically merged. Missing dependencies
and cycles are rejected. Detached concurrency defaults to three, is bounded
from one to sixteen, and uses atomic slot claims across processes. Nesting is
bounded. A durable supervisor advances queued work; opening the board or
running `manta task schedule` also reconciles it after a restart.

## Review and integrate

Git tasks start from the source checkout's **committed HEAD** in an owned
worktree. Source dirty state is recorded and displayed. Staged, unstaged and
untracked source edits are not silently copied; commit anything the task must
see before assigning it. Non-Git projects use an owned directory snapshot and
a baseline for conflict checks.

Choose **Review** on a stopped task. Results include status/final answer,
changed files, complete diff (including task commits), attached artifacts,
observed test commands/exit codes and spend completeness. Test receipts come
from the owned backend's exit metadata for recognized direct test runners.
Model claims and printed “tests passed” are not evidence. Unrecognized shell
groups remain ordinary tool events. Missing test evidence remains missing;
earlier failures retain their attempt number.

Spend separates a task's own requests from recursive team totals. Unknown usage
and prices remain explicit. Successful agent exit alone does not mean tests
passed or the requested change is correct.

**Integrate** requires a stopped task and a reviewed diff; the board offers it
for completed tasks. The CLI/SDK can also integrate a reviewed partial result.
The board passes
the exact reviewed patch fingerprint. Changed patches, conflicting source
content, unsafe paths and unsupported structural replacements are refused.
Git integration preserves the caller's index and unrelated edits. Directory
integration preflights changes and rolls back an application error. Resolve
nested isolated child results before integrating/discarding their parent.
Integration applies files; it does not create a commit or merge a branch.

**Discard** removes a stopped, unintegrated owned checkout after confirmation
and retains its diff/receipts. Integrated checkouts are currently retained for
inspection; there is no automatic post-integration cleanup command. Submodules,
symlink changes and non-Git file/directory replacements require manual
review/integration.

### CLI and SDK

The CLI, TUI and SDK share one definition service and task store:

```bash
manta agents create api-specialist --description "Implement and test API fixes"
manta agents clone api-specialist api-reviewer
manta agents edit api-specialist --file ./agent-settings.json
manta agents show api-specialist --json
manta agents disable api-reviewer
manta agents enable api-reviewer
manta agents test api-specialist

manta -p labs task submit api-specialist "Fix the parser and run its tests"
manta task list
manta task status TASK_ID --json
manta task send TASK_ID "Keep the public interface unchanged"
manta task review TASK_ID --json
manta task integrate TASK_ID --expected-patch SHA256_FROM_REVIEW
manta task discard OTHER_TASK_ID
```

For decisions, inspect `checkpoint.pending_id` and `checkpoint.interrupts`
before responding:

```bash
manta task respond TASK_ID --pending-id PENDING_ID --decision answer --value "Use option A"
manta task respond TASK_ID --pending-id PENDING_ID --decision approve
```

The SDK exposes agent authoring and `TaskHandle.status/output/send/cancel`,
`respond/retry/resume/dependencies`, and `review/integrate/discard`:

```python
from manta_code import sdk

handle = sdk.submit("api-specialist", "Fix the parser and run its tests", profile="labs")
record = handle.wait(timeout=600)  # Also returns when input/approval is pending.
if record.state == "done":
    result = handle.review()
    # Inspect the diff and evidence before explicitly applying it.
    handle.integrate(expected_patch_sha256=result["patch_sha256"])
```

## Qualification and boundaries

Run the live fixture in disposable Git, Manta and Deep Agents directories:

```bash
.venv/bin/python scripts/qualify_visible_team.py --live --profile labs \
  --output docs/qualification/visible-agent-team-2026-10-21.json
```

The [recorded labs result](qualification/visible-agent-team-2026-10-21.json)
passed authoring/sync/dispatch, isolated execution, separate input and write
approval restarts, same task/thread continuation, actual passing pytest evidence,
attached artifact, exact reviewed integration, held acceptance test,
caller-change preservation and persistent disable. It keeps only sanitized
acceptance flags and reads normal SDK profile credentials without copying them.
This qualifies the workflow on `system.ai.gpt-6-1-sol`, not every Gateway model
or unrestricted arbitrary repository work.

Offline tests cover mounted authoring/board controls, actual upstream Textual
decisions, server restarts, inline sync/async graphs, pending-request races,
nested programmatic tool approval, integration conflicts, durable scheduling and
shared CLI/SDK behavior. The final source suites passed **1,126 tests each** on
**Python 3.12.8** and **Python 3.14.5**. The installed wheel passed 41 mounted
CLI/SDK/integration checks outside the checkout. Ruff, dependency consistency,
source/wheel integrity and GitHub CI passed; independent reviews found no
remaining blockers. See the [release notes](releases/v0.4.0.md).

Gateway-only discovery and Manta branding retain their existing regressions.
Searchable/editable/forgettable project memory, broader multimodal affordances
and comparative coding-quality evaluation remain package 9. Existing
project/agent memory controls are preserved. This milestone introduces neither
a central fleet server nor an OS sandbox.
