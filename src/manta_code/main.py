from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import init_project, interactive_endpoints, load_config, project_manta_dir

app = typer.Typer(
    help="Manta Code — Databricks-preconfigured launcher for the deepagents-code coding agent",
)
console = Console()

#: Manta's own subcommands. Anything else (bare invocation, unknown flags) is
#: treated as a request to launch the interactive runtime and is forwarded.
KNOWN_SUBCOMMANDS = frozenset({"doctor", "init"})


def classify_args(argv: list[str]) -> tuple[str, Optional[str], list[str]]:
    """Decide whether to delegate to Typer or launch the interactive runtime.

    Returns ``("delegate", None, None)`` when a Manta subcommand or help/version
    flag is present (let Typer parse), or ``("launch", profile, passthrough)``
    when ``manta`` should start the deepagents-code TUI. ``-p/--profile`` is
    extracted in launch mode; all other tokens are forwarded verbatim.
    """
    profile: Optional[str] = None
    passthrough: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-p", "--profile"):
            if i + 1 < len(argv):
                profile = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("-p") and len(arg) > 2:
            profile = arg[2:]
            i += 1
            continue
        if arg in ("-h", "--help", "--version"):
            return ("delegate", None, None)
        if not arg.startswith("-"):
            if arg in KNOWN_SUBCOMMANDS:
                return ("delegate", None, None)
            passthrough.append(arg)
            i += 1
            continue
        passthrough.append(arg)
        i += 1
    return ("launch", profile, passthrough)


def main_entry() -> None:
    """Console-script entry point (``manta``).

    Routes Manta subcommands through Typer and everything else (bare launch,
    deepagents-code passthrough flags) directly to the interactive runtime.
    """
    import sys

    mode, profile, passthrough = classify_args(sys.argv[1:])
    if mode == "delegate":
        app()
        return
    _launch_interactive(profile=profile, passthrough=passthrough or [])


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None, "-p", "--profile", help="Databricks config profile (default: env or default profile)"
    ),
) -> None:
    """Manta Code. Run with no subcommand to launch the interactive coding agent.

    Unrecognized options are forwarded to the underlying deepagents-code TUI, so
    `manta -r`, `manta -a <agent>`, `manta --skill <name>`, etc. all work.
    """
    ctx.obj = {"profile": profile}
    if ctx.invoked_subcommand is not None:
        return
    _launch_interactive(profile=profile, passthrough=list(ctx.args))
    raise typer.Exit()


@contextmanager
def _databricks_profile_env(profile: Optional[str]) -> Iterator[None]:
    """Temporarily set ``DATABRICKS_CONFIG_PROFILE`` for SDK-backed checks."""
    if not profile:
        yield
        return
    previous = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
        else:
            os.environ["DATABRICKS_CONFIG_PROFILE"] = previous


def _launch_interactive(*, profile: Optional[str], passthrough: list[str]) -> None:
    """Provision Databricks config and launch the deepagents-code TUI."""
    from importlib.util import find_spec

    if find_spec("deepagents_code") is None:
        console.print(
            "[red]The interactive runtime (deepagents-code) is not installed.[/red]\n"
            "Install it with: [bold]pip install -e '.[agent]'[/bold]"
        )
        raise typer.Exit(code=1)

    from . import dcode

    cfg = load_config()
    try:
        dcode.launch(
            profile=profile,
            default_endpoint=cfg.interactive.default_endpoint,
            endpoints=interactive_endpoints(cfg),
            passthrough=passthrough,
        )
    except dcode.LauncherError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check local Manta setup, dependencies, and Databricks auth."""
    profile = (ctx.obj or {}).get("profile")
    console.print(f"[bold]Manta Code[/bold] {__version__}")
    console.print(f"Project: {Path.cwd()}")
    console.print(f"Manta dir: {project_manta_dir()}")

    table = Table(title="Preflight")
    table.add_column("Check")
    table.add_column("OK")
    table.add_column("Detail")
    all_ok = True

    def add(name: str, ok: bool, detail: str = "") -> None:
        nonlocal all_ok
        all_ok = all_ok and ok
        table.add_row(name, "[green]yes[/green]" if ok else "[red]no[/red]", detail)

    from importlib.util import find_spec

    dcode_ok = find_spec("deepagents_code") is not None
    add(
        "deepagents-code",
        dcode_ok,
        "interactive runtime" if dcode_ok else "pip install -e '.[agent]'",
    )

    try:
        import databricks_langchain  # noqa: F401

        add("databricks-langchain", True, "")
    except Exception as exc:  # noqa: BLE001
        add("databricks-langchain", False, f"pip install -e '.[agent]' ({exc})")

    cfg = load_config()
    endpoint = cfg.interactive.default_endpoint

    # Provision + validate the Databricks provider in deepagents-code's config.
    # This is offline (no model call) and idempotent.
    if dcode_ok:
        try:
            from . import dcode
            from .auth import resolve_profile

            path = dcode.ensure_dcode_config(
                interactive_endpoints(cfg), default_endpoint=endpoint
            )
            dcode.mark_onboarding_complete()
            add("dcode config", True, str(path))
            try:
                from deepagents_code.config import create_model

                with _databricks_profile_env(resolve_profile(profile)):
                    result = create_model(f"databricks:{endpoint}")
                add("model wiring", True, type(result.model).__name__)
            except Exception as exc:  # noqa: BLE001
                add("model wiring", False, f"{endpoint}: {exc}")
        except Exception as exc:  # noqa: BLE001
            add("dcode config", False, str(exc))

    try:
        from .auth import current_username, is_authenticated, resolve_profile

        authed = is_authenticated(profile)
        who = current_username(profile) if authed else (resolve_profile(profile) or "default profile")
        add("databricks auth", authed, who or "")
    except Exception as exc:  # noqa: BLE001
        add("databricks auth", False, str(exc))

    console.print(table)
    console.print("[green]Status: OK[/green]" if all_ok else "[yellow]Status: issues found[/yellow]")


@app.command()
def init(overwrite: bool = typer.Option(False, help="Overwrite existing .manta/config.toml")) -> None:
    """Initialize .manta project config (launcher settings: endpoints)."""
    path = init_project(overwrite=overwrite)
    console.print(f"Initialized Manta config: [bold]{path}[/bold]")


if __name__ == "__main__":
    main_entry()
