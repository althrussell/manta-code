# v1 Scope and Non-goals

## v1 scope

Manta v1 is a repo-first autonomous developer CLI.

Included:

- CLI entrypoint.
- Project initialization.
- Cheap router.
- Route-to-pipeline mapping.
- Model role registry.
- Token/cost budget ledger.
- Context broker v1.
- File and git inspection tools.
- Patch-based edits.
- Allowlisted shell runner.
- Code review agent.
- Security review agent.
- Session event log.
- Dry-run mode.
- Basic eval harness.

## v1 non-goals

Not included in v1:

- Full desktop app.
- Native macOS Swift UI.
- Multi-channel personal assistant behavior.
- Calendar/email/browser automation.
- Cloud-hosted background daemon.
- Team multi-user permission server.
- Remote sandbox orchestration beyond local shell allowlisting.
- Full IDE plugin.
- Full ACP client/server implementation.
- Marketplace for skills.
- Arbitrary OpenClaw-style personal automation.

## Why repo-first

Repo-first gives the cleanest path to prove the core differentiation:

```text
cheap router + role pipeline + token budget + review gates
```

A general-purpose assistant would create too many variables before the cost-control and role-separation engine is proven.

## Expansion path after v1

1. v1: repo-first CLI.
2. v1.5: IDE/ACP bridge and richer TUI.
3. v2: Manta macOS native shell around the CLI runtime.
4. v3: generalized local task automation with channel adapters and deeper MCP ecosystem.
