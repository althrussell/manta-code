"""Manta runtime middleware layered onto deepagents-code (ADR 0008).

These are LangChain ``AgentMiddleware`` subclasses that Manta injects into agent
construction via :mod:`manta_code.hook`:

- :mod:`manta_code.middleware.policy` — tool allow/deny + read-only enforcement
  (the half of "enforced permissions" that filesystem ``permissions`` can't
  cover, e.g. blocking ``execute``).
- :mod:`manta_code.middleware.economy` — trust-first token accounting, budget
  warnings + approve-to-continue, and the usage-ledger write.
- :mod:`manta_code.middleware.routing` — cost-aware model escalation.

Each module imports the LangChain middleware base lazily/at module top but is
only ever imported when the ``agent`` extra is installed (the hook guards this),
so importing :mod:`manta_code` itself stays light.
"""

from __future__ import annotations
