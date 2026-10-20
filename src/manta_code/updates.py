"""Manta-owned release checks and qualified, tag-pinned upgrades."""

from __future__ import annotations

import json
import errno
import hashlib
import importlib
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from . import __version__

REPOSITORY = "althrussell/manta-code"
TAG_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

QUALIFIED_DEPENDENCY_GUIDANCE = (
    "Manta preserves the dependencies qualified for its release. Automatic "
    "extra/package installation, removal and dependency-only refresh are unavailable. "
    "Use `manta update --check` to check releases and `manta update` to install a "
    "complete qualified release. For an optional integration, use a separate "
    "development environment, resolve its dependencies against that Manta "
    "checkout's requirements-dev.lock, and verify dependency consistency and tests."
)

_SAFE_RECOVERY_COMMAND = "manta update --check"
_DEPENDENCY_COMMAND_BUILDERS = (
    "upgrade_install_command", "dependency_refresh_command", "dependency_refresh_dry_run_command",
    "install_package_command", "install_extras_command", "install_extra_command",
    "install_extra_recovery_command", "safe_install_extra_recovery_command", "uninstall_extra_command",
    "format_shadowed_dcode_fix_command",
)
_DEPENDENCY_TUI_METHODS = (
    "_handle_install_command", "_handle_uninstall_command", "_handle_install_package",
    "_install_extra", "_install_extra_unlocked", "_uninstall_extra_unlocked",
    "_perform_package_install", "_perform_package_install_unlocked",
    "_refresh_dependencies", "_refresh_dependencies_unlocked",
)
_DEPENDENCY_FALLBACK_CONSTANTS = (
    "INSTALL_SCRIPT_COMMAND", "FALLBACK_UPGRADE_COMMAND", "_UV_PRERELEASE_UPGRADE_COMMAND",
)

# Both doctor and the adapter tests use the same seam inventory. A dependency
# bump must not silently remove a protected install or recovery path.
UPDATE_ADAPTER_TARGETS = (
    *(("deepagents_code.main", name, "callable") for name in (
        "_run_startup_auto_update", "_restart_current_process",
    )),
    *(("deepagents_code.app", "DeepAgentsApp." + name, "callable") for name in (
        "_handle_update_command", "_handle_auto_update_toggle", "_check_for_updates_impl",
        *_DEPENDENCY_TUI_METHODS,
    )),
    *(("deepagents_code.update_check", name, "callable") for name in (
        "is_auto_update_enabled", "set_auto_update", "is_update_available", "upgrade_command",
        "perform_upgrade", "perform_install_extra", "perform_uninstall_extra", "perform_install_package",
        "perform_dependency_refresh", "perform_dependency_refresh_dry_run", "dependency_refresh_supported",
        "prerelease_upgrade_supported", "uninstall_extra_method_error", "editable_extra_hint",
        "editable_extra_removal_hint", "editable_package_hint",
        *_DEPENDENCY_COMMAND_BUILDERS,
    )),
    *(("deepagents_code.update_check", name, "attr") for name in _DEPENDENCY_FALLBACK_CONSTANTS),
    ("deepagents_code.update_check", "_PRERELEASE_UNSUPPORTED_MESSAGE", "attr"),
    *(("deepagents_code.client.commands.extras", name, "callable") for name in (
        "run_install_request", "run_uninstall_request",
    )),
    *(("deepagents_code._dep_floor_check", name, "callable") for name in (
        "_refresh_environment", "refresh_command",
    )),
)


@dataclass(frozen=True)
class Release:
    tag: str
    url: str

    @property
    def version(self) -> tuple[int, ...]:
        match = TAG_PATTERN.fullmatch(self.tag)
        if match is None:
            raise ValueError("Release must be a stable vMAJOR.MINOR.PATCH tag")
        return tuple(map(int, match.groups()))

    @property
    def newer(self) -> bool:
        return self.version > tuple(map(int, __version__.split(".")))


def check_release(tag: str | None = None) -> Release:
    if tag and not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"Use a stable Manta release tag such as v{__version__}")
    suffix = f"tags/{tag}" if tag else "latest"
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/{suffix}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "manta-code"},
    )
    with urlopen(request, timeout=10) as response:
        data = json.load(response)
    release = Release(str(data.get("tag_name", "")), str(data.get("html_url", "")))
    release.version  # Validate untrusted release metadata before constructing argv.
    if data.get("draft") or data.get("prerelease") or not release.url.startswith(
        f"https://github.com/{REPOSITORY}/releases/"
    ):
        raise ValueError("Release metadata does not describe a published stable Manta release")
    return release


def install_method() -> str:
    try:
        direct = json.loads(metadata.distribution("manta-code").read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return "editable"
    except (metadata.PackageNotFoundError, ValueError, TypeError):
        pass
    # uv tool receipts live beside this tool's interpreter, not in some other
    # PATH winner's environment. Never update deepagents-code independently.
    if (Path(sys.prefix) / "uv-receipt.toml").is_file():
        return "uv"
    return "pip"


def upgrade_argv(release: Release, method: str | None = None) -> list[str]:
    release.version
    method = method or install_method()
    constraints = f"https://raw.githubusercontent.com/{REPOSITORY}/{release.tag}/requirements-dev.lock"
    requirement = f"manta-code[agent] @ git+https://github.com/{REPOSITORY}.git@{release.tag}"
    if method == "uv":
        return ["uv", "tool", "install", "--force", "--upgrade", "--python", sys.executable,
                "--constraints", constraints, requirement]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall",
                "--constraint", constraints, requirement]
    if method == "editable":
        raise ValueError(
            "Editable checkout: update the repository deliberately, then sync its "
            "requirements-dev.lock and reinstall .[agent]. Manta will not replace your source checkout."
        )
    raise ValueError(f"Unsupported Manta installation method: {method}")


def installation_lock_path() -> Path:
    """Keep the lock beside the installation so uv replacement cannot erase it.

    MANTA_HOME and the current project can differ between processes using one
    interpreter. Neither participates in the lock identity. The lock file stays
    in place after release to avoid creating competing lock-file inodes.
    """
    prefix = Path(sys.prefix).resolve()
    identity = hashlib.sha256(os.path.normcase(str(prefix)).encode()).hexdigest()[:24]
    return prefix.parent / f".manta-install-{identity}.lock"


@contextmanager
def installation_lock() -> Iterator[int]:
    """Acquire a nonblocking, fail-closed lock for this installed environment."""
    path = installation_lock_path()
    handle = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError as exc:
        contended = handle is not None and exc.errno in {errno.EACCES, errno.EAGAIN}
        if handle is not None:
            handle.close()
        if contended:
            raise ValueError(
                "Another Manta process is updating this installation. "
                "Wait for it to finish, then retry; no packages changed."
            ) from exc
        raise ValueError(f"Cannot lock the Manta installation at {path}: {exc}; no packages changed.") from exc
    try:
        yield handle.fileno()
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def upgrade(release: Release, method: str | None = None) -> int:
    # Validate the selected release/install method before creating state. On
    # POSIX, the installer inherits the lock descriptor so a killed launcher
    # cannot release exclusion while its still-running installer mutates files.
    argv = upgrade_argv(release, method)
    with installation_lock() as descriptor:
        options: dict[str, Any] = {"check": False}
        if os.name != "nt":
            options["pass_fds"] = (descriptor,)
        return subprocess.run(argv, **options).returncode


def restart_argv(arguments: list[str] | None = None) -> list[str]:
    return [sys.executable, "-m", "manta_code.main", *(arguments or [])]


def install_runtime_update_adapter() -> bool:
    """Own upstream update entry points without changing user dcode settings.

    Automatic dependency replacement is disabled for Manta's qualified release.
    `/update` checks Manta releases and supplies the executable Manta CLI action.
    Explicit installation is owned by `manta update`, which returns installer
    failure codes and never falls back to an unqualified dependency refresh.
    """
    for module, attribute, kind in UPDATE_ADAPTER_TARGETS:
        value = importlib.import_module(module)
        for part in attribute.split("."):
            value = getattr(value, part)
        if kind == "callable" and not callable(value):
            raise RuntimeError(f"Required Manta update adapter target is unavailable: {module}.{attribute}")

    from deepagents_code import main as upstream_main
    from deepagents_code import update_check
    from deepagents_code.app import DeepAgentsApp
    from deepagents_code.tui.widgets.messages import AppMessage

    import asyncio

    def qualified_auto_update() -> bool:
        return False

    update_check.is_auto_update_enabled = qualified_auto_update
    update_check.set_auto_update = lambda *a, **k: None
    upstream_main._run_startup_auto_update = lambda *a, **k: None
    update_check.is_update_available = lambda *a, **k: (False, None)
    update_check.upgrade_command = lambda *a, **k: "manta update"

    async def refuse_dependency_update(**kwargs: Any) -> tuple[bool, str, None]:
        return False, "Use manta update to install a complete qualified Manta release.", None

    update_check.perform_upgrade = refuse_dependency_update

    # Optional dependency actions rebuild dcode's environment independently of
    # Manta. Own both their entry points and performers: notification actions or
    # install-on-select must not bypass the slash/CLI command guard.
    async def refuse_extra(*args: Any, **kwargs: Any) -> Any:
        return update_check.ExtraInstallOutcome(False, QUALIFIED_DEPENDENCY_GUIDANCE, manual_recovery_safe=False)

    async def refuse_removal(*args: Any, **kwargs: Any) -> Any:
        return update_check.ExtraRemovalOutcome(False, QUALIFIED_DEPENDENCY_GUIDANCE, manual_recovery_safe=False)

    async def refuse_dependency_action(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        return False, QUALIFIED_DEPENDENCY_GUIDANCE

    def recovery_command(*args: Any, **kwargs: Any) -> str:
        # Always ignore upstream fallback argv, including an exception-path
        # fallback supplied by the caller. This command is read-only.
        return _SAFE_RECOVERY_COMMAND

    update_check.perform_install_extra = refuse_extra
    update_check.perform_uninstall_extra = refuse_removal
    update_check.perform_install_package = refuse_dependency_action
    update_check.perform_dependency_refresh = refuse_dependency_action
    update_check.perform_dependency_refresh_dry_run = refuse_dependency_action
    update_check.dependency_refresh_supported = lambda *a, **k: (False, QUALIFIED_DEPENDENCY_GUIDANCE)
    update_check.prerelease_upgrade_supported = lambda *a, **k: (False, QUALIFIED_DEPENDENCY_GUIDANCE)
    update_check.uninstall_extra_method_error = lambda *a, **k: QUALIFIED_DEPENDENCY_GUIDANCE
    update_check.editable_extra_hint = lambda *a, **k: QUALIFIED_DEPENDENCY_GUIDANCE
    update_check.editable_extra_removal_hint = lambda *a, **k: QUALIFIED_DEPENDENCY_GUIDANCE
    update_check.editable_package_hint = lambda *a, **k: QUALIFIED_DEPENDENCY_GUIDANCE
    update_check._PRERELEASE_UNSUPPORTED_MESSAGE = QUALIFIED_DEPENDENCY_GUIDANCE
    for name in _DEPENDENCY_COMMAND_BUILDERS:
        setattr(update_check, name, recovery_command)
    for name in _DEPENDENCY_FALLBACK_CONSTANTS:
        setattr(update_check, name, _SAFE_RECOVERY_COMMAND)

    from deepagents_code.client.commands import extras
    from deepagents_code import _dep_floor_check

    def refuse_cli_mutation(*args: Any, **kwargs: Any) -> int:
        from rich.console import Console

        Console(stderr=True).print(QUALIFIED_DEPENDENCY_GUIDANCE, markup=False)
        return 1

    def refuse_editable_refresh(console: Any) -> bool:
        console.print(QUALIFIED_DEPENDENCY_GUIDANCE, markup=False)
        return False

    extras.run_install_request = refuse_cli_mutation
    extras.run_uninstall_request = refuse_cli_mutation
    _dep_floor_check._refresh_environment = refuse_editable_refresh
    _dep_floor_check.refresh_command = recovery_command

    async def refuse_tui_mutation(self: Any, *args: Any, **kwargs: Any) -> bool:
        await self._mount_message(AppMessage(QUALIFIED_DEPENDENCY_GUIDANCE))
        return False

    for name in _DEPENDENCY_TUI_METHODS:
        setattr(DeepAgentsApp, name, refuse_tui_mutation)

    async def handle_update(self: Any, command: str = "/update") -> None:
        if command.strip() not in {"/update", "/update --check"}:
            await self._mount_message(AppMessage(
                "Manta uses qualified GitHub releases. Use /update to check, then "
                "manta update --version vMAJOR.MINOR.PATCH to install the complete release."
            ))
            return
        try:
            release = await asyncio.to_thread(check_release)
            text = f"Manta {__version__}; latest release {release.tag}: {release.url}"
            if release.newer:
                try:
                    upgrade_argv(release)  # Check support before suggesting an install.
                    text += f"\nSave your work, exit Manta, then run:\nmanta update --version {release.tag}"
                except ValueError as exc:
                    text += f"\n{exc}"
        except Exception as exc:
            text = f"Manta release check failed ({type(exc).__name__}). No packages changed."
        await self._mount_message(AppMessage(text))

    async def auto_update_notice(self: Any) -> None:
        await self._mount_message(AppMessage(
            "Manta updates the complete qualified release. Use /update to check; "
            "independent automatic dcode upgrades are disabled."
        ))

    DeepAgentsApp._handle_update_command = handle_update
    DeepAgentsApp._handle_auto_update_toggle = auto_update_notice

    async def startup_check(self: Any, *, periodic: bool = False) -> None:
        try:
            release = await asyncio.to_thread(check_release)
            if release.newer:
                await self._mount_message(AppMessage(
                    f"Manta release {release.tag} is available. Use /update for qualified install instructions."
                ))
        except Exception:
            # Offline use is supported. A manual /update reports check failures.
            return

    DeepAgentsApp._check_for_updates_impl = startup_check
    # Startup success must re-enter Manta even if another integration calls it.
    def restart(*args: Any, **kwargs: Any) -> None:
        import os
        argv = restart_argv(sys.argv[1:])
        os.execv(argv[0], argv)

    upstream_main._restart_current_process = restart
    return True
