# Goose Lessons for Manta

Goose is a strong reference for a local general-purpose agent product. Manta should borrow patterns, not fork blindly.

## Borrow

### CLI shape

Goose has strong patterns for:

- interactive sessions,
- non-interactive `run`,
- instruction files,
- session resume,
- JSON output,
- provider/model overrides,
- max turn controls.

Manta should support the same broad UX class but make budget and routing first-class.

### Config model

Borrow the idea of persistent config plus environment overrides, but add:

- role-specific model mappings,
- price table,
- route budgets,
- context budgets,
- security gates.

### Subagents

Borrow the idea that specialist agents keep the main context clean.

Manta should make subagents deterministic and role-scoped rather than leaving delegation fully to a supervisor.

### Recipes

Borrow recipes as reusable workflows.

Manta equivalent:

```text
skills + route presets + policies + context profiles
```

### Adversary reviewer

Borrow the pattern of a silent reviewer checking risky tool calls before execution.

Manta v1 can implement deterministic policy first, then model-based adversary later.

### MCP ecosystem

Borrow MCP as the extension path.

Do not enable broad MCP access by default. Every MCP tool needs policy classification.

## Do not borrow blindly

- Do not let the agent choose expensive models without Manta budget control.
- Do not let generic subagents replace explicit role pipeline.
- Do not default to arbitrary shell or network.
- Do not hide cost.
- Do not make Manta a personal assistant before repo-first loop is proven.

## Manta wedge vs Goose

Goose:

```text
General-purpose local agent with broad tools and extensions.
```

Manta:

```text
Budget-aware multi-model developer pipeline with explicit roles, cost control, and review gates.
```
