# Config Schema

Manta config controls launch targets, discovery, inference policy, local budgets,
and pricing. Agent definitions and their model pins live in the Manta registry;
upstream UI, approval, session, and provider settings live in
`~/.deepagents/config.toml`. See [the integration guide](13-deepagents-integration.md)
for the interactive, headless, and delegated construction paths.

## Config locations and precedence

Project `.manta/config.toml` settings merge over user settings in
`~/.manta/config.toml` (or `$MANTA_HOME/config.toml`), which merge over defaults.
An explicit launch `-M` wins over the configured launch default. A resumed
conversation keeps its saved model unless the user explicitly switches it.

For one Databricks target, constructor/runtime options take precedence over
per-target settings, Manta-wide settings, preserved upstream provider defaults,
and automatic selection, in that order. Semantic aliases participate in this
precedence: an explicit `use_responses_api=False` overrides a lower-layer
`api="responses"`. Contradictory options in the same layer fail clearly.

## Manta schema

The historical `default_endpoint` and `extra_endpoints` keys accept either
legacy serving-endpoint names or exact three-part Unity Catalog model-service
names. `databricks:<target>` is the public model reference; inference receives
the target name unchanged. Do not add or remove a `databricks-` component.

```toml
[runtime]
provider = "databricks"

[interactive]
default_endpoint = "system.ai.gpt-6-luna"
extra_endpoints = ["existing-team-chat", "custom.team.private-service"]

[databricks]
discovery_schemas = ["system.ai", "custom.team"]
service_api = "auto"           # auto | chat | responses
request_tags = { application = "manta" }
discovery_timeout_seconds = 4.0 # > 0 and <= 30; complete launch discovery deadline
discovery_cache_seconds = 120.0 # 0 disables reuse; maximum 3600

[databricks.params]
timeout = 60

[databricks.targets."system.ai.gpt-6-1-sol"]
api = "responses"
max_output_tokens = 4096
request_tags = { workload = "engineering" }

[databricks.targets."custom.team.private-service"]
# Optional explicit capability overrides: use verified values for your target.
profile = { max_input_tokens = 32768, tool_calling = true }

[databricks.targets."legacy.dotted.endpoint"]
kind = "endpoint"             # Disambiguates an actual dotted legacy endpoint.

[budget]
daily_max_usd = 25.0
mode = "checkpoint"           # checkpoint (default) | advisory

# Example only: set rates verified for this exact service and your pricing.
[pricing."custom.team.private-service"]
input = 2.0                   # USD per 1M tokens
output = 8.0
# Optional explicit cache_read and cache_creation rates.
source = "workspace contract" # Optional provenance, displayed by manta pricing.
as_of = "2026-10-20"
```

`manta init` writes the current defaults from `model_defaults.py`;
`manta init --overwrite` regenerates the file. Existing explicit user settings
and saved agent pins retain their values. Inspect pins with `manta agents`,
then change individual agents with `manta agents set-model <agent> <target>`.
The launch default and agent pins are separate choices.

## Target identity, surfaces, and capabilities

A service FQN has exactly three nonempty catalog/schema/service components;
letters, digits, underscores, and hyphens are supported. A management resource
`model-services/catalog.schema.service` is normalized to the same service.
Registered-model and model-provider-service resource paths are different
namespaces and are rejected as inference targets.

| Target / surface | Inference base | Automatic API selection |
| --- | --- | --- |
| Endpoint / `legacy` | `/serving-endpoints` | Chat Completions |
| Service / `unified` | `/ai-gateway/mlflow/v1` | Responses unless exact compatibility evidence or Chat-only metadata selects Chat |
| Service / `openai` | `/ai-gateway/openai/v1` | Responses when supported or metadata is unknown; chat for chat-only metadata |

`surface` can be set per target or constructor. The compatibility options
`use_ai_gateway`, `use_ai_gateway_native_api`, and `use_responses_api` map to the
same policy. Services cannot use the legacy route; an endpoint cannot use a
Unity route. `api` accepts `auto`, `chat`, or `responses`. Exact curated evidence
and discovered `supported_api_types` guide automatic selection; names and family
substrings do not establish API support. An explicit selection known to be
unsupported fails before inference. Metadata denial does not prevent a direct,
explicitly configured call.

One exact-target capability catalog supplies adapter validation, model cards,
`/effort`, and context compaction. Facts carry source/date and protocol/workspace
qualification separately. Capabilities remain unknown unless supported by
metadata, exact documented compatibility evidence, or an explicit `profile`
override. A protocol advertisement
does not imply a context limit, tool support, or multimodal support. `profile`
is a model capability map; `databricks_profile` selects SDK authentication.
Native Anthropic Messages, native Gemini, and direct provider-service addressing
are optional extensions outside this transport. Compatible services use the
unified gateway APIs.

The adapter checks known output and effort limits before dispatch. Context
preflight estimates instructions, messages and tool schemas, then reserves the
requested output cap (or the known maximum output when none was supplied).
Media and opaque continuation state remain intact and are marked unmeasured;
this estimate is not proof that a maximum-size context will succeed. See
[model capability qualification](18-model-capability-qualification.md) for exact
limits, provenance, tested controls and remaining unknowns.

## Discovery and diagnostics

Discovery independently lists legacy chat endpoints and model services in the
configured schemas. SDK pagination, empty intermediate pages, and later-page
failures retain available results. Known embedding-only services are excluded
from coding selection. Configured targets outside the schemas receive bounded
direct lookups and remain selectable when metadata is denied or unavailable.

The launch deadline covers SDK client construction and all discovery sources.
Results carry independent success, partial, denied, unsupported, unavailable,
or direct-target absence outcomes. A bounded in-memory cache is scoped to the
workspace, profile/auth context, schemas, and configured identities.
`manta doctor` and `manta gateway --refresh` refresh metadata. Workspace
switching refreshes inventory and exported capability evidence before restart.
No metadata result verifies inference `EXECUTE` access.

Ordinary `manta doctor` checks credentials and metadata without inference or a
`current_user` call. `manta doctor --probe` explicitly requests a bounded
two-call tool round trip for each distinct launch/agent target and records
returned usage locally. `manta gateway --target <target>` inspects detailed
routing, fallback, and visible policy
configuration on demand. Missing visible policy is reported as unknown.

## Authentication and request tags

Databricks uses the official OpenAI LangChain adapter plus Databricks SDK unified
auth. Profile precedence is:

```text
child -p/--profile > root -p/--profile > MANTA_PROFILE > DATABRICKS_CONFIG_PROFILE > SDK default profile
```

An explicit model `databricks_profile` selects that model's SDK profile. The
launcher aligns `MANTA_PROFILE` and `DATABRICKS_CONFIG_PROFILE` in child processes.
Clients are lazy; all SDK authentication headers refresh for every HTTP request,
including retries and auxiliary Azure credentials. Async credential work runs
off the event loop. Databricks calls do not use unrelated `OPENAI_API_KEY` or
`OPENAI_API_BASE` settings. An explicit base URL must use the workspace origin.

Request tags are a validated string-to-string map, sent in
`Databricks-Ai-Gateway-Request-Tags`. Configured tags merge with target tags,
explicit constructor headers, and per-call values; the explicit value wins.
Unrelated per-call headers do not discard constructor tags. Malformed maps,
line breaks, and headers above the supported size fail locally.

Inference authorization requires `EXECUTE`, `USE CATALOG`, and `USE SCHEMA`,
plus applicable entitlement and model availability. See
[Databricks querying requirements](https://docs.databricks.com/aws/en/ai-gateway/query-model-services).

## Pricing, usage, and USD caps

Pricing first matches the exact requested identity, including explicit config
overrides, then an exact observed backend identity if returned. Service FQNs and
custom services never receive a price guessed from `claude`, `gpt`, or another
family substring. Historical substring estimates remain available for legacy
endpoint names. No built-in service price is assumed without a verified rate.
Rates must be finite and nonnegative. Cache read/write prices must be explicit;
Manta does not derive one vendor's rates from another vendor's economics.

The local SQLite ledger migrates additively, preserving historical rows and the
existing model field. New rows record requested target/service, observed backend
when available, surface/API, gateway request ID when exposed, usage completeness,
cache/reasoning detail, call kind, latency, and pricing source/version. A shared
LangChain callback collector observes main, specialist and helper requests;
invocation/request identity deduplicates repeated accounting observations.
An echoed service name does
not establish a backend. Missing usage or rates produce unknown cost rather than
a zero-dollar call. Reasoning tokens already included in output are not charged
again. Historical usage completeness remains unconfirmed.

`manta pricing` shows effective exact-target rates and source/version metadata.
Project rates override user rates. Configured `source` and `as_of` values describe
the supplied rate; Manta does not independently verify a private contract price.
Cost, budget, receipts, and `/cost` show known subtotals and the number of calls
priced when coverage is incomplete; savings are unavailable for incomplete
receipts. Unknown charges cannot count toward a client USD cap. Manta records
and warns about incomplete cap coverage. Gateway budgets and actual billing
remain separate from these local estimates.

Budget policy is explicit:

| Mode | At an observed token/USD threshold |
| --- | --- |
| `checkpoint` (default) | Interrupt before another agent request. Interactive runs ask for approval; unattended runs retain a pending checkpoint instead of auto-approving it. Missing checkpoint machinery stops the request. |
| `advisory` | Record the threshold and continue. |

Per-agent `budget_max_tokens` and `budget_max_usd` belong in the agent registry;
`budget.daily_max_usd` is the shared daily known-spend threshold. Checks happen
between requests, so one request can exceed a limit. Helper calls are recorded
and attributed, but this is not a prepaid or server-enforced spending ceiling.
Unknown usage/prices cannot establish a hard dollar cap; use bounded turns/time
and any applicable Gateway controls as well.

Detached budget-paused tasks keep their task ID, thread, output and nonsecret
execution manifest. Inspect `manta task status <id>` and
`manta task output <id>`, then explicitly approve continuation with
`manta task resume <id>`. Resume uses the saved graph checkpoint and validates
the original project/workspace/agent identity; it refuses a missing checkpoint
or changed execution context. It does not replay the original prompt as a new
task. Ordinary headless runs report a thread ID for interactive `manta -r <id>`.

## Upstream provider config and ownership

Manta idempotently registers its stable class path:

```toml
[models.providers.databricks]
class_path = "manta_code.databricks_chat:MantaChatDatabricks"
models = ["existing-team-chat", "system.ai.gpt-6-luna"]
```

Unrelated settings, user models, provider parameters, and explicit user defaults
are preserved. `[manta.databricks_inventory]` records which model entries and
default Manta owns. A refreshed workspace inventory replaces only owned entries;
it does not accumulate stale entries or remove user registrations. Only an
unchanged default with recorded Manta ownership can migrate automatically.
Older unowned defaults remain in place with a diagnostic migration notice.

Preserved provider defaults, per-model settings, and Manta policy are shared by
primary, switched, registry-pinned, delegated, direct, and background model
construction. `MANTA_DATABRICKS_POLICY` transports nonsecret settings and
capability evidence to child processes; credentials and client objects are
excluded. Request-tag headers are preserved without authorization headers.
Do not edit this internal environment value manually.
