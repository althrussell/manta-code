"""Versioned model choices for Manta-managed roles.

Saved user agent definitions and explicit model arguments always take precedence.
Changing this table only changes built-ins and newly generated configuration.
"""

DEFAULTS_VERSION = 2
ROLE_TARGETS = {
    "orchestrator": "system.ai.gpt-6-luna",
    "chief": "system.ai.gpt-6-luna",
    "planning": "system.ai.claude-opus-5-5",
    "swe": "system.ai.gpt-6-1-sol",
    "review": "system.ai.claude-sonnet-5-5",
}


def role_model(role: str) -> str:
    return f"databricks:{ROLE_TARGETS[role]}"


def extra_role_targets() -> list[str]:
    return list(dict.fromkeys(v for v in ROLE_TARGETS.values() if v != ROLE_TARGETS["orchestrator"]))
