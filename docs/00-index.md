# Manta Code Docs Index

Manta Code uses the upstream
[`deepagents-code`](https://pypi.org/project/deepagents-code/) interactive coding
runtime and adds enforced agents, policy, usage accounting, and background tasks.
Type `manta` to launch the TUI, with SDK-authenticated Databricks serving endpoints
or Unity Gateway model services. See
[ADR 0008](adr/0008-owned-agent-control-plane.md) and
[the integration guide](13-deepagents-integration.md) for ownership and current
transport limits.

## Docs

- `09-cli-ux-contract.md` — interactive, headless, diagnostic, and task commands.
- `10-config-schema.md` — `.manta/config.toml` and the deepagents-code provider config.
- `13-deepagents-integration.md` — how Manta wires Databricks into deepagents-code.
- `14-unity-gateway-model-services-plan.md` — initial audit, completed design, and
  implementation status for `system.ai.*` model services.
- `15-unity-gateway-qualification.md` — live labs matrix, regression evidence,
  and current gateway limitations.
- `16-installation-and-upgrades.md` — tagged installs, release assets, upgrades,
  Python migration, and workspace verification.

## Architecture decision records

See `docs/adr/`. ADRs 0001 and 0003–0006 describe the original pipeline;
ADR 0007 adopted the upstream TUI, and ADR 0008 reintroduced Manta's control
plane through runtime middleware. Later records cover provider support,
background tasks, and pilot readiness. Historical records preserve their
original dependency versions and decisions.

## Source

See `src/manta_code/`:

- `main.py` — CLI entry, agent/task/usage commands, and argument routing.
- `dcode.py` — provisions the Databricks provider and launches the TUI.
- `auth.py` — Databricks profile resolution (SDK unified auth).
- `config.py` — launcher config schema and `.manta/config.toml` handling.
