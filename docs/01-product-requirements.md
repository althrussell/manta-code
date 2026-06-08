# Product Requirements Document — Manta CLI v1

## Product thesis

Manta CLI is a budget-aware multi-model autonomous developer CLI.

It routes simple work to cheap models, escalates complex planning to Opus-class models, builds with GPT/Codex-class models, and reviews with Gemini-class models. The user gets the best model for the job without uncontrolled token burn.

## Primary user

A developer or technical builder who wants autonomous coding productivity but does not want:

- every prompt to hit the most expensive model,
- hidden token spend,
- a single monolithic agent doing planning, implementation, and review,
- blind trust in file edits and shell execution,
- noisy context bloat,
- or unreviewed autonomous changes.

## Core promise

```text
Best model. Best job. Best output. No token bill shock.
```

## v1 target outcome

A user can run:

```bash
manta run "add settings page with tests" --auto --max-usd 2
```

Manta will:

1. classify the task cheaply,
2. decide the pipeline and budget,
3. select scoped context,
4. implement changes,
5. run tests/lint where safe,
6. review the diff independently,
7. fix reviewer findings if budget remains,
8. summarize final changes and cost.

## Success criteria

### Functional

- CLI can initialize project config.
- CLI can route tasks into explicit pipelines.
- CLI can run in dry-run mode without model keys.
- CLI can run model-backed builder/reviewer flow.
- CLI can track per-role token and cost estimates.
- CLI can record a session event log.
- CLI can produce a final diff summary and review report.
- CLI can stop on budget exhaustion.
- CLI can block unsafe shell/network/git actions.

### Quality

- The router avoids Opus for simple Q&A and trivial edits.
- The builder receives narrow context.
- The reviewer receives diff-first context.
- Security review is automatically triggered by risk signals.
- Users can understand why a model was selected.
- Users can see estimated budget before autonomy begins.

### Cost

- Default route for simple Q&A costs pennies or less.
- Normal code change stays under the configured default budget.
- Opus planning only activates for high ambiguity, architectural impact, explicit deep-plan requests, or high-risk work.

## Key user stories

### Ask mode

As a developer, I want to ask a simple question without invoking expensive planning.

Acceptance:

- `manta ask "what does this error mean?"` uses cheap responder by default.
- The output shows the selected route and model if verbose mode is enabled.

### Build mode

As a developer, I want Manta to implement a normal feature using a builder model and review the result.

Acceptance:

- `manta run "add profile settings form" --max-usd 1` routes to builder + reviewer unless complexity requires planning.
- Manta records diff, tests, review, and cost.

### Complex planning

As a developer, I want complex architecture work to get deeper planning only when justified.

Acceptance:

- Multi-file or architecture-impacting prompts route to planner + builder + reviewer.
- Planner writes acceptance criteria and context manifest.

### Security-sensitive work

As a developer, I want auth, secrets, network, dependency, and data-access changes to get a security review.

Acceptance:

- Security risk signals trigger security reviewer.
- High severity findings block final approval.

### Budget protection

As a developer, I want a hard budget so autonomy cannot surprise me.

Acceptance:

- User can set `--max-usd`.
- Budget exhaustion stops the loop and writes a partial completion report.
- Every model call records estimated cost.

## Non-functional requirements

- Project-local config in `.manta/config.toml`.
- User-level config in `~/.manta/config.toml`.
- Event log as JSONL.
- No model secrets in repo config.
- No writes outside project root by default.
- No network by default.
- No git push by default.
- Human approval path for risky actions.
