from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


DEFAULT_CONFIG_TEXT = """\
[models]
router = "openai:gpt-5-nano"
cheap_responder = "openai:gpt-5-mini"
planner = "anthropic:claude-opus"
builder = "openai:gpt-builder"
reviewer = "google:gemini-pro"
security_reviewer = "google:gemini-pro"
release = "openai:gpt-5-mini"

[budgets]
default_task_usd = 1.00
hard_max_task_usd = 5.00
max_iterations = 3
max_turns = 30
show_cost_always = true

[autonomy]
mode = "smart_approve"
allow_file_writes = true
allow_shell = "allowlisted"
allow_network = false
allow_git_commit = "approval"
allow_git_push = false

[context]
strategy = "brokered"
auto_compact = true
store_full_history = true
repo_index = true
max_router_tokens = 4000
max_builder_tokens = 64000
max_reviewer_tokens = 128000

[review]
code_review_required = true
security_review_on_risk = true
block_on_high_severity = true
"""


class ModelsConfig(BaseModel):
    router: str = "openai:gpt-5-nano"
    cheap_responder: str = "openai:gpt-5-mini"
    planner: str = "anthropic:claude-opus"
    builder: str = "openai:gpt-builder"
    reviewer: str = "google:gemini-pro"
    security_reviewer: str = "google:gemini-pro"
    release: str = "openai:gpt-5-mini"


class BudgetsConfig(BaseModel):
    default_task_usd: float = 1.0
    hard_max_task_usd: float = 5.0
    max_iterations: int = 3
    max_turns: int = 30
    show_cost_always: bool = True


class AutonomyConfig(BaseModel):
    mode: str = "smart_approve"
    allow_file_writes: bool = True
    allow_shell: str = "allowlisted"
    allow_network: bool = False
    allow_git_commit: str = "approval"
    allow_git_push: bool = False


class ContextConfig(BaseModel):
    strategy: str = "brokered"
    auto_compact: bool = True
    store_full_history: bool = True
    repo_index: bool = True
    max_router_tokens: int = 4000
    max_builder_tokens: int = 64000
    max_reviewer_tokens: int = 128000


class ReviewConfig(BaseModel):
    code_review_required: bool = True
    security_review_on_risk: bool = True
    block_on_high_severity: bool = True


class MantaConfig(BaseModel):
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)


def project_manta_dir(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / ".manta"


def user_manta_dir() -> Path:
    return Path(os.environ.get("MANTA_HOME", Path.home() / ".manta"))


def init_project(root: Path | None = None, overwrite: bool = False) -> Path:
    manta_dir = project_manta_dir(root)
    manta_dir.mkdir(exist_ok=True)
    for child in ["sessions", "context", "reports", "memory", "policies"]:
        (manta_dir / child).mkdir(exist_ok=True)
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


def copy_example_configs(target: Path) -> None:
    """Optional helper for future packaging; not used by CLI currently."""
    source_dir = Path(__file__).resolve().parents[2] / "configs"
    if source_dir.exists():
        shutil.copytree(source_dir, target, dirs_exist_ok=True)
