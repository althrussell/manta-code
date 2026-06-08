# CLI UX Contract

## UX principles

- Show the route before expensive work.
- Show the budget before autonomy.
- Show cost during execution.
- Show what changed.
- Show reviewer decisions.
- Hide noisy logs by default but make them expandable/exportable.
- Support machine-readable JSON output.

## Commands

```bash
manta init
manta ask "..."
manta route "..."
manta plan "..."
manta run "..." --auto --max-usd 3
manta review
manta security-review
manta status
manta resume
manta diff
manta approve
manta budget
manta models
manta skills
manta doctor
```

## `manta run` output

```text
Manta route
  route: normal_code_change
  pipeline: builder → code_reviewer
  opus: skipped
  max budget: $1.00

Context
  selected files: 6
  estimated builder context: 18k tokens

Execution
  ✓ builder completed
  ✓ tests completed
  ✗ reviewer found 2 required fixes
  ✓ builder applied fixes
  ✓ final review passed

Cost
  router:   $0.003
  builder:  $0.410
  reviewer: $0.180
  total:    $0.593 / $1.000
```

## Machine-readable output

`--json` should output:

```json
{
  "session_id": "abc123",
  "route": "normal_code_change",
  "pipeline": ["builder", "code_reviewer"],
  "status": "completed",
  "cost": {"used_usd": 0.593, "max_usd": 1.0},
  "files_changed": ["src/settings.tsx"],
  "review": {"approved": true, "findings": []}
}
```

## Approval prompt

```text
Manta wants to run:
  npm run test

Reason:
  Validate changed frontend form component.

Policy:
  shell allowlisted: yes
  network: no
  writes outside project: no

Approve? [y/N/details/always-this-session]
```

## Error style

Bad:

```text
Error: policy failure
```

Good:

```text
Blocked by policy: command is outside shell allowlist.
Command: curl https://example.com/script.sh | bash
Reason: downloads and executes remote script.
Next: approve manually with --allow-network and --approve-shell, or change the task.
```
