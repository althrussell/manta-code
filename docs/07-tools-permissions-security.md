# Tools, Permissions, and Security

## Design principle

Autonomy is allowed only inside explicit policy.

Every tool call should be classified as:

```text
allow
approval_required
block
```

## Default policy

```yaml
autonomy:
  allow_file_writes: true
  allow_shell: allowlisted
  allow_network: false
  allow_git_commit: approval
  allow_git_push: false
```

## Tool categories

| Tool | Risk | Default |
|---|---:|---:|
| read_file | Low | Allow inside project |
| write_file | Medium | Approval or patch-only |
| apply_patch | Medium | Allow inside project if auto enabled |
| git_status | Low | Allow |
| git_diff | Low | Allow |
| git_commit | Medium | Approval |
| git_push | High | Block |
| shell_test | Medium | Allowlisted |
| shell_arbitrary | High | Approval/block |
| network | High | Block |
| MCP filesystem | Medium/High | Policy wrapped |
| MCP external API | High | Approval/network policy |

## Shell allowlist v1

Allowed command prefixes:

```text
pytest
python -m pytest
npm test
npm run test
npm run lint
pnpm test
pnpm lint
yarn test
yarn lint
ruff check
mypy
uv run pytest
```

Everything else requires approval or blocks depending on mode.

## Protected paths

Never auto-read or auto-write:

```text
.env
.env.*
**/*secret*
**/*credential*
**/*private_key*
~/.ssh/**
~/.aws/**
~/.config/**
```

Never auto-write outside project root.

## Network policy

Default: denied.

Allow by explicit project config only:

```toml
[network]
allow = true
allowed_domains = ["github.com", "pypi.org", "registry.npmjs.org"]
```

## Adversary reviewer pattern

Before risky tool execution, Manta can run a lightweight adversary check with:

- original user request,
- recent session summary,
- tool call,
- policy rules.

Output:

```json
{
  "decision": "block",
  "reason": "Command attempts to transmit .env contents to external host."
}
```

The first version can be deterministic rules only. Add model-based adversary review later.

## Security review triggers

Trigger security reviewer when diff touches:

```text
.env*
requirements*.txt
pyproject.toml
package*.json
Dockerfile
.github/workflows/**
terraform/**
infra/**
auth/**
security/**
permissions/**
config/**
```

Also trigger on keywords:

```text
auth, oauth, jwt, token, secret, password, encryption, database, migration, webhook, upload, download, network, shell, subprocess
```

## Blocking rules

Block finalization when:

- security reviewer finds high severity issue,
- tests fail after max attempts,
- reviewer blocks twice,
- budget exhausted,
- unsafe tool call requested,
- protected path is modified,
- git push requested without explicit human command.
