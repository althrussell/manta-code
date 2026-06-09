from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


DEFAULT_CONFIG_TEXT = """\
# Manta launches deepagents-code's interactive TUI preconfigured for Databricks.
# `provider` is fixed to databricks; Manta wires ChatDatabricks into
# deepagents-code via a class_path provider in ~/.deepagents/config.toml.
[runtime]
provider = "databricks"

# `default_endpoint` is the Databricks Model Serving / Foundation Model API
# endpoint that `manta` launches with (passed as `databricks:<endpoint>`).
# `extra_endpoints` are also registered in deepagents-code's `/model` switcher
# so you can switch between them in-session.
[interactive]
default_endpoint = "databricks-claude-sonnet-4-5"
extra_endpoints = [
    "databricks-meta-llama-3-3-70b-instruct",
]
"""


class RuntimeConfig(BaseModel):
    provider: str = "databricks"


class InteractiveConfig(BaseModel):
    default_endpoint: str = "databricks-claude-sonnet-4-5"
    extra_endpoints: list[str] = Field(
        default_factory=lambda: ["databricks-meta-llama-3-3-70b-instruct"]
    )


class MantaConfig(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    interactive: InteractiveConfig = Field(default_factory=InteractiveConfig)


def interactive_endpoints(cfg: MantaConfig) -> list[str]:
    """Return the deduped set of Databricks endpoints to register with the TUI.

    The default endpoint is listed first (it is what ``manta`` launches with),
    followed by every distinct extra endpoint so they appear in deepagents-code's
    ``/model`` switcher.
    """
    ordered = [cfg.interactive.default_endpoint, *cfg.interactive.extra_endpoints]
    return list(dict.fromkeys(e for e in ordered if e))


def project_manta_dir(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / ".manta"


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
    user_cfg = load_toml(user_manta_dir() / "config.toml")
    project_cfg = load_toml(project_manta_dir(root) / "config.toml")
    merged = deep_merge(user_cfg, project_cfg)
    return MantaConfig.model_validate(merged or {})
