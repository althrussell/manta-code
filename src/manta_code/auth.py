"""Databricks authentication for Manta.

Manta runs its models on Databricks-hosted endpoints via ``ChatDatabricks``,
which authenticates through the Databricks SDK :class:`WorkspaceClient`. The SDK
already implements unified auth: it reads ``~/.databrickscfg`` profiles,
``DATABRICKS_*`` environment variables, and OAuth tokens minted by
``databricks auth login``. We therefore do not re-implement auth (unlike
``databricks/ucode`` which shells out to the CLI for everything); we only:

- resolve which *profile* to use (explicit flag > env var > default),
- expose a quick ``is_authenticated`` probe,
- list configured profiles (parsed from the config file), and
- offer an optional first-run onboarding that runs ``databricks auth login``.

The ``databricks`` SDK import is lazy so importing this module never requires
the optional ``[agent]`` extra.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from databricks.sdk import WorkspaceClient

# Environment variables (in priority order) that select a profile when no
# explicit ``--profile`` flag is given.
PROFILE_ENV_VARS = ("MANTA_PROFILE", "DATABRICKS_CONFIG_PROFILE")


@dataclass(frozen=True)
class ProfileInfo:
    """A profile entry parsed from the Databricks config file."""

    name: str
    host: str | None = None


class AuthError(RuntimeError):
    """Raised when Databricks authentication cannot be established."""


def config_file_path() -> Path:
    """Return the Databricks config file path (honors ``DATABRICKS_CONFIG_FILE``)."""
    override = os.environ.get("DATABRICKS_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".databrickscfg"


def resolve_profile(explicit: str | None = None) -> str | None:
    """Resolve the profile to use: explicit flag > env vars > ``None`` (default)."""
    if explicit:
        return explicit
    for var in PROFILE_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def list_profiles() -> list[ProfileInfo]:
    """Parse profiles from the Databricks config file.

    Returns an empty list when the file does not exist. The ``DEFAULT`` section
    is surfaced as the ``DEFAULT`` profile when it carries a host.
    """
    path = config_file_path()
    if not path.is_file():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    profiles: list[ProfileInfo] = []
    if parser.defaults().get("host"):
        profiles.append(ProfileInfo(name="DEFAULT", host=parser.defaults().get("host")))
    for section in parser.sections():
        # Skip reserved sections (e.g. ``[__settings__]`` written by the
        # Databricks CLI) — they are not authenticatable workspace profiles.
        if section.startswith("__") and section.endswith("__"):
            continue
        profiles.append(ProfileInfo(name=section, host=parser.get(section, "host", fallback=None)))
    return profiles


def resolve_workspace_client(profile: str | None = None) -> "WorkspaceClient":
    """Build a :class:`WorkspaceClient` for ``profile`` (or the default profile).

    Raises :class:`AuthError` if the Databricks SDK is not installed.
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AuthError(
            "databricks-sdk is not installed. Install with: pip install -e '.[agent]'"
        ) from exc
    name = resolve_profile(profile)
    if name:
        return WorkspaceClient(profile=name)
    return WorkspaceClient()


def is_authenticated(profile: str | None = None) -> bool:
    """Return ``True`` if a quick ``current_user.me()`` call succeeds."""
    try:
        client = resolve_workspace_client(profile)
        client.current_user.me()
        return True
    except Exception:  # noqa: BLE001 - any auth/SDK error means "not authenticated"
        return False


def current_username(profile: str | None = None) -> str | None:
    """Return the authenticated user name, or ``None`` if not authenticated."""
    try:
        client = resolve_workspace_client(profile)
        me = client.current_user.me()
        return me.user_name or me.display_name
    except Exception:  # noqa: BLE001
        return None


def run_databricks_login(profile: str | None = None, host: str | None = None) -> bool:
    """Run ``databricks auth login`` interactively. Returns ``True`` on success.

    Borrowed in spirit from ``databricks/ucode``: the Databricks CLI owns the
    OAuth dance, so we shell out to it for the one thing the SDK cannot do
    headlessly (interactive browser login).
    """
    cmd = ["databricks", "auth", "login"]
    if host:
        cmd += ["--host", host]
    if profile:
        cmd += ["--profile", profile]
    try:
        completed = subprocess.run(cmd, check=False)  # noqa: S603 - fixed argv, no shell
    except FileNotFoundError:
        return False
    return completed.returncode == 0


def ensure_auth(profile: str | None = None, *, interactive: bool = True) -> str | None:
    """Ensure a working Databricks auth context; return the resolved profile name.

    If already authenticated, returns immediately. Otherwise, when ``interactive``
    is set and a TTY is available, offers a one-time onboarding that runs
    ``databricks auth login``. Returns the profile name that authenticated, or
    ``None`` if authentication could not be established.
    """
    resolved = resolve_profile(profile)
    if is_authenticated(resolved):
        return resolved or "DEFAULT"
    if not interactive:
        return None
    # Best-effort onboarding: try CLI login for the requested/default profile.
    if run_databricks_login(profile=resolved):
        if is_authenticated(resolved):
            return resolved or "DEFAULT"
    return None
