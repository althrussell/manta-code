# Open Questions

## Product

1. Should v1 target Python/JS repos first, or be language-agnostic from day one?
2. Should Manta default to smart approve or approve mode for first alpha?
3. Should users be able to force Opus for any task, or require `--allow-expensive`?
4. Should cost display be always on, or configurable?
5. Is Manta a standalone CLI only, or should the Swift app shell invoke it in v1.5?

## Runtime

1. Should role orchestration be a single Deep Agent with subagents, or a Manta-controlled sequence of separate agent invocations?
2. How should actual provider token usage be captured across providers?
3. Should MCP be enabled in alpha or deferred until the policy wrapper is complete?
4. Should sandboxing use local process restrictions, Docker, or provider sandboxes?

## Context

1. Should repo indexing use static AST parsing in v1?
2. Should context manifests be editable by the user before execution?
3. Should Manta learn project conventions into memory automatically?

## Security

1. Should `.env` reads always require explicit approval even in trusted repos?
2. Should network be globally denied or route-specific?
3. Should dependency install commands be denied by default?
4. Should git commit be permitted with approval in v1?

## Commercial/product

1. Should model prices be bundled or user-maintained only?
2. Should Manta support subscriptions/API keys from model vendors directly?
3. Should Databricks Model Serving be a first-class provider profile later?
