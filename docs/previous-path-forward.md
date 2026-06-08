# Manta CLI — Path Forward

## Product thesis

Manta CLI should be a multi-model autonomous developer productivity tool that chooses the cheapest capable model for each job, escalates only when justified, and produces higher-quality outputs through explicit role separation.

The core promise:

> Best model. Best job. Best output. No token bill shock.

Manta CLI should not simply be another chat-in-terminal wrapper. It should be a budgeted agent operating system for development work.

## Core architecture

```text
manta CLI / TUI
  ↓
Session Controller
  ↓
Cheap Intent Router
  ↓
Budget + Policy Engine
  ↓
Context Broker
  ↓
Role Pipeline
  ├─ Cheap Responder
  ├─ Opus Planner
  ├─ GPT Builder
  ├─ Test Agent
  ├─ Gemini Code Reviewer
  ├─ Gemini / Claude Security Reviewer
  └─ Release Agent
  ↓
Tool Layer
  ├─ filesystem
  ├─ apply_patch
  ├─ shell runner
  ├─ git
  ├─ test/lint runners
  ├─ dependency/security scanners
  ├─ repo indexer
  └─ MCP tools
  ↓
State Layer
  ├─ .manta/project memory
  ├─ ~/.manta/user memory
  ├─ session event log
  ├─ token/cost ledger
  ├─ context manifests
  └─ generated artifacts
```

## Use Goose for product/runtime patterns

Goose is the strongest open-source reference for local agent product architecture. It provides a mature view of:

- CLI, desktop, and API entrypoints
- MCP extension model
- ACP integration
- config and permission files
- recipes
- subagents
- adversary reviewer pattern
- custom distributions
- provider abstraction
- local session management
- cost and token visibility patterns

Manta should study Goose deeply, but should not fork Goose as the main foundation for v1 unless the goal is to become a custom Goose distribution.

## Use Deep Agents for orchestration

Deep Agents is the stronger programmable harness. It provides:

- model-agnostic agent orchestration
- subagents
- per-subagent model overrides
- per-subagent tools
- per-subagent permissions
- skills
- memory
- context compression
- filesystem backends
- shell execution
- human-in-the-loop controls
- MCP support

For Manta, use the Deep Agents SDK as the first implementation foundation. Treat Deep Agents Code / dcode as a CLI reference, not the final product shell.

## Manta differentiator

The differentiator is not “agent can edit files.” That already exists.

The differentiator is:

```text
Budgeted role pipeline
  +
cheap router
  +
model escalation
  +
scoped skills
  +
context broker
  +
review gates
  +
audit/cost ledger
```

## Model-role strategy

Do not build vendor agents. Build role agents.

Bad:

```text
Claude Agent
GPT Agent
Gemini Agent
```

Good:

```text
Router Agent      → cheap, fast model
Planner Agent     → Claude Opus
Builder Agent     → GPT / Codex-style model
Reviewer Agent    → Gemini
Security Reviewer → Gemini / Claude depending risk
Release Agent     → cheap model
```

## Default pipeline

```text
User request
  ↓
Cheap Router
  ↓
Route decision
  ├─ simple answer → cheap responder
  ├─ small edit → GPT builder
  ├─ normal edit → GPT builder + Gemini review
  ├─ complex feature → Opus planner + GPT builder + Gemini review
  └─ risky/security work → Opus planner + GPT builder + Gemini review + security gate
```

## Routing tiers

```yaml
routes:
  simple_answer:
    pipeline: [cheap_responder]
    max_usd: 0.02
    opus_allowed: false

  trivial_code_change:
    pipeline: [builder]
    max_usd: 0.25
    opus_allowed: false

  normal_code_change:
    pipeline: [builder, code_reviewer]
    max_usd: 1.00
    opus_allowed: false

  complex_architecture:
    pipeline: [planner, builder, code_reviewer]
    max_usd: 3.00
    opus_allowed: true

  security_sensitive:
    pipeline: [planner, builder, code_reviewer, security_reviewer]
    max_usd: 5.00
    opus_allowed: true
```

## Role definitions

### Router

Purpose: classify intent, risk, complexity, required pipeline, and max budget.

Tools: none.

Output: strict JSON.

```json
{
  "intent": "code_change",
  "complexity": "medium",
  "risk": "low",
  "needs_planning": false,
  "needs_review": true,
  "needs_security_review": false,
  "pipeline": ["builder", "code_reviewer"],
  "max_budget_usd": 1.0,
  "reason": "Single feature change with tests"
}
```

### Planner

Model: Claude Opus.

Purpose: architecture, decomposition, acceptance criteria, context manifest.

Permissions: read-only.

Outputs:

- `task_plan.md`
- `acceptance_criteria.md`
- `context_manifest.json`
- `risk_notes.md`

### Builder

Model: GPT / Codex-style model.

Purpose: implementation, tests, local fixes.

Permissions:

- read selected files
- write via patch only
- run allowlisted shell commands
- no network by default
- no git push

Outputs:

- patch/diff
- implementation notes
- test logs

### Code Reviewer

Model: Gemini.

Purpose: correctness, maintainability, style, missed edge cases.

Permissions: read-only.

Inputs:

- git diff
- test logs
- acceptance criteria
- relevant file context
- coding standards

Outputs:

- `review_report.md`
- `required_fixes.json`
- approve/block decision

### Security Reviewer

Model: Gemini or Claude.

Purpose: security review for auth, secrets, injection, access control, data handling, dependency risks, risky commands.

Permissions:

- read-only repo/diff
- allowlisted scanners
- no arbitrary shell
- no write access

Outputs:

- `security_findings.md`
- `security_required_fixes.json`
- approve/block decision

### Release Agent

Model: cheap model.

Purpose: final summary, changelog, commit message, PR body.

Permissions:

- read final diff
- optionally commit after final approval
- never push without human approval

## Context strategy

Manta must not give every model everything.

```text
Router sees:
  user prompt + cwd + git status + minimal repo summary

Planner sees:
  repo map + architecture notes + relevant docs + selected files

Builder sees:
  plan + acceptance criteria + selected files only

Reviewer sees:
  final diff + tests + policies + relevant surrounding code

Security reviewer sees:
  diff + security policies + auth/config/dependency context
```

## Context broker responsibilities

- Build a repo map.
- Select relevant files.
- Estimate tokens before sending context.
- Compress when needed.
- Offload large logs/artifacts to files.
- Store context manifests.
- Refuse prompts that exceed budget unless user approves escalation.
- Track what each role saw.

## Budget system

Track four budget types:

```text
1. total task dollar budget
2. per-role dollar budget
3. per-call input/output token budget
4. max iterations / max turns
```

Every model call writes to the ledger:

```json
{
  "session_id": "...",
  "role": "builder",
  "model": "openai:gpt",
  "input_tokens": 18321,
  "output_tokens": 2711,
  "estimated_cost_usd": 0.41,
  "route": "normal_code_change",
  "budget_remaining_usd": 0.59
}
```

## Cost-control rules

- Opus is never the default.
- Router must run first unless user explicitly forces a model.
- Planner only runs for high complexity, ambiguity, architecture impact, or risk.
- Reviewer sees diffs first, not full repos.
- Builder gets narrow file packs, not broad context.
- Security reviewer only activates when route/risk requires it or user requests it.
- Summaries/offloads are used before stuffing huge logs into context.
- Hard budget caps stop the loop.

## Permission model

Permissions are role-specific.

```yaml
permissions:
  planner:
    read: [repo, docs]
    write: []
    shell: denied

  builder:
    read: [repo_selected_files]
    write: [apply_patch]
    shell: allowlisted
    network: denied

  reviewer:
    read: [diff, repo_selected_files, policies]
    write: [review_report]
    shell: denied

  security_reviewer:
    read: [diff, config, dependency_files, policies]
    write: [security_report]
    shell: allowlisted_scanners
    network: denied

  release:
    read: [diff, reports]
    write: [commit_message, pr_body]
    git_commit: requires_approval
    git_push: denied_by_default
```

## Safety gates

Stop or require approval when:

- budget is exhausted
- destructive file operation requested
- shell command is outside allowlist
- command touches files outside project root
- secret/env file access requested
- network exfiltration risk appears
- dependency install script is untrusted
- review blocks twice
- tests fail after max attempts
- git push is requested

## CLI contract

```bash
manta ask "explain this error"
manta plan "add OAuth login"
manta build "add Stripe webhook handling"
manta fix "resolve failing tests"
manta review
manta security-review
manta run "build the feature" --auto --max-usd 3
manta status
manta diff
manta approve
manta budget
manta models
manta skills
manta doctor
manta resume
```

## Config file shape

`.manta/config.toml`

```toml
[models]
router = "openai:gpt-5-nano"
cheap_responder = "openai:gpt-5-mini"
planner = "anthropic:claude-opus"
builder = "openai:gpt"
reviewer = "google:gemini-pro"
security_reviewer = "google:gemini-pro"
release = "openai:gpt-5-mini"

[budgets]
default_task_usd = 1.00
hard_max_task_usd = 5.00
max_iterations = 3
max_turns = 30
show_cost_always = true

[autonomy]
allow_file_writes = true
allow_shell = "allowlisted"
allow_network = false
allow_git_commit = "approval"
allow_git_push = false

[context]
strategy = "brokered"
auto_compact = true
store_full_history = true
repo_index = true

[review]
code_review_required = true
security_review_on_risk = true
block_on_high_severity = true
```

## Build sequence

### Phase 1 — Core runner

- Deep Agents SDK runner.
- One coordinator.
- Filesystem, patch, shell, git tools.
- Session event log.
- Token/cost ledger.

### Phase 2 — Cheap router

- Structured JSON routing.
- Route-to-pipeline mapping.
- Budget selection.
- Opus escalation rules.

### Phase 3 — Role registry

- Planner, builder, reviewer, security reviewer.
- Per-role model, tools, permissions, context packs.
- Structured outputs.

### Phase 4 — Context broker

- Repo map.
- File selection.
- Context manifests.
- Token estimation.
- Compaction/offloading.

### Phase 5 — Review gates

- Gemini code review.
- Security review.
- Required fixes loop.
- Final approval gate.

### Phase 6 — Safety and autonomy

- Shell allowlist.
- Sandbox mode.
- Adversary reviewer.
- Permission prompts.
- Hard stop conditions.

### Phase 7 — Developer UX

- Polished CLI/TUI.
- Status timeline.
- Cost display.
- Diff viewer.
- Resume/fork/export.
- Non-interactive CI mode.

### Phase 8 — Evals

- Benchmark tasks.
- Track success rate.
- Track review quality.
- Track cost per successful task.
- Compare model-role combinations.

## Repo strategy

### Deep Agents

Use as dependency and harness foundation.

Do not rely only on dcode’s markdown subagent mechanism for Manta’s scoped roles. Use the SDK so subagents can have explicit tools, models, permissions, skills, and structured outputs.

### Goose

Do not fork for v1 unless you want a custom Goose distribution.

Study and borrow:

- configuration model
- recipes
- MCP/ACP patterns
- subagent UX
- adversary reviewer
- session management
- cost display
- custom distribution architecture
- REST/API separation

## Final recommendation

Manta CLI should be:

```text
Deep Agents SDK as harness
+
Goose-inspired local runtime/product patterns
+
Manta-specific budget router
+
role-based multi-model pipeline
+
scoped skills and permissions
+
context broker
+
review/security gates
```

This gives Manta a credible wedge against Claude Code, Codex CLI, Cursor, and Goose:

> It does not just use the most powerful model. It uses the right model at the right time, with the right context, for the right role, under a visible budget.
