"""Manta's agent control plane: a real, configurable agent registry.

This package turns Manta's prompt-only markdown "subagents" into real agents the
user can create, edit, and delete, with **enforced** tools/permissions, durable
private memory, and a per-agent token budget (ADR 0008).

Modules:

- :mod:`manta_code.agents.registry` — the agent definition schema and on-disk
  CRUD under ``~/.manta/agents/<name>/``.
- :mod:`manta_code.agents.factory` — compiles a definition into the
  ``deepagents`` ``SubAgent`` dict the runtime understands (filesystem
  permissions, tool policy, approval, model pin).
- :mod:`manta_code.agents.defaults` — Manta's built-in agents (planning / swe /
  review) in the enforced format.
- :mod:`manta_code.agents.memory` — persistent per-agent memory + privacy
  guardrails.
- :mod:`manta_code.agents.usage` — the local token/cost usage ledger.
- :mod:`manta_code.agents.importer` — import CLAUDE.md / .cursor/rules / .mcp.json.
- :mod:`manta_code.agents.profiles` — project the registry into top-level
  ``deepagents`` profiles (the in-app ``/agents`` picker).
"""

from __future__ import annotations
