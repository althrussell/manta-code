# Model controls and capability evidence

The subsequent [Argon coding-harness qualification](21-gemini-argon-harness-qualification.md)
covers its full tool schemas, actual streamed `grep` execution and separately
qualified legacy endpoint on 21 October 2026.

Reviewed and qualified on 20 October 2026. Model construction, the model picker,
`/effort`, and context compaction now read the same exact-target capability
evidence. The registry supplies metadata; it does not restrict discovery or
prevent an unrecognized service from being selected by its full name.

## What the app exposes

The picker shows context and output limits, supported input types, tool/schema
facts, the selected protocol, and the features actually checked through Manta.
Unknown fields remain unknown. Discovery does not prove workspace `EXECUTE`
permission, inference availability, or that every advertised feature works.

`/effort` uses the same profile as the adapter. It supports the documented or
qualified effort values for that exact service and protocol. Changing an effort
preserves unrelated reasoning options and removes conflicting older settings,
including nested `reasoning.effort` from a resumed session. An API change
re-resolves protocol-specific facts instead of promoting old facts to overrides.

The adapter checks every final request payload against its effective profile,
including CLI profile overrides applied after model construction. It rejects a
known invalid effort, output cap, or unsupported forced tool choice locally.
Context preflight includes instructions, messages, tool schemas and reserved
output. When no output cap is supplied, it reserves the known output maximum.
The tokenizer provides an estimate: media and opaque continuation state are
marked unmeasured and retained untouched. `last_context_preflight` exposes only
counts, limits, estimation method and unmeasured categories.

## Reviewed limits

These are documented limits, not results of filling a context window in a live
test. Databricks states that OpenAI, Anthropic and Google service limits follow
their provider-published limits. References are recorded per field in the
capability evidence. Explicit profile overrides remain possible.

| Exact services | Combined context | Maximum output | Effort controls on the default route |
| --- | ---: | ---: | --- |
| `system.ai.gpt-6-1-sol` | 1,050,000 | 128,000 | `low`, `medium`, `high`, `xhigh`, `max`; Responses |
| `system.ai.gpt-6-sol`, `system.ai.gpt-6-luna` | 1,050,000 | 128,000 | `none`, `low`, `medium`, `high`, `xhigh`, `max`; Responses |
| `system.ai.gpt-6-astra` | 1,050,000 | 128,000 | `low`, `medium`, `high`, `xhigh`, `max`; Responses; default unknown |
| `system.ai.claude-opus-5-5`, `system.ai.claude-sonnet-5-5` | 1,000,000 | 128,000 | All five values from `low` through `max` accepted on unified Responses |
| `system.ai.gemini-4-argon` | 1,048,576 | 32,768 unary / 65,536 streaming | Unavailable on its qualified Chat route; the Gateway rejects the control |
| `system.ai.glm-5-3` | 1,048,576 | 65,536 | `low`, `high`, `max`; high accepted on unified Responses |
| `system.ai.glm-5-3-flash` | 1,048,576 | Unknown | `low`, `high`, `max`; high accepted on unified Responses |

The registry also records documented efforts and input types for exact Gemini
3.7/3.8 Flash, Kimi K3, GLM 5.2 and DeepSeek V4 service names. Limits not
established precisely in this review remain absent. The documented
`system.ai.databricks-gemini-4-argon` name is treated as its own identity; it was
not in the observed `labs` inventory. No alias or substring match grants another
service the live qualification of `system.ai.gemini-4-argon`.

## Live control qualification

The [sanitized evidence](qualification/model-controls-2026-10-20.json) contains
18 exact adapter probes: 16 passed and two failed. It includes request IDs,
requested and observed model identity, protocol and returned usage, with the
`labs` workspace recorded as a nonsecret SHA-256 fingerprint. Each prompt asked
only for `OK`, with a 512-token output cap. No grants, services or workspace
policies were changed; no failure triggered a fallback.

| Exact service | Protocol | Result |
| --- | --- | --- |
| Claude Opus 5.5 and Sonnet 5.5 | Unified Responses | Each of `low`, `medium`, `high`, `xhigh`, `max` accepted |
| GPT 6.1 Sol | Unified Responses | `low` accepted; backend reported `gpt-6.1-sol` |
| GLM 5.3 and GLM 5.3 Flash | Unified Responses | `high` accepted |
| Kimi K3 and DeepSeek V4.1 Flash | Unified Responses | `high` accepted |
| Gemini 3.7 Flash | Unified Chat | `high` accepted |
| Gemini 4 Argon | Unified Chat | HTTP 400: `reasoning_effort` rejected inside `generation_config` |
| GLM 5.2 | Unified Responses | HTTP 400: visible service routes to a deprecated endpoint |

These probes establish acceptance of these controls. They do not measure
reasoning quality, long-context accuracy, multimodal input, structured output,
or helper-model behavior. Gemini 3.7 returned a total-token count that exceeds
the sum of its reported input/output counts; consumers must preserve that
uncertainty instead of presenting fully reconciled token semantics.

The existing [tool and checkpoint qualification](15-unity-gateway-qualification.md)
remains separate. Gemini's qualified Chat default preserves tool signatures;
switching it to Responses to obtain another control does not establish a working
tool continuation loop. Claude Sonnet's ordinary tool loop passes but forced
selection remains rejected. Provider-native Anthropic and Gemini adapters,
large-context/media qualification and workspace-specific governance testing
remain further work.

## Sources

- [Databricks supported model services](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)
- [Databricks reasoning controls](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models)
- [Databricks unified Responses behavior](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-open-responses-models)
- [OpenAI models and context/output limits](https://developers.openai.com/api/docs/models)
- [Claude context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)
- [Gemini 4 Argon limits and request modes](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-4-argon)

GPT 6.1 documentation disagrees about `none`; Manta exposes the common documented
subset. Provider-native effort documentation alone is insufficient evidence
for an untested Gateway wire mapping.
