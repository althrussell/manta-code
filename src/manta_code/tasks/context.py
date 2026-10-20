"""Nonsecret, durable identity for detached execution and continuation."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


class TaskContextError(RuntimeError):
    """An intended project, workspace or agent can no longer be established."""


def resolve_profile(child: str | None = None, root: str | None = None) -> str | None:
    """Child option > root option > Manta environment > SDK environment."""
    return (
        child
        or root
        or os.environ.get("MANTA_PROFILE")
        or os.environ.get("DATABRICKS_CONFIG_PROFILE")
        or None
    )


def _workspace(profile: str | None, config_file: str) -> tuple[str | None, str]:
    parser = configparser.ConfigParser(interpolation=None)
    if Path(config_file).is_file():
        parser.read(config_file, encoding="utf-8")
    effective = profile
    if effective is None and not os.environ.get("DATABRICKS_HOST"):
        effective = parser.get("__settings__", "default_profile", fallback=None) or "DEFAULT"
    raw = os.environ.get("DATABRICKS_HOST")
    if not raw and effective is not None:
        raw = parser.get(effective, "host", fallback="")
    if not raw:
        return effective, ""
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise TaskContextError(
            "Workspace host must be a plain HTTP(S) workspace URL without credentials."
        )
    host = parsed.hostname.lower()
    if parsed.port is not None:
        host += f":{parsed.port}"
    return effective, f"{parsed.scheme}://{host}"


def _agent_definition(name: str):
    from ..agents.defaults import merged_agents
    from ..agents.registry import list_agents

    defn = next((a for a in merged_agents(list_agents()) if a.name == name), None)
    if defn is None:
        raise TaskContextError(f"Agent '{name}' is no longer configured.")
    return defn


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(defn, config_root: Path | None = None) -> dict:
    # Store identities, not prompts, credentials, config contents or arbitrary
    # environment variables. Changed roles/policies block execution on restore.
    data = defn.model_dump(mode="json")
    from ..config import load_config

    effective_model = (
        defn.model or f"databricks:{load_config(config_root).interactive.default_endpoint}"
    )
    return {
        "agent": defn.name,
        "model": effective_model,
        "agent_digest": _digest(data),
        "policy_digest": _digest(
            {k: v for k, v in data.items() if k not in {"description", "system_prompt", "model"}}
        ),
    }


def _project_identity(root: Path) -> dict:
    info = root.stat()
    result = {"device": info.st_dev, "inode": info.st_ino}
    marker = root / ".git"
    git_dir = marker
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            git_dir = (root / text.removeprefix("gitdir:").strip()).resolve()
    head = git_dir / "HEAD"
    if head.is_file():
        identity = head.read_text(encoding="utf-8").strip()
        # Detached heads are pinned; branch commits may advance as work lands.
        result["git_dir"] = str(git_dir.resolve())
        result["branch"] = identity
    return result


def build_execution_manifest(
    agent: str, profile: str | None = None, *, cwd: str | None = None
) -> dict:
    from ..auth import config_file_path

    selected = (
        cwd or os.environ.get("DEEPAGENTS_CODE_SERVER_CWD") or os.environ.get("MANTA_ORIGINAL_CWD")
    )
    try:
        original = (
            Path(selected).expanduser().resolve(strict=True)
            if selected
            else Path.cwd().resolve(strict=True)
        )
    except OSError as exc:
        raise TaskContextError(
            "The original working directory is unavailable; select a valid project before submitting."
        ) from exc
    if not original.is_dir():
        raise TaskContextError("The original working directory is not a directory.")
    root_hint = os.environ.get("DEEPAGENTS_CODE_SERVER_PROJECT_ROOT")
    root = (
        Path(root_hint).expanduser().resolve(strict=True)
        if root_hint
        else next(
            (p for p in (original, *original.parents) if (p / ".git").exists()),
            original,
        )
    )
    if not original.is_relative_to(root):
        raise TaskContextError("The working directory is outside the recorded project root.")
    config_file = str(config_file_path().resolve())
    profile, workspace = _workspace(resolve_profile(profile), config_file)
    defn = _agent_definition(agent)
    from ..config import load_config

    config_root = Path(os.environ.get("MANTA_TASK_CONFIG_ROOT", root)).resolve(strict=True)
    config_digest = _digest(load_config(config_root).model_dump(mode="json"))
    if _identity(defn, config_root)["model"].startswith("databricks:") and not workspace:
        raise TaskContextError(
            "Cannot establish the Databricks workspace host for this agent; select a configured profile."
        )
    parent_thread = ""
    try:
        from langgraph.config import get_config

        parent_thread = get_config().get("configurable", {}).get("thread_id", "")
    except (ImportError, RuntimeError):
        pass  # CLI submission is outside a graph invocation.
    return {
        "schema_version": 1,
        "original_cwd": str(original),
        "project_root": str(root),
        "profile": profile,
        "workspace": workspace,
        "config_file": config_file,
        "parent_task_id": os.environ.get("MANTA_TASK_ID", ""),
        "parent_thread_id": parent_thread,
        "project_identity": _project_identity(root),
        "config_root": str(config_root),
        "runtime_config_digest": config_digest,
        "task_shell_allow_list": defn.task_shell_allow_list,
        **_identity(defn, config_root),
    }


def validate_execution_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {1, 2}:
        raise TaskContextError(
            "Legacy task has no verified execution context; submit a new task from its intended project."
        )
    required = (
        "original_cwd",
        "project_root",
        "agent",
        "config_file",
        "model",
        "agent_digest",
        "policy_digest",
    )
    if any(not isinstance(manifest.get(key), str) or not manifest[key] for key in required):
        raise TaskContextError(
            "The task execution context is incomplete; submit a new task from its intended project."
        )
    try:
        cwd = Path(manifest["original_cwd"]).resolve(strict=True)
        root = Path(manifest["project_root"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise TaskContextError(
            "Recorded project/working directory is unavailable; restore it before continuing."
        ) from exc
    if not cwd.is_dir() or not root.is_dir() or not cwd.is_relative_to(root):
        raise TaskContextError("Recorded working directory no longer belongs to the project.")
    if str(cwd) != manifest["original_cwd"] or str(root) != manifest["project_root"]:
        raise TaskContextError("Recorded project path now resolves to a different directory.")
    actual = _project_identity(root)
    recorded = manifest.get("project_identity", {})
    # A task may commit its work while retaining the same owned worktree.
    if manifest.get("schema_version") == 2:
        actual = {k: v for k, v in actual.items() if k != "branch"}
        recorded = {k: v for k, v in recorded.items() if k != "branch"}
    if actual != recorded:
        raise TaskContextError(
            "The project identity or Git branch changed; restore the intended checkout before continuing."
        )
    source_root = Path(manifest.get("config_root", manifest.get("source_project_root", root)))
    if manifest.get("runtime_config_digest"):
        from ..config import load_config

        if (
            _digest(load_config(source_root).model_dump(mode="json"))
            != manifest["runtime_config_digest"]
        ):
            raise TaskContextError(
                "The project runtime configuration changed after submission; restore the reviewed configuration or submit a new task"
            )
    current = _identity(_agent_definition(manifest["agent"]), source_root)
    if any(current[k] != manifest.get(k) for k in ("agent_digest", "policy_digest", "model")):
        raise TaskContextError(
            "The agent/model/policy changed after submission; review the new definition and submit a new task."
        )
    _, workspace = _workspace(manifest.get("profile"), manifest["config_file"])
    if workspace != manifest.get("workspace"):
        raise TaskContextError(
            "The recorded profile now resolves to a different workspace; restore the intended profile before continuing."
        )


@contextmanager
def execution_context(manifest: dict) -> Iterator[None]:
    """Restore only reviewed identity fields; leave credential resolution to SDK."""
    validate_execution_manifest(manifest)
    keys = {
        "MANTA_ORIGINAL_CWD": manifest["original_cwd"],
        "MANTA_PROJECT_ROOT": manifest["project_root"],
        "MANTA_TASK_CONFIG_ROOT": manifest.get(
            "config_root", manifest.get("source_project_root", manifest["project_root"])
        ),
        "DEEPAGENTS_CODE_SERVER_CWD": manifest["original_cwd"],
        "DEEPAGENTS_CODE_SERVER_PROJECT_ROOT": manifest["project_root"],
        "DATABRICKS_CONFIG_FILE": manifest["config_file"],
        "MANTA_PROFILE": manifest.get("profile"),
        "DATABRICKS_CONFIG_PROFILE": manifest.get("profile"),
    }
    before = {key: os.environ.get(key) for key in keys}
    try:
        previous_cwd = Path.cwd()
    except OSError:
        previous_cwd = None
    os.chdir(manifest["original_cwd"])
    try:
        for key, value in keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if previous_cwd is not None and previous_cwd.is_dir():
            os.chdir(previous_cwd)
