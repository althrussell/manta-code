# Research Source Notes

These source notes were used to shape the bootstrap architecture.

## Deep Agents

- Deep Agents is positioned as an open-source agent harness with subagents, filesystem, context management, shell access, persistent memory, human-in-the-loop, skills, tools, and MCP support.
- Deep Agents subagents can define role-specific names, descriptions, system prompts, tools, models, middleware, human-in-the-loop interrupts, skills, response formats, and filesystem permissions.
- Deep Agents skills use `SKILL.md` with progressive disclosure.
- Deep Agents filesystem permissions apply to built-in filesystem tools, not all custom/MCP/sandbox tools; Manta therefore needs its own policy wrapper.

Useful URLs:

- https://github.com/langchain-ai/deepagents
- https://docs.langchain.com/oss/python/deepagents/overview
- https://docs.langchain.com/oss/python/deepagents/subagents
- https://docs.langchain.com/oss/python/deepagents/permissions
- https://docs.langchain.com/oss/python/deepagents/skills

## Goose

- Goose is a native open-source AI agent with desktop app, CLI, and API entrypoints.
- Goose supports broad provider/model configuration, MCP extensions, recipes, subagents, permission modes, and cost display.
- Goose's adversary mode inspired Manta's pre-tool-call policy/adversary review pattern.

Useful URLs:

- https://github.com/aaif-goose/goose
- https://goose-docs.ai/
- https://goose-docs.ai/docs/guides/config-files/
- https://goose-docs.ai/docs/guides/goose-cli-commands/
- https://goose-docs.ai/docs/guides/context-engineering/subagents/
- https://goose-docs.ai/docs/guides/security/adversary-mode/
