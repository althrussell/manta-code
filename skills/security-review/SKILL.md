---
name: security-review
description: Review a diff for auth, secrets, injection, dependency, network, shell, and data exposure risks. Use for security-sensitive changes.
---

# security-review

## Instructions

1. Review touched files and diff.
2. Look for secrets, credentials, unsafe env handling, auth bypass, injection, unsafe network, dependency risk, and excessive permissions.
3. Block high severity issues.
4. Return concrete required fixes.
