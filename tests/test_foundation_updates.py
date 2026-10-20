from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from types import SimpleNamespace
from types import MethodType

import pytest
from typer.testing import CliRunner

from manta_code import main, updates


@pytest.fixture(autouse=True)
def restore_update_adapter_targets(monkeypatch):
    """Exercise real installed seams without leaving mutations for other tests."""
    for module, attribute, _kind in updates.UPDATE_ADAPTER_TARGETS:
        owner = importlib.import_module(module)
        parts = attribute.split(".")
        for part in parts[:-1]:
            owner = getattr(owner, part)
        monkeypatch.setattr(owner, parts[-1], getattr(owner, parts[-1]))


def forbid_installer(*args, **kwargs):
    pytest.fail("An unqualified dependency installer was reached")


def test_release_check_rejects_untrusted_tags(monkeypatch):
    class Response:
        def __enter__(self):
            import io
            return io.StringIO(json.dumps({"tag_name": "v0.4.0", "html_url": "https://github.com/althrussell/manta-code/releases/tag/v0.4.0"}))
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(updates, "urlopen", lambda *a, **k: Response())
    assert updates.check_release().tag == "v0.4.0"
    with pytest.raises(ValueError):
        updates.check_release("main;bad")


@pytest.mark.parametrize("method", ["pip", "uv"])
def test_upgrade_pins_manta_and_complete_constraints(method):
    argv = updates.upgrade_argv(updates.Release("v0.4.0", ""), method)
    assert any("manta-code[agent]" in arg and "@v0.4.0" in arg for arg in argv)
    assert any("v0.4.0/requirements-dev.lock" in arg for arg in argv)
    assert not any(arg == "deepagents-code" for arg in argv)
    assert "manta_code.main" in updates.restart_argv(["-r", "thread"])


def test_editable_update_preserves_checkout():
    with pytest.raises(ValueError, match="Editable checkout"):
        updates.upgrade_argv(updates.Release("v0.4.0", ""), "editable")


def test_cli_propagates_installer_failure(monkeypatch):
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    monkeypatch.setattr(updates, "check_release", lambda tag=None: updates.Release("v0.4.0", "url"))
    monkeypatch.setattr(updates, "install_method", lambda: "pip")
    monkeypatch.setattr(updates, "upgrade", lambda *a: 7)
    assert CliRunner().invoke(main.app, ["update"]).exit_code == 7
    assert CliRunner().invoke(main.app, ["update", "--check"]).exit_code == 0


def test_runtime_update_adapter_owns_all_update_surfaces(monkeypatch):
    from deepagents_code import main as upstream
    from deepagents_code import update_check
    from deepagents_code.app import DeepAgentsApp

    # Restore patched globals after this test while exercising the real adapter.
    for obj, name in ((upstream, "_run_startup_auto_update"), (upstream, "_restart_current_process"),
                      (update_check, "is_auto_update_enabled"), (update_check, "set_auto_update"),
                      (update_check, "is_update_available"), (update_check, "perform_upgrade"),
                      (update_check, "upgrade_command"), (DeepAgentsApp, "_handle_update_command"),
                      (DeepAgentsApp, "_handle_auto_update_toggle"), (DeepAgentsApp, "_check_for_updates_impl")):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    updates.install_runtime_update_adapter()
    assert update_check.is_auto_update_enabled() is False
    assert update_check.upgrade_command() == "manta update"
    assert asyncio.run(update_check.perform_upgrade())[0] is False
    mounted = []
    async def mount(message):
        mounted.append(message)
    monkeypatch.setattr(updates, "check_release", lambda: updates.Release("v0.4.0", "url"))
    asyncio.run(DeepAgentsApp._handle_update_command(SimpleNamespace(_mount_message=mount)))
    assert mounted


def test_runtime_group_help_routes_to_upstream():
    assert main.classify_args(["mcp", "--help"])[0] == "launch"
    assert main.classify_args(["--help"])[0] == "delegate"


def test_optional_dependency_performers_and_recovery_never_mutate(monkeypatch):
    from deepagents_code import update_check

    monkeypatch.setattr(update_check, "_run_install_subprocess", forbid_installer)
    updates.install_runtime_update_adapter()
    for function, argument in (
        (update_check.perform_install_extra, "daytona"),
        (update_check.perform_uninstall_extra, "daytona"),
        (update_check.perform_install_package, "custom-provider"),
    ):
        outcome = asyncio.run(function(argument))
        assert outcome[0] is False
        assert "qualified" in outcome[1]
    assert asyncio.run(update_check.perform_dependency_refresh())[0] is False
    assert asyncio.run(update_check.perform_dependency_refresh_dry_run())[0] is False
    assert update_check.dependency_refresh_supported("uv")[0] is False
    assert update_check.prerelease_upgrade_supported("uv")[0] is False
    assert "qualified" in update_check.uninstall_extra_method_error("daytona")
    assert "qualified" in update_check.editable_extra_removal_hint("daytona")
    assert "qualified" in update_check._PRERELEASE_UNSUPPORTED_MESSAGE
    for name in updates._DEPENDENCY_COMMAND_BUILDERS:
        # A caller-provided unsafe exception fallback must also be discarded.
        command = getattr(update_check, name)("daytona", fallback="uv tool upgrade deepagents-code")
        assert command == "manta update --check"
    for name in updates._DEPENDENCY_FALLBACK_CONSTANTS:
        assert getattr(update_check, name) == "manta update --check"


def test_missing_dependency_guard_target_fails_before_partial_install(monkeypatch):
    from deepagents_code import update_check

    original = update_check.perform_upgrade
    monkeypatch.setattr(update_check, "perform_install_extra", None)
    with pytest.raises(RuntimeError, match="perform_install_extra"):
        updates.install_runtime_update_adapter()
    assert update_check.perform_upgrade is original


@pytest.mark.parametrize("arguments", [
    ["install", "daytona"], ["uninstall", "daytona"],
    ["install", "custom-provider", "--package", "--yes"],
    ["--install", "daytona"], ["--uninstall", "daytona"],
    ["--install", "custom-provider", "--package", "--yes"],
])
def test_real_upstream_cli_dependency_routes_refuse(arguments):
    # dcode deliberately accepts one CLI provider per process. Separate real
    # invocations prove parser/dispatch behavior without leaking resolver state.
    script = """import runpy, sys
from deepagents_code import update_check
async def forbid(*args, **kwargs):
    raise AssertionError('Unqualified installer reached')
update_check._run_install_subprocess = forbid
sys.argv = ['manta', *sys.argv[1:]]
runpy.run_module('manta_code._boot', run_name='__main__')
"""
    run = subprocess.run([sys.executable, "-c", script, *arguments], capture_output=True, text=True, check=False)
    assert run.returncode == 1
    assert "qualified" in run.stderr
    assert "Unqualified installer reached" not in run.stderr


@pytest.mark.parametrize("command", [
    "/install daytona --force", "/install custom-provider --package --force", "/uninstall daytona",
])
def test_real_tui_slash_dispatch_refuses_dependency_mutation(command, monkeypatch):
    from deepagents_code import update_check
    from deepagents_code.app import DeepAgentsApp
    from textual.screen import ModalScreen

    monkeypatch.setattr(update_check, "_run_install_subprocess", forbid_installer)
    updates.install_runtime_update_adapter()
    mounted = []

    async def mount(message):
        mounted.append(message)

    app = SimpleNamespace(_mount_message=mount, screen=ModalScreen())
    for name in updates._DEPENDENCY_TUI_METHODS:
        setattr(app, name, MethodType(getattr(DeepAgentsApp, name), app))
    asyncio.run(DeepAgentsApp._handle_command(app, command))
    assert len(mounted) == 1
    assert "qualified" in mounted[0]._content


@pytest.mark.parametrize("method", updates._DEPENDENCY_TUI_METHODS)
def test_direct_notification_and_install_on_select_routes_refuse(method, monkeypatch):
    from deepagents_code import update_check
    from deepagents_code.app import DeepAgentsApp

    monkeypatch.setattr(update_check, "_run_install_subprocess", forbid_installer)
    updates.install_runtime_update_adapter()
    mounted = []

    async def mount(message):
        mounted.append(message)

    result = asyncio.run(getattr(DeepAgentsApp, method)(SimpleNamespace(_mount_message=mount), "daytona"))
    assert result is False
    assert "qualified" in mounted[0]._content


def test_editable_dependency_floor_refresh_is_guarded(monkeypatch):
    from deepagents_code import _dep_floor_check

    monkeypatch.setattr(_dep_floor_check.subprocess, "run", forbid_installer)
    updates.install_runtime_update_adapter()
    lines = []
    assert _dep_floor_check._refresh_environment(SimpleNamespace(print=lambda text, **kw: lines.append(text))) is False
    assert "qualified" in lines[0]
    assert _dep_floor_check.refresh_command() == "manta update --check"


def test_installation_lock_identity_does_not_depend_on_project_or_manta_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "environment"))
    first = updates.installation_lock_path()
    monkeypatch.setenv("MANTA_HOME", str(tmp_path / "another-home"))
    monkeypatch.chdir(tmp_path)
    assert updates.installation_lock_path() == first
    assert first.parent == tmp_path


def test_installation_lock_contends_across_processes(monkeypatch, tmp_path):
    lock = tmp_path / "installation.lock"
    monkeypatch.setattr(updates, "installation_lock_path", lambda: lock)
    script = """from pathlib import Path
import sys
from manta_code import updates
updates.installation_lock_path = lambda: Path(sys.argv[1])
try:
    with updates.installation_lock():
        raise SystemExit('unexpectedly acquired')
except ValueError as exc:
    print(exc)
    raise SystemExit(23)
"""
    with updates.installation_lock():
        contender = subprocess.run([sys.executable, "-c", script, str(lock)], capture_output=True, text=True, check=False)
    assert contender.returncode == 23
    assert "Another Manta process" in contender.stdout
    assert lock.is_file()
    with updates.installation_lock():
        pass  # A completed updater releases the same persistent lock inode.


def test_busy_upgrade_never_starts_installer_and_lock_recovers(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "installation_lock_path", lambda: tmp_path / "installation.lock")
    calls = []

    def installer(argv, **options):
        calls.append(argv)
        assert options["check"] is False
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(updates.subprocess, "run", installer)
    release = updates.Release("v0.4.0", "")
    with updates.installation_lock():
        with pytest.raises(ValueError, match="Another Manta process"):
            updates.upgrade(release, "pip")
    assert not calls
    assert updates.upgrade(release, "pip") == 7
    assert len(calls) == 1


def test_unusable_installation_lock_fails_without_installer(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(updates, "installation_lock_path", lambda: blocker / "installation.lock")
    monkeypatch.setattr(updates.subprocess, "run", forbid_installer)
    with pytest.raises(ValueError, match="Cannot lock.*no packages changed"):
        updates.upgrade(updates.Release("v0.4.0", ""), "pip")


def test_cli_busy_update_reports_refusal_without_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    monkeypatch.setattr(updates, "installation_lock_path", lambda: tmp_path / "installation.lock")
    monkeypatch.setattr(updates, "check_release", lambda tag=None: updates.Release("v0.4.0", "url"))
    monkeypatch.setattr(updates, "install_method", lambda: "pip")
    monkeypatch.setattr(updates.subprocess, "run", forbid_installer)
    with updates.installation_lock():
        result = CliRunner().invoke(main.app, ["update"])
    assert result.exit_code == 1
    assert "Another Manta process" in result.output
    assert "no packages changed" in result.output
