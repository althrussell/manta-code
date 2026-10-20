# Changelog

## [0.3.0] — 2026-10-20

Unity Gateway model services now work throughout Manta: interactive sessions,
delegated agents, saved conversations, headless runs, and detached tasks.

### Runtime migration

- Requires Python 3.12 or newer; CI qualifies Python 3.12 and 3.14.
- Updates Deep Agents Code to 0.1.86, Deep Agents to 0.7.28, and the supporting
  dependency graph to the latest compatible releases recorded in the lock file.
- Replaces the Databricks LangChain/OpenAI bridge with LangChain OpenAI and
  request-time Databricks SDK unified authentication, resolving the MCP 1/MCP 2
  dependency conflict.

### Model services and usage

- Adds exact `system.ai.*` and custom-schema service targeting, independent
  Unity/legacy discovery, per-target protocol and parameter settings, request
  tags, and explicit native OpenAI routing.
- Preserves opaque reasoning and signed tool state through sync/async invoke,
  streaming, checkpoints, delegation, and compatible model/API switches.
- Isolates target and capability evidence by profile/workspace. Workspace
  refreshes preserve user-owned provider settings, defaults, and agent pins.
- Uses qualified latest service defaults for coordination, planning, SWE,
  and review; existing explicit user choices remain preserved.
- Extends doctor, model verification, picker, and gateway inspection to model
  services. Metadata denial remains separate from inference permission.
- Records additive usage provenance, exact price overrides, missing usage,
  and unknown prices. Cost reports and evals show incomplete pricing without
  inventing zero-cost calls or savings.

### Release and CLI fixes

- Adds `manta --version`, which works without starting the runtime or loading
  workspace credentials.
- Restores headless JSON results and live JSONL stdout/stderr output on the
  current upstream runtime.
- Preserves explicit launch models, compact resume arguments, saved targets,
  and independent parent/child protocol state.
- Adds validated wheel/source assets, dependency constraints, release notes,
  checksums, and a tag-triggered GitHub release workflow.
- Updates installation, explicit tag upgrades, pip environments, release-wheel
  verification, migration, and installed-runtime recovery instructions.

### Qualification and limits

Live labs qualification covers GPT and Claude unified Responses, Gemini unified
Chat, native OpenAI Responses, legacy Chat, real delegation/checkpoint resume,
and headless/background execution. Exact qualified Gemini services use Chat
because the gateway currently rejects Responses tool continuation. Native
Anthropic/Gemini adapters remain optional extensions. See the
[qualification report](docs/15-unity-gateway-qualification.md) and
[release notes](docs/releases/v0.3.0.md).

## [0.2.0] — 2026-06-11

Fresh installs open as chief of staff. Added steerable tasks, ASK enforcement,
durable agent memory, job-run diagnostics, receipts, live doctor probes, Manta
version branding, and expanded README workflows.

## [0.1.0] — 2026-06-11

Initial pilot release of the Databricks-configured Deep Agents launcher and
Manta agent control plane.

[0.3.0]: https://github.com/althrussell/manta-code/releases/tag/v0.3.0
[0.2.0]: https://github.com/althrussell/manta-code/releases/tag/v0.2.0
[0.1.0]: https://github.com/althrussell/manta-code/releases/tag/v0.1.0
