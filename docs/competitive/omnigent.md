# Competitive review — Omnigent (Databricks `agent-framework`)

*Reviewed 2026-06-11 against the full source tree (~1,250 Python files: engine,
`ap-web`, SDKs, deploy, design docs). This records the assessment behind
[ADR 0011](../adr/0011-level-up-steerable-tasks-ask-policies-sdk.md).*

## What it is

An **agent hosting and governance platform**, not a coding agent. The product:
durable sessions on a central server (Postgres conversation store,
persist-before-forward, snapshot + live-tail SSE) that follow the user from
terminal to browser to phone (Databricks One app), are **shareable live** with
teammates (4-tier RBAC, OIDC, comments, conversation forking), and **supervise
other agents** — Claude Code, Codex, OpenAI Agents SDK, Pi, and YAML-defined
custom agents all run as interchangeable *harnesses* under one session model.
React web UI (chat, xterm terminals, files, approvals, sub-agent tree), Python
SDK + REST API, seven deployment targets, OSS launch plan in flight.

## Capabilities Manta lacks (verified in source)

| Capability | Where |
|---|---|
| Device-portable durable sessions (server-side state) | `omnigent/server`, `omnigent/stores/conversation_store` |
| Multi-user sharing/collab, RBAC, forking | `designs/SESSIONS_AUTH.md`, `omnigent/server/permissions.py` |
| Cross-harness supervision (Claude Code + Codex in one session, cross-vendor review) | `omnigent/inner/*_executor.py`, `examples/polly` |
| 4-phase policy engine (input/tool-call/tool-result/output), LLM-classifier policies, **label/taint information-flow tracking**, **ASK verdicts**, server/agent/session policy scopes, per-user daily cost budgets | `omnigent/runtime/policies/`, `designs/POLICIES.md` |
| OS-level sandboxing per agent spec (bwrap/seatbelt, read/write paths, network) | `omnigent/inner/os_env.py`, `omnigent/inner/sandbox.py` |
| **Steerable background agents**: send messages into a running child, async inbox, peek at child history mid-flight | `omnigent/tools/builtins/spawn.py`, `agents.py` |
| Python SDK + REST API + web UI | `sdks/python-client`, `ap-web/` |

## Manta capabilities Omnigent lacks (verified in their source/TODOs)

- **Token economy as product**: scaffold-vs-net-new analysis, cache-economics
  splits, `manta cost --advise`, per-task drilldown. They track cost + a daily
  budget policy; no spend *optimization* surface at all.
- **Proactive model advice and live model switching** — their docs state
  mid-session model switching **requires a new session**; Manta switches live,
  advises when to switch, pins per role with a truthful UI.
- **Deterministic routing** (plan-intent, `@agent` addressing) — their
  supervision is entirely LLM-judgment via `sys_session_send`.
- **Lakehouse-native data tools** (governed UC catalog/lineage/SQL/jobs) —
  their UC-functions support is literally a `TODO.md` entry.
- **Cost-quality eval harness** ("cheaper at equal quality or CI fails") and
  ledger-priced live evals.
- Their own gaps, per their TODO/designs: native Claude/Codex tools **bypass
  their PolicyEngine** entirely; no session crash-durability; macOS/Windows
  sandboxing stubbed; memory/history system tools unimplemented.

## Strategic posture (decided)

- **Do not chase**: central server, web UI, multi-tenant platform,
  cross-harness abstraction. That is their game and a huge surface; Manta wins
  on depth-per-token, the economy brain, and lakehouse-native coding, not
  hosting breadth. (Explicitly confirmed by the maintainer 2026-06-11.)
- **Complement**: their harness model wraps any CLI agent (they already wrap
  Claude Code). Manta can run *as an Omnigent harness* later, inheriting
  device portability/sharing/mobile for free while staying the smartest agent
  in the fleet. No work required from us now — their YAML wraps CLIs.
- **Raid their three genuinely better ideas** — that is ADR 0011:
  steerable background tasks, an ASK policy tier, and a Python SDK.
