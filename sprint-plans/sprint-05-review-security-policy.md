# Sprint 5 — Review, Security, and Policy

## Goal

Make autonomy safe enough for alpha usage.

## Deliverables

- code review gate,
- security review trigger rules,
- shell allowlist,
- protected path policy,
- network policy,
- git policy,
- stop conditions.

## Stories

### S5-1 Code review loop

Acceptance:

- Reviewer can block.
- Builder can fix required findings.
- Max fix loop is enforced.

### S5-2 Security trigger rules

Acceptance:

- Auth/config/dependency/network/secrets changes trigger security review.
- High severity blocks finalization.

### S5-3 Policy engine

Acceptance:

- Shell outside allowlist is blocked or requires approval.
- Protected paths are not auto-read/written.
- Network denied by default.
- Git push denied by default.

## Demo

```bash
manta run "add JWT token refresh" --auto --max-usd 3
manta security-review
```
