from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .model_defaults import ROLE_TARGETS, extra_role_targets


DEFAULT_CONFIG_TEXT = (
    """\
# Manta launches deepagents-code's interactive TUI preconfigured for Databricks.
# Manta wires its SDK-authenticated OpenAI adapter into
# deepagents-code via a class_path provider in ~/.deepagents/config.toml.
[runtime]
provider = "databricks"

# `default_endpoint` is a Databricks serving endpoint or exact three-part Unity
# model-service name (passed as `databricks:<target>`).
# `extra_endpoints` are also registered in deepagents-code's `/model` switcher
# so you can switch between them in-session.
# The default is the orchestration model; `extra_endpoints` includes the models
# Manta's planning/swe/review agents use (see manta_code.agents.defaults) so they
# are all available in deepagents-code's `/model` switcher.
[interactive]
"""
    + f'default_endpoint = "{ROLE_TARGETS["orchestrator"]}"\n'
    + "extra_endpoints = [\n"
    + "".join(f'    "{target}",\n' for target in extra_role_targets())
    + "]\n"
)


class RuntimeConfig(BaseModel):
    provider: str = "databricks"


class InteractiveConfig(BaseModel):
    default_endpoint: str = ROLE_TARGETS["orchestrator"]
    extra_endpoints: list[str] = Field(default_factory=extra_role_targets)


class BudgetConfig(BaseModel):
    #: Daily (UTC) spend cap across all Manta usage on this machine (ADR
    #: 0011). Checkpoint mode pauses interactive and unattended runs; explicit
    #: advisory mode records an event and continues. ``None`` = no cap.
    daily_max_usd: float | None = None
    mode: Literal["checkpoint", "advisory"] = "checkpoint"


class DatabricksConfig(BaseModel):
    """Nonsecret discovery and inference policy, shared by all model paths."""

    discovery_schemas: list[str] = Field(default_factory=lambda: ["system.ai"])
    service_api: str = "auto"
    params: dict[str, Any] = Field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    request_tags: dict[str, str] = Field(default_factory=dict)
    discovery_timeout_seconds: float = Field(default=4.0, gt=0, le=30)
    discovery_cache_seconds: float = Field(default=120.0, ge=0, le=3600)


class MantaConfig(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    interactive: InteractiveConfig = Field(default_factory=InteractiveConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    databricks: DatabricksConfig = Field(default_factory=DatabricksConfig)
    #: Per-model pricing overrides (USD / 1M tokens), merged over Manta's
    #: built-in table. Service keys are exact FQNs; legacy endpoint keys retain
    #: substring matching. Cache rates are optional and never vendor-derived.::
    #:
    #:     [pricing."my-finetune"]
    #:     input = 2.0
    #:     output = 8.0
    #:
    #: Fields: input, output, and optionally cache_read / cache_creation.
    pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)


def interactive_endpoints(cfg: MantaConfig) -> list[str]:
    """Return the deduped set of Databricks targets to register with the TUI.

    The default endpoint is listed first (it is what ``manta`` launches with),
    followed by every distinct extra endpoint so they appear in deepagents-code's
    ``/model`` switcher.
    """
    ordered = [cfg.interactive.default_endpoint, *cfg.interactive.extra_endpoints]
    return list(dict.fromkeys(e for e in ordered if e))


def project_manta_dir(root: Path | None = None) -> Path:
    if root is None:
        hint = os.environ.get("MANTA_PROJECT_ROOT")
        cwd = Path.cwd().resolve()
        root = (
            Path(hint)
            if hint
            else next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), cwd)
        )
    return root / ".manta"


def user_manta_dir() -> Path:
    return Path(os.environ.get("MANTA_HOME", Path.home() / ".manta"))


def init_project(root: Path | None = None, overwrite: bool = False) -> Path:
    manta_dir = project_manta_dir(root)
    manta_dir.mkdir(exist_ok=True)
    config_path = manta_dir / "config.toml"
    if config_path.exists() and not overwrite:
        return config_path
    config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return config_path


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(root: Path | None = None) -> MantaConfig:
    # Detached tasks run inside an isolated checkout but keep the reviewed
    # project configuration. The task manifest validates its digest on restore.
    if root is None and os.environ.get("MANTA_TASK_CONFIG_ROOT"):
        root = Path(os.environ["MANTA_TASK_CONFIG_ROOT"])
    user_cfg = load_toml(user_manta_dir() / "config.toml")
    project_cfg = load_toml(project_manta_dir(root) / "config.toml")
    merged = deep_merge(user_cfg, project_cfg)
    return MantaConfig.model_validate(merged or {})
