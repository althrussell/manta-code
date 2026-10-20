# Gemini Argon coding-harness compatibility

Qualified on 21 October 2026 with the `labs` profile. The exact Unity service
`system.ai.gemini-4-argon` and legacy endpoint `databricks-gemini-4-argon` pass
streamed coding-tool execution through the actual Manta harness. The
[sanitized results](qualification/gemini-argon-harness-2026-10-21.json) record
the separate targets, protocols, request IDs and acceptance checks.

## Failure and fix

Selecting Argon in v0.4.0 could display `OpenAIInvalidRequestError: An internal
error occurred`. The server log reveals the underlying HTTP 400: Google's
function-schema representation rejects `exclusiveMinimum` in the built-in
`grep` tool's optional positive `max_count` argument. A simple text or echo-tool
probe does not exercise this part of the full coding harness.

For the two exact qualified Argon Chat targets, Manta translates exclusive
integer bounds to equivalent inclusive bounds in the outgoing tool schemas:
`max_count > 0` becomes `minimum: 1`. Original tool definitions and Pydantic
validation remain unchanged. The adapter preserves unions, descriptions and
properties named after schema keywords. It does not remove arbitrary unknown
schema fields or enable the translation by model-name substring.

Non-integer numeric bounds have no exact inclusive successor. Their outgoing
schemas use the inclusive boundary and retain the original strict constraint
in the description. Pydantic-backed tools continue enforcing the strict bound;
callers executing raw schema-only tools must validate their arguments.

The legacy endpoint also rejects streaming `stream_options` and configurable
`reasoning_effort`. Its qualified Chat policy now disables those options,
matching the Unity service's previously qualified constraints.

Streamed tool signatures can arrive after the initial fragment containing the
call ID, or in a signature-only delta. Manta associates these fragments with
their call using stream-local state and preserves the opaque values through
checkpoint serialization and replay. Overlapping streams retain independent
state. Repeated identical opaque fields are captured once so message merging
does not concatenate and corrupt them. Signatures are never synthesized or
copied between calls.

## Live coverage

- Both targets passed unary and synchronous streaming tool roundtrips with
  33 built-in/Manta tool schemas plus a synthetic positive-integer echo tool.
  Each response survived checkpoint serialization before signed continuation.
- Both targets passed asynchronous streaming through the actual interactive
  agent graph with all 34 tools, memory enabled, required runtime controls
  validated and manual approvals. Each executed exactly one real `grep` with
  `max_count=1`, returned the fixture token and left the fixture unchanged.
- Requests included ordinary prior conversation text, exercising the tool
  schema after a conversation already contains messages.

Tests used disposable projects and state directories, synthetic messages and
existing SDK workspace authentication. Private user skills were disabled in
the fixtures. No workspace resources, permissions or model services changed.
The evidence omits prompts, opaque signature values and credentials.

Offline transport regressions cover fragmented and interleaved tool calls,
signature-only chunks, synchronous/asynchronous streams, both output formats,
overlapping streams, checkpoint replay and input immutability. Schema tests use
the real upstream `grep` tool and verify that `max_count=0` still fails local
validation.

This qualification applies to the exact targets and Chat protocols above.
It does not transfer to `system.ai.databricks-gemini-4-argon`, custom services,
Responses, native Gemini APIs, media inputs or general model-quality claims.
