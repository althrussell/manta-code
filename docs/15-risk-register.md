# Risk Register

| Risk | Severity | Likelihood | Mitigation |
|---|---:|---:|---|
| Router underestimates complexity | High | Medium | Track misroutes, allow escalation, add evals. |
| Opus overuse creates bill shock | High | Medium | Opus denied by default unless route permits. |
| Builder changes too many files | Medium | High | Patch-only writes, context manifest, reviewer gates. |
| Reviewer misses security issue | High | Medium | Trigger separate security reviewer and scanners. |
| Shell command causes damage | High | Medium | Allowlist, policy engine, adversary review, project root checks. |
| MCP tool bypasses policy | High | Medium | Wrap all MCP calls through ToolRequest policy. |
| Context broker omits critical file | Medium | High | Planner can request context expansion within budget; reviewer can flag missing context. |
| Cost estimates inaccurate | Medium | Medium | User-editable price table; store actual provider usage where available. |
| Deep Agents API changes | Medium | Medium | Adapter boundary and integration tests. |
| Product becomes too broad | High | Medium | Keep v1 repo-first. |
| Review loops never converge | Medium | Medium | Max iterations and stop reports. |
| Users dislike approvals | Medium | Medium | Smart approve mode with transparent policy. |

## Highest-priority mitigations

1. Budget hard caps.
2. Tool policy wrapper.
3. Context manifests.
4. Structured review outputs.
5. Evaluation suite.
