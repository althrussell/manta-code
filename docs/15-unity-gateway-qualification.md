# Unity Gateway qualification

Qualified on 20 October 2026 (Australia/Sydney) using the Databricks `labs`
profile. Core support covers exact model-service FQNs through unified Responses
and Chat Completions, plus explicitly selected native OpenAI Responses.

The implementation started from `5c28a8add76a9867256f7997aa83aa50627a6907`, which
matched remote `main` when rechecked during qualification. The v0.3.0 release
includes this implementation; see its [release notes](releases/v0.3.0.md) for
distribution and migration details.

## Runtime and dependency set

The portable `requirements-dev.lock` records the latest compatible resolution
as of that date. The main changes are:

| Component | Qualified version |
| --- | --- |
| Python | 3.12.8 and 3.14.5 |
| Deep Agents Code / Deep Agents | 0.1.86 / 0.7.28 |
| Databricks SDK | 0.154.0 |
| LangChain / Core / OpenAI | 1.4.6 / 1.6.11 / 1.7.2 |
| OpenAI SDK | 3.31.0 |
| MCP / FastMCP Slim | 2.3.0 / 4.1.0 |

The Databricks LangChain/OpenAI bridge was removed because its MCP 1 dependency
conflicts with the selected runtime's MCP 2 requirement. `MantaChatDatabricks`
retains its stable class path and uses the official OpenAI-compatible LangChain
client with SDK unified authentication refreshed on each HTTP request.
Pydantic 2.13.5, filelock 3.29.7, and uuid-utils 0.17.1 remain constrained by
upstream bounds. Updating them independently would violate this environment's
dependency requirements.

## Live results

SDK discovery found 67 visible services in `system.ai` and 62 legacy endpoints.
The full no-inference `manta -p labs doctor` workflow returned `Status: OK`,
including all 28 runtime seams and service pins. Targeted `manta gateway`
inspection returned service API/destination metadata and correctly labelled
absent visible policy as unknown. Visibility and API
advertisements were treated as metadata evidence; each result below also
required real inference. No grants, workspace enforcement, or service policies
were changed.

A passing tool roundtrip means: request a nonce through a harmless echo tool,
verify the decoded tool name/arguments/ID, submit its result with the complete
assistant state, and verify the final answer. Each probe used two requests with
no error-triggered protocol, model, or provider fallback.

| Exact target | Surface / API | Result |
| --- | --- | --- |
| `system.ai.gpt-6-1-sol` | Unified Responses | Tool roundtrip passed |
| `system.ai.gpt-6-luna` | Unified Responses | Tool roundtrip passed |
| `system.ai.claude-opus-5-5` | Unified Responses | Ordinary tool roundtrip passed |
| `system.ai.claude-sonnet-5-5` | Unified Responses | Ordinary tool roundtrip passed; forced selection rejected by service |
| `system.ai.claude-opus-4-8` | Unified Responses | Tool roundtrip passed |
| `system.ai.gemini-3-7-flash` | Unified Chat | Tool roundtrip passed |
| `system.ai.gemini-4-argon` | Unified Chat | Invoke, sync stream, and async stream tool roundtrips passed; signatures and provider usage preserved |
| `system.ai.gpt-5-3-codex` | Native OpenAI Responses | Tool roundtrip passed |
| `databricks-gpt-oss-120b` | Legacy Chat | Two repeated tool roundtrips passed |

Gemini unified Responses returned HTTP 400 on tool continuation. The same
failure occurred with SDK-authenticated raw HTTP when the server's output was
replayed untouched, establishing a gateway/backend limitation independent of
LangChain conversion. The exact two qualified Gemini service identities
therefore prefer unified Chat. Manta preserves its camel-case `thoughtSignature`
fields through messages, streaming, serialization, and checkpoints. This is a
selection policy established before inference. Explicit Responses remains
available for retesting after the gateway is corrected.

The Gemini Chat route also rejects `stream_options`; these exact services omit
that option. The gateway returned usage without it in the live streamed tests.
If usage is absent on another call, accounting records incomplete usage.

Claude Sonnet 5.5 advertises tool APIs but rejected forced tool selection during
qualification. Its ordinary tool loop passed. Exact capability evidence marks
forced choice unsupported and raises before a request when it is explicitly
asked for. Other model features remain metadata-dependent or require explicit
configuration; passing an echo loop does not prove every structured-output,
image, reasoning, or context-limit option.

## Execution paths and migration

Real Manta runs on `labs` passed for the default agent, a read-only custom
profile, detached background execution, and the buffered JSON result envelope.
These used `system.ai.gpt-6-luna` and isolated Manta/upstream state under `/tmp`.
The detached task reached `done` with exit 0 and the expected output.

An actual Deep Agents `task` call also passed with `system.ai.gpt-6-luna` as
parent and a factory-compiled, read-only `system.ai.claude-sonnet-5-5` child,
followed by continuation from the parent's saved checkpoint. Both used unified
Responses. Protocol channels are private checkpoint state: a child's selection
cannot overwrite its parent's protocol, and isolated delegation does not inherit
the parent's opaque state. Forked history retains compatibility checks.

Managed role defaults now select:

| Role | Exact service |
| --- | --- |
| Orchestrator / chief | `system.ai.gpt-6-luna` |
| Planning | `system.ai.claude-opus-5-5` |
| SWE | `system.ai.gpt-6-1-sol` |
| Review | `system.ai.claude-sonnet-5-5` |

Existing user agent definitions, explicit model selections, upstream user-owned
defaults, and other providers remain preserved. Manta tracks ownership of its
provider registrations and generated defaults so a workspace refresh removes
only Manta-owned entries. Newly available services can be addressed by exact FQN
without adding their names to a hardcoded model list.

## Deterministic qualification

The regression suite exercises real SDK-compatible HTTP fixtures and actual
LangGraph/Deep Agents composition. Coverage includes:

- Constructor/registry policy parity, exact identities, mixed legacy/Unity
  inventory, private schemas, pagination, delayed iteration failures, metadata
  denial, deadlines, embedding exclusion, and workspace/auth cache isolation.
- Sync/async invoke and stream, parallel tool replay, opaque item/signature
  retention, immutable history, checkpoint dialect restoration, and deliberate
  model/profile/API switch rejection when history is incompatible.
- Structured-output JSON schemas, refusals, image/file inputs, named forced
  function selection, output-limit aliases, tags, SDK auth refresh on retries, cancellation,
  permission/rate-limit/transient failures, and no replay after visible output.
- Primary/delegated ordering, explicit model and resume precedence, model picker
  rendering, workspace refresh, detached task propagation, and machine-readable
  headless output with concurrent stdout/stderr drains.
- Historical ledger migration, requested/observed identity and request IDs,
  token semantics, missing usage, exact pricing overrides, unknown prices,
  partial totals, and evals that do not claim savings for unpriced calls.

Final validation:

| Check | Result |
| --- | --- |
| Python 3.12.8: `.venv/bin/python -m pytest -q` | 738 passed in 38.94 seconds |
| Python 3.14.5: `/tmp/manta-latest-py314/bin/python -m pytest -q` | 738 passed in 39.13 seconds |
| Ruff and `git diff --check` | Passed |
| Dependency checks on both Python versions | No broken requirements |
| Source distribution and wheel built from extracted source | Passed; new runtime modules and source lock included |
| Isolated wheel import, `manta --version`, and command help | Passed |
| Canned eval comparison | Quality held; incomplete pricing correctly suppresses cost/savings verdict |

Both test runs report one expected upstream warning for the experimental forked
subagent fixture. Build checks used the installed setuptools backend without
network package resolution.

## Limits of this qualification

The labs credentials did not provide separate test cases for a Unity-enforced
workspace, an inference-only principal, or a private service with configured
cross-provider fallback. Deterministic fixtures cover those paths; they are not
claimed as live workspace results. Interactive picker, profile-switch, and
resume behavior is tested through offline UI/graph contracts rather than a live
terminal UI session.

Native Anthropic Messages and native Gemini APIs are optional follow-up adapters.
The unified routes support those provider families today. Embedding generation,
model-service provisioning, direct model-provider-service addressing, gateway
billing reconciliation, and workspace governance changes are outside this
implementation.

Local USD amounts are estimates. Unknown prices remain unknown, and their tokens
remain visible. Unknown charges cannot count toward a client-side USD cap;
gateway budgets and billing remain authoritative. Explicit exact-target price
overrides can supply verified workspace-specific rates.
