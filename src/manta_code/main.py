from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    MantaConfig,
    init_project,
    interactive_endpoints,
    load_config,
    project_manta_dir,
)

app = typer.Typer(
    help="Manta Code — Databricks-preconfigured launcher for the deepagents-code coding agent",
)
console = Console()

#: Manta's own subcommands. Anything else (bare invocation, unknown flags) is
#: treated as a request to launch the interactive runtime and is forwarded.
KNOWN_SUBCOMMANDS = frozenset(
    {
        "doctor",
        "init",
        "agents",
        "cost",
        "budget",
        "run",
        "watch",
        "task",
        "status",
        "gateway",
        "receipts",
    }
)


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
    from .auth import databricks_configured

    cfg = load_config()
    configured = databricks_configured(profile)
    if not configured:
        console.print(
            "[dim]Databricks: not configured (optional) — launching with your "
            "other model providers. `databricks auth login` enables Databricks "
            "models and tools.[/dim]"
        )
    try:
        dcode.launch(
            profile=profile,
            default_endpoint=cfg.interactive.default_endpoint if configured else None,
            endpoints=_resolve_endpoints(cfg, profile) if configured else [],
            passthrough=passthrough,
        )
    except dcode.LauncherError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _resolve_endpoints(cfg: MantaConfig, profile: Optional[str]) -> list[str]:
    """Merge configured endpoints with chat endpoints discovered in the workspace.

    The configured ``default_endpoint`` stays first (it is what ``manta``
    launches with) and the explicit ``extra_endpoints`` follow, so a curated
    ordering is preserved even when discovery succeeds. Every chat-capable
    serving endpoint found in the workspace is then appended (deduped), so
    ``/model`` reflects the live workspace rather than a hardcoded pair.
    Discovery is best-effort: on any failure the configured endpoints are used
    unchanged.
    """
    from .auth import list_serving_chat_endpoints

    configured = interactive_endpoints(cfg)
    discovered = list_serving_chat_endpoints(profile)
    return list(dict.fromkeys([*configured, *discovered]))


@app.command()
def doctor(
    ctx: typer.Context,
    probe: bool = typer.Option(
        False,
        "--probe",
        help="Live-test each pinned model in the real agent loop (spends ~1 cent per model).",
    ),
) -> None:
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

    # Databricks is detect-and-enable (ADR 0010): absent entirely, it is
    # reported as optional rather than failing preflight, and the
    # Databricks-specific checks (model wiring, auth probe) are skipped.
    try:
        from .auth import databricks_configured

        db_configured = databricks_configured(profile)
    except Exception:  # noqa: BLE001
        db_configured = False

    # Provision + validate the Databricks provider in deepagents-code's config.
    # This is offline (no model call) and idempotent.
    if dcode_ok:
        try:
            from . import dcode
            from .auth import resolve_profile

            path = dcode.ensure_dcode_config(
                interactive_endpoints(cfg) if db_configured else [],
                default_endpoint=endpoint if db_configured else None,
            )
            dcode.mark_onboarding_complete()
            add("dcode config", True, str(path))
            if db_configured:
                try:
                    from deepagents_code.config import create_model

                    with _databricks_profile_env(resolve_profile(profile)):
                        result = create_model(f"databricks:{endpoint}")
                    add("model wiring", True, type(result.model).__name__)
                except Exception as exc:  # noqa: BLE001
                    add("model wiring", False, f"{endpoint}: {exc}")
        except Exception as exc:  # noqa: BLE001
            add("dcode config", False, str(exc))

    if not db_configured:
        add(
            "databricks",
            True,
            "not configured (optional) — `databricks auth login` to enable",
        )
    else:
        try:
            from .auth import current_username, is_authenticated, resolve_profile

            authed = is_authenticated(profile)
            who = current_username(profile) if authed else (resolve_profile(profile) or "default profile")
            add("databricks auth", authed, who or "")
        except Exception as exc:  # noqa: BLE001
            add("databricks auth", False, str(exc))

        # Validate every agent's Databricks model pin against the live
        # workspace. A pin to a nonexistent serving endpoint otherwise only
        # surfaces mid-run as a cryptic NotFoundError from the server.
        try:
            from .agents.defaults import merged_agents
            from .agents.registry import list_agents
            from .auth import list_serving_chat_endpoints

            live = set(list_serving_chat_endpoints(profile))
            if live:
                for defn in merged_agents(list_agents()):
                    pin = defn.model or ""
                    if not pin.startswith("databricks:"):
                        continue
                    endpoint_name = pin.split(":", 1)[1]
                    ok = endpoint_name in live
                    add(
                        f"agent pin: {defn.name}",
                        ok,
                        endpoint_name
                        if ok
                        else f"{endpoint_name} not found in workspace — "
                        f"fix with `manta agents edit {defn.name}`",
                    )
        except Exception:  # noqa: BLE001 - validation is best-effort
            pass

    # Control-plane hooks: Manta monkeypatches a few internal upstream symbols
    # (ADR 0008). If a pinned-version bump moved one, the build hook degrades to
    # a vanilla launch — surface that here so the loss of Manta agents/budget is
    # visible before the user wonders why their agents vanished.
    if dcode_ok:
        from .reliability import verify_patch_targets

        for result in verify_patch_targets():
            add(
                f"hook: {result.target.attribute}",
                result.ok,
                result.detail if not result.ok else result.target.purpose,
            )

    console.print(table)

    # Live model-compat probe (ADR 0012): an endpoint that exists can still
    # fail inside the agent loop (request-shape incompatibilities) — three
    # such pins were found only by live use. Opt-in because it spends tokens.
    if probe and dcode_ok and db_configured:
        from . import dcode
        from .agents.defaults import merged_agents
        from .agents.registry import list_agents

        endpoints = {endpoint}
        for defn in merged_agents(list_agents()):
            pin = defn.model or ""
            if pin.startswith("databricks:"):
                endpoints.add(pin.split(":", 1)[1])
        console.print(f"\nProbing {len(endpoints)} model(s) in the live agent loop…")
        for name in sorted(endpoints):
            code = dcode.run_headless(
                profile=profile,
                default_endpoint=None,
                endpoints=[],
                message="Reply with exactly: OK",
                passthrough=["-a", "agent", "-M", f"databricks:{name}"],
                timeout=120,
                max_turns=2,
                quiet=True,
            )
            ok = code == 0
            all_ok = all_ok and ok
            mark = "[green]pass[/green]" if ok else "[red]FAIL[/red]"
            console.print(f"  {mark}  {name}")
        console.print(
            "[dim]A FAIL means the endpoint rejects the agent loop's requests "
            "— repin with `manta agents set-model`.[/dim]"
        )

    console.print("[green]Status: OK[/green]" if all_ok else "[yellow]Status: issues found[/yellow]")


@app.command()
def init(overwrite: bool = typer.Option(False, help="Overwrite existing .manta/config.toml")) -> None:
    """Initialize .manta project config (launcher settings: endpoints)."""
    path = init_project(overwrite=overwrite)
    console.print(f"Initialized Manta config: [bold]{path}[/bold]")


agents_app = typer.Typer(
    help="Create, edit, and inspect Manta's real agents (enforced tools/permissions).",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(agents_app, name="agents")


def _enforcement_summary(defn: "object") -> str:
    """One-line human summary of an agent's enforced boundaries."""
    from .agents.registry import AgentDef

    assert isinstance(defn, AgentDef)
    bits: list[str] = []
    bits.append("read-only" if defn.read_only else "read-write")
    if defn.tools_allow is not None:
        bits.append(f"allow={','.join(defn.tools_allow) or 'none'}")
    if defn.tools_deny:
        bits.append(f"deny={','.join(defn.tools_deny)}")
    if defn.approval:
        bits.append(f"approve={','.join(defn.approval)}")
    if defn.databricks_tools:
        bits.append(f"db={','.join(defn.databricks_tools)}")
    return ", ".join(bits)


@agents_app.callback(invoke_without_command=True)
def agents_main(ctx: typer.Context) -> None:
    """List Manta's agents (built-in + your own) when run with no subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    _agents_list()


def _agents_list() -> None:
    from .agents.defaults import DEFAULT_AGENT_NAMES, merged_agents
    from .agents.registry import list_agents

    user = list_agents()
    user_names = {a.name for a in user}
    merged = merged_agents(user)

    table = Table(title="Manta agents")
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Source")
    table.add_column("Enforcement")
    table.add_column("Description")
    for defn in merged:
        if defn.name in user_names:
            source = "user (override)" if defn.name in DEFAULT_AGENT_NAMES else "user"
        else:
            source = "built-in"
        table.add_row(
            defn.name,
            defn.model or "[dim](inherits orchestrator)[/dim]",
            source,
            _enforcement_summary(defn),
            (defn.description or "").strip(),
        )
    console.print(table)
    console.print(
        "[dim]manta agents show <name> • create <name> • edit <name> • "
        "delete <name> • import • sync[/dim]"
    )
    console.print(
        "[dim]These agents are also selectable in-app via the /agents picker.[/dim]"
    )


@agents_app.command("sync")
def agents_sync() -> None:
    """Regenerate the in-app ``/agents`` profiles from the registry.

    Manta keeps a top-level deepagents profile for every agent (built-in +
    yours) so they appear in the in-app ``/agents`` picker. This runs
    automatically on launch; run it manually to refresh after editing agents.
    """
    from .agents.defaults import merged_agents
    from .agents.profiles import sync_agent_profiles
    from .agents.registry import list_agents

    result = sync_agent_profiles(merged_agents(list_agents()))
    console.print(
        f"Profiles: {len(result.written)} written, "
        f"{len(result.pruned)} pruned, {len(result.skipped)} skipped."
    )
    for label, names in (
        ("written", result.written),
        ("pruned", result.pruned),
        ("skipped (non-Manta)", result.skipped),
    ):
        if names:
            console.print(f"[dim]{label}: {', '.join(sorted(names))}[/dim]")


def _resolve_agent(name: str):
    """Return an AgentDef for ``name`` from the user registry or built-ins."""
    from .agents.defaults import DEFAULT_AGENTS
    from .agents.registry import agent_exists, load_agent

    if agent_exists(name):
        return load_agent(name), "user"
    for default in DEFAULT_AGENTS:
        if default.name == name:
            return default, "built-in"
    return None, None


@agents_app.command("show")
def agents_show(name: str = typer.Argument(..., help="Agent name")) -> None:
    """Show an agent's full definition and enforced boundaries."""
    defn, source = _resolve_agent(name)
    if defn is None:
        console.print(f"[red]No Manta agent named '{name}'.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{defn.name}[/bold] [dim]({source})[/dim]")
    console.print(f"Model:       {defn.model or '[dim](inherits orchestrator)[/dim]'}")
    console.print(f"Enforcement: {_enforcement_summary(defn)}")
    if defn.budget_max_tokens or defn.budget_max_usd:
        console.print(
            f"Budget:      {defn.budget_max_tokens or '∞'} tokens / "
            f"${defn.budget_max_usd if defn.budget_max_usd is not None else '∞'}"
        )
    console.print(f"Memory:      {'on' if defn.memory else 'off'} "
                  f"(namespace: {defn.effective_namespace()})")
    console.print("\n[bold]System prompt[/bold]")
    console.print(defn.system_prompt or "[dim](none)[/dim]")


@agents_app.command("create")
def agents_create(
    name: str = typer.Argument(..., help="New agent name (slug: lowercase, dashes)"),
    describe: Optional[str] = typer.Option(
        None, "--describe", help="Describe the agent in plain English; Manta drafts it."
    ),
    description: Optional[str] = typer.Option(None, help="One-line description."),
    model: Optional[str] = typer.Option(None, help="Model pin, e.g. databricks:<endpoint>."),
    prompt: Optional[str] = typer.Option(None, help="System prompt (overrides drafted)."),
    read_only: bool = typer.Option(False, "--read-only", help="Enforce read-only."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing agent."),
) -> None:
    """Create a new agent, optionally drafting it from a plain-English description."""
    from .agents.authoring import draft_agent_from_description
    from .agents.registry import AgentDef, agent_exists, is_valid_name, save_agent

    if not is_valid_name(name):
        console.print(
            f"[red]Invalid name '{name}'.[/red] Use lowercase letters, digits, and dashes."
        )
        raise typer.Exit(code=1)
    if agent_exists(name) and not force:
        console.print(
            f"[red]Agent '{name}' already exists.[/red] Use --force to overwrite "
            "or 'manta agents edit'."
        )
        raise typer.Exit(code=1)

    if describe:
        defn = draft_agent_from_description(name, describe)
    else:
        defn = AgentDef(name=name, description=description or "", read_only=read_only)
    # Explicit options always win over drafted values.
    if description is not None:
        defn.description = description
    if model is not None:
        defn.model = model
    if prompt is not None:
        defn.system_prompt = prompt
    if read_only:
        defn.read_only = True

    directory = save_agent(defn)
    console.print(f"Created agent [bold]{name}[/bold] at [dim]{directory}[/dim]")
    console.print(f"Enforcement: {_enforcement_summary(defn)}")
    if describe:
        console.print(
            "[dim]Drafted from your description — review with 'manta agents show "
            f"{name}' and tune with 'manta agents edit {name}'.[/dim]"
        )


@agents_app.command("edit")
def agents_edit(name: str = typer.Argument(..., help="Agent name")) -> None:
    """Open an agent's files in $EDITOR (materializing a built-in first if needed)."""
    import shutil
    import subprocess

    from .agents.registry import agent_exists, agent_md_path, agent_toml_path, save_agent

    if not agent_exists(name):
        defn, source = _resolve_agent(name)
        if defn is None:
            console.print(f"[red]No Manta agent named '{name}'.[/red]")
            raise typer.Exit(code=1)
        # Materialize a built-in into the user registry so edits persist.
        save_agent(defn)
        console.print(f"[dim]Copied built-in '{name}' into your registry for editing.[/dim]")

    toml_path = agent_toml_path(name)
    md_path = agent_md_path(name)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor or shutil.which(editor.split()[0]) is None:
        console.print(
            "No usable $EDITOR set. Edit these files directly:\n"
            f"  {toml_path}\n  {md_path}"
        )
        return
    subprocess.run([*editor.split(), str(toml_path), str(md_path)], check=False)  # noqa: S603
    console.print(f"[green]Saved edits to '{name}'.[/green]")


@agents_app.command("set-model")
def agents_set_model(
    name: str = typer.Argument(..., help="Agent name."),
    model: str = typer.Argument(
        ..., help="Model ref (databricks:<endpoint>, anthropic:<model>, or bare endpoint)."
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Check databricks: refs against the live workspace's endpoints.",
    ),
) -> None:
    """Switch an agent's pinned model — takes effect on its next call.

    The pin resolves through the provider registry, so any registered provider
    (and anything langchain resolves natively) is valid. A built-in agent is
    materialized into your registry first so the change sticks.
    """
    from .agents.registry import agent_exists, save_agent
    from .providers import parse_model_ref

    defn, _source = _resolve_agent(name)
    if defn is None:
        console.print(f"[red]No Manta agent named '{name}'.[/red]")
        raise typer.Exit(code=1)

    ref = parse_model_ref(model)
    if ref is None:
        # Bare endpoint name: default to the Databricks provider.
        model = f"databricks:{model}"
        ref = parse_model_ref(model)

    if verify and ref is not None and ref.provider == "databricks":
        from .auth import databricks_configured, list_serving_chat_endpoints

        if databricks_configured():
            live = list_serving_chat_endpoints()
            if live and ref.model not in live:
                near = [e for e in live if ref.model.split("-")[-1] in e][:5]
                console.print(
                    f"[red]Endpoint '{ref.model}' not found in this workspace.[/red]"
                )
                if near:
                    console.print(f"[dim]Close matches: {', '.join(near)}[/dim]")
                console.print("[dim]Override with --no-verify if intentional.[/dim]")
                raise typer.Exit(code=1)

    if not agent_exists(name):
        console.print(f"[dim]Copied built-in '{name}' into your registry.[/dim]")
    defn.model = model
    save_agent(defn)
    try:
        from .agents.defaults import merged_agents
        from .agents.profiles import sync_agent_profiles
        from .agents.registry import list_agents

        sync_agent_profiles(merged_agents(list_agents()))
    except Exception:  # noqa: BLE001 - profile sync is best-effort
        pass
    console.print(
        f"Pinned [bold]{name}[/bold] to [bold]{model}[/bold] — applies from the "
        "next session (or /restart in a running one)."
    )


@agents_app.command("delete")
def agents_delete(
    name: str = typer.Argument(..., help="Agent name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a user agent from the registry."""
    from .agents.registry import agent_exists, delete_agent

    if not agent_exists(name):
        console.print(
            f"[yellow]No user agent '{name}' to delete.[/yellow] "
            "(Built-ins live in code and cannot be deleted; override them instead.)"
        )
        raise typer.Exit(code=1)
    if not yes:
        confirmed = typer.confirm(f"Delete agent '{name}'?")
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit(code=1)
    delete_agent(name)
    console.print(f"Deleted agent [bold]{name}[/bold].")


@agents_app.command("memory")
def agents_memory(
    name: str = typer.Argument(..., help="Agent name"),
    show: bool = typer.Option(True, "--show/--no-show", help="Print remembered notes."),
    add: Optional[str] = typer.Option(None, "--add", help="Add a note (redacted on write)."),
    clear: bool = typer.Option(False, "--clear", help="Delete all of this agent's memory."),
) -> None:
    """View, add to, or clear an agent's durable private memory."""
    defn, _ = _resolve_agent(name)
    if defn is None:
        console.print(f"[red]No Manta agent named '{name}'.[/red]")
        raise typer.Exit(code=1)
    try:
        from .agents import memory as mem
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Memory unavailable: {exc}[/red] (install the [agent] extra)")
        raise typer.Exit(code=1) from exc

    store = mem.shared_memory_store()
    if store is None:
        console.print("[red]Could not open the memory store.[/red]")
        raise typer.Exit(code=1)
    namespace = mem.memory_namespace(defn)

    if clear:
        removed = mem.clear_memories(store, namespace)
        console.print(f"Cleared {removed} memory item(s) for [bold]{name}[/bold].")
        return
    if add:
        import time

        mem.write_memory(store, namespace, key=f"note-{int(time.time()*1000)}", text=add)
        console.print(f"Added a note to [bold]{name}[/bold]'s memory (secrets redacted on write).")
    if show:
        notes = mem.read_memories(store, namespace)
        if not notes:
            console.print(f"[dim]No memories for '{name}' yet.[/dim]")
            return
        console.print(f"[bold]{name}[/bold] remembers:")
        for note in notes:
            console.print(f"  - {note}")


@agents_app.command("import")
def agents_import(
    path: Optional[str] = typer.Option(None, "--from", help="Project root (default: cwd)."),
    name: str = typer.Option("imported", help="Name for the imported agent."),
) -> None:
    """Import CLAUDE.md / .cursor/rules / .mcp.json into Manta (cut switching cost)."""
    from .agents.importer import import_sources
    from .dcode import DEEPAGENTS_CONFIG_DIR

    root = Path(path) if path else Path.cwd()
    report = import_sources(root, dest_config_dir=DEEPAGENTS_CONFIG_DIR, agent_name=name)
    if not report.imported_anything:
        console.print("[yellow]Nothing to import.[/yellow]")
        for note in report.notes:
            console.print(f"[dim]{note}[/dim]")
        return
    if report.claude_md:
        console.print(f"Imported project memory: [dim]{report.claude_md}[/dim]")
    if report.cursor_rules:
        console.print(f"Imported {len(report.cursor_rules)} cursor rule file(s).")
    if report.agent_created:
        console.print(f"Created agent [bold]{report.agent_created}[/bold] from project rules.")
    if report.mcp_servers:
        console.print(f"Merged MCP servers: {', '.join(report.mcp_servers)}")
    for note in report.notes:
        console.print(f"[dim]{note}[/dim]")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cost(usd: float, known: bool) -> str:
    return f"${usd:.4f}" if known else f"~${usd:.4f}*"


def _since_timestamp(days: Optional[int]) -> Optional[float]:
    if days is None:
        return None
    import time

    return time.time() - days * 86400


@app.command()
def cost(
    by: str = typer.Option("agent", "--by", help="Group by: agent | model | task."),
    agent: Optional[str] = typer.Option(None, help="Drill into a single agent."),
    days: Optional[int] = typer.Option(None, "--days", help="Only the last N days."),
    breakdown: bool = typer.Option(
        False, "--breakdown", help="Show scaffolding (skills/defaults) vs net-new tokens."
    ),
    advise: bool = typer.Option(
        False, "--advise", help="Spend-optimization recommendations from the ledger."
    ),
) -> None:
    """Historical token + dollar spend from the local usage ledger.

    Answers "which agents/models/tasks cost the most" with a cache-hit rate per
    group. ``*`` marks rows whose dollar cost is partly estimated (an endpoint
    with unknown pricing). All data is local — nothing leaves the machine.
    """
    from .agents import usage

    since = _since_timestamp(days)
    try:
        rows = usage.aggregate(by=by, since=since, agent=agent)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not rows:
        console.print("[dim]No usage recorded yet. Run an agent, then check back.[/dim]")
        return

    table = Table(title=f"Manta cost by {by}" + (f" — {agent}" if agent else ""))
    table.add_column(by.capitalize(), style="bold")
    table.add_column("Calls", justify="right")
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Cache hit", justify="right")
    table.add_column("Cost (USD)", justify="right")

    grand = 0.0
    grand_known = True
    for r in rows:
        hit = r.cache_hit_rate
        hit_str = f"{hit * 100:.0f}%" if hit is not None else "[dim]n/a[/dim]"
        table.add_row(
            r.key,
            str(r.calls),
            _fmt_tokens(r.input_tokens),
            _fmt_tokens(r.output_tokens),
            hit_str,
            _fmt_cost(r.cost_usd, r.cost_known),
        )
        grand += r.cost_usd
        grand_known = grand_known and r.cost_known
    console.print(table)
    console.print(f"Total: [bold]{_fmt_cost(grand, grand_known)}[/bold]")
    if not grand_known:
        console.print("[dim]* cost partly estimated (endpoint pricing unknown).[/dim]")

    if breakdown:
        sb = usage.scaffold_breakdown(since=since, agent=agent)
        if sb.total == 0:
            console.print("[dim]No scaffolding/net-new estimate available.[/dim]")
        else:
            ratio = sb.overhead_ratio
            ratio_str = f"{ratio:.2f}:1" if ratio is not None else "n/a"
            console.print(
                f"\n[bold]Token breakdown (estimated):[/bold] "
                f"scaffolding {_fmt_tokens(sb.scaffold_tokens)} "
                f"vs net-new {_fmt_tokens(sb.net_new_tokens)} "
                f"(overhead:net-new {ratio_str})"
            )
            console.print(
                "[dim]Scaffolding = system prompt + tool/skill/memory schemas paid "
                "before any task work. Prune expensive defaults to lower it.[/dim]"
            )

    if advise:
        recommendations = usage.advise(since=since)
        recent = usage.recent_advice(since=since, limit=10)
        if not recommendations and not recent:
            console.print(
                "\n[green]No spend-optimization advice — the ledger looks healthy.[/green]"
            )
        if recommendations:
            console.print("\n[bold]Spend advice (from the ledger):[/bold]")
            for rec in recommendations:
                console.print(f"  • {rec}")
        if recent:
            console.print("\n[bold]Recent in-session advice:[/bold]")
            for r in recent:
                marker = "⏸" if r.severity == "interrupt" else "•"
                console.print(f"  {marker} [{r.agent}] {r.kind}: {r.message[:110]}")


@app.command()
def budget(
    days: Optional[int] = typer.Option(1, "--days", help="Window in days (default: today)."),
) -> None:
    """Live-ish spend for the recent window (per-agent tokens + dollars).

    A lightweight read over the local ledger; for full history use ``manta cost``.
    """
    from .agents import usage

    since = _since_timestamp(days)
    rows = usage.aggregate(by="agent", since=since)
    if not rows:
        console.print("[dim]No usage in this window.[/dim]")
        return
    total = usage.totals(since=since)
    table = Table(title=f"Manta budget — last {days} day(s)")
    table.add_column("Agent", style="bold")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    for r in rows:
        table.add_row(r.key, _fmt_tokens(r.total_tokens), _fmt_cost(r.cost_usd, r.cost_known))
    console.print(table)
    console.print(
        f"Total: [bold]{_fmt_tokens(total.total_tokens)} tokens / "
        f"{_fmt_cost(total.cost_usd, total.cost_known)}[/bold]"
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="The task to run, in plain English."),
    profile: Optional[str] = typer.Option(None, "-p", "--profile", help="Databricks profile."),
    timeout: int = typer.Option(
        600, "--timeout", help="Hard wall-clock timeout in seconds (bounds startup hangs)."
    ),
    max_turns: int = typer.Option(50, "--max-turns", help="Max agentic turns before stopping."),
    json_output: Optional[str] = typer.Option(
        None, "--json", help="Emit machine-readable output: text | stream-json."
    ),
    stream: bool = typer.Option(False, "--stream/--no-stream", help="Stream tokens (default off)."),
    allow_shell: Optional[str] = typer.Option(
        None,
        "--allow-shell",
        help="Comma-separated shell allow-list to enable the shell in headless mode.",
    ),
) -> None:
    """Run a single task headlessly and exit — for scripts, CI, and SDK use.

    Wraps deepagents-code's non-interactive mode with CI-safe defaults: a bounded
    timeout, a turn cap, and buffered output. Manta's control plane (enforced
    agents, token economy/ledger) applies here too. Extra args after the task are
    forwarded to the runtime verbatim.
    """
    from importlib.util import find_spec

    if find_spec("deepagents_code") is None:
        console.print(
            "[red]The runtime (deepagents-code) is not installed.[/red]\n"
            "Install it with: [bold]pip install -e '.[agent]'[/bold]"
        )
        raise typer.Exit(code=1)

    from . import dcode
    from .auth import databricks_configured

    cfg = load_config()
    db_configured = databricks_configured(profile)
    try:
        code = dcode.run_headless(
            profile=profile,
            default_endpoint=cfg.interactive.default_endpoint if db_configured else None,
            endpoints=_resolve_endpoints(cfg, profile) if db_configured else [],
            message=task,
            passthrough=list(ctx.args),
            timeout=timeout,
            max_turns=max_turns,
            no_stream=not stream,
            json_output=json_output,
            shell_allow_list=allow_shell,
        )
    except dcode.LauncherError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=code)


@app.command()
def gateway(
    ctx: typer.Context,
    limit: int = typer.Option(100, "--limit", help="Max endpoints to inspect."),
) -> None:
    """Show the AI Gateway surface: brokered providers and governance per endpoint.

    Inspects every chat serving endpoint for its gateway posture — usage
    tracking, rate limits, guardrails, fallbacks — and which underlying vendor
    serves it (Databricks-hosted or an external provider brokered through the
    gateway). One live ``get`` per endpoint, so this is an on-demand view.
    """
    profile = (ctx.obj or {}).get("profile")
    from .auth import databricks_configured
    from .providers.gateway import discover_gateway_surface

    if not databricks_configured(profile):
        console.print(
            "[yellow]Databricks is not configured — no gateway surface to "
            "inspect.[/yellow] `databricks auth login` to enable."
        )
        raise typer.Exit(code=1)

    with console.status("Inspecting serving endpoints…"):
        surface = discover_gateway_surface(profile, limit=limit)
    if not surface.endpoints:
        console.print("[yellow]No chat endpoints found (or not authenticated).[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title="AI Gateway surface (chat endpoints)")
    table.add_column("Endpoint", style="bold")
    table.add_column("Source")
    table.add_column("Gateway governance")
    for info in surface.endpoints:
        feats = ", ".join(info.features) if info.features else "[dim]none[/dim]"
        table.add_row(info.name, info.source, feats)
    console.print(table)
    console.print(
        f"Providers brokered: [bold]{', '.join(surface.providers)}[/bold] • "
        f"{len(surface.governed)}/{len(surface.endpoints)} endpoints "
        "gateway-governed"
    )


task_app = typer.Typer(
    help="Background agent tasks: submit, watch, collect, cancel (ADR 0010).",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(task_app, name="task")


def _fmt_age(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    import time

    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


_STATE_STYLES = {
    "queued": "yellow",
    "running": "cyan",
    "done": "green",
    "failed": "red",
    "cancelled": "dim",
}


def _task_table(tasks: list) -> Table:
    table = Table(title="Manta background tasks")
    table.add_column("Id", style="bold")
    table.add_column("Agent")
    table.add_column("State")
    table.add_column("Age", justify="right")
    table.add_column("Prompt")
    for t in tasks:
        style = _STATE_STYLES.get(t.state, "")
        state = f"[{style}]{t.state}[/{style}]" if style else t.state
        table.add_row(t.id, f"@{t.agent}", state, _fmt_age(t.created_at), t.prompt[:70])
    return table


@task_app.callback(invoke_without_command=True)
def task_main(ctx: typer.Context) -> None:
    """List background tasks when run with no subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    _task_list(state=None)


def _task_list(state: Optional[str]) -> None:
    from .tasks.executor import reconcile_stale_tasks
    from .tasks.store import list_tasks

    reconcile_stale_tasks()
    tasks = list_tasks(state=state, limit=30)
    if not tasks:
        console.print(
            "[dim]No background tasks yet. Submit one with "
            "`manta task submit <agent> \"...\"` or `@<agent> ... &` in-session.[/dim]"
        )
        return
    console.print(_task_table(tasks))
    console.print("[dim]manta task status <id> • output <id> • cancel <id>[/dim]")


@task_app.command("submit")
def task_submit(
    agent: str = typer.Argument(..., help="Agent to run the task (e.g. swe)."),
    prompt: str = typer.Argument(..., help="The task, in plain English."),
    timeout: int = typer.Option(
        None, "--timeout", help="Wall-clock cap in seconds (default 1800)."
    ),
    max_turns: int = typer.Option(
        None, "--max-turns", help="Agentic turn cap (default 80)."
    ),
    profile: Optional[str] = typer.Option(None, "-p", "--profile", help="Databricks profile."),
    allow_asks: bool = typer.Option(
        False,
        "--allow-asks",
        help="Pre-approve this agent's ask-gated tools for this unattended run.",
    ),
) -> None:
    """Hand a long-running task to a named agent; returns immediately with an id."""
    from .tasks import executor

    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    try:
        record = executor.submit_task(
            agent, prompt, profile=profile, allow_asks=allow_asks, **kwargs
        )
    except executor.TaskError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Submitted task [bold]{record.id}[/bold] to [bold]@{record.agent}[/bold] "
        f"(pid {record.pid})."
    )
    console.print(
        f"[dim]manta task status {record.id} • manta task output {record.id} • "
        f"manta task cancel {record.id}[/dim]"
    )


@task_app.command("list")
def task_list_cmd(
    state: Optional[str] = typer.Option(None, "--state", help="Filter by state."),
) -> None:
    """List recent background tasks."""
    _task_list(state=state)


@task_app.command("status")
def task_status(task_id: str = typer.Argument(..., help="Task id.")) -> None:
    """Show one task's state, timing, and exit code."""
    from .tasks.executor import reconcile_stale_tasks
    from .tasks.store import get_task

    reconcile_stale_tasks()
    record = get_task(task_id)
    if record is None:
        console.print(f"[red]No task '{task_id}'.[/red]")
        raise typer.Exit(code=1)
    style = _STATE_STYLES.get(record.state, "")
    state = f"[{style}]{record.state}[/{style}]" if style else record.state
    console.print(f"Task [bold]{record.id}[/bold] — @{record.agent} — {state}")
    console.print(f"Submitted: {_fmt_age(record.created_at)} ago")
    if record.started_at:
        console.print(f"Started:   {_fmt_age(record.started_at)} ago")
    if record.finished_at:
        console.print(f"Finished:  {_fmt_age(record.finished_at)} ago "
                      f"(exit {record.exit_code})")
    console.print(f"Prompt:    {record.prompt}")
    try:
        from .tasks.store import inbox_count

        steered = inbox_count(task_id)
        if steered:
            console.print(f"Steered:   {steered} message(s)")
    except Exception:  # noqa: BLE001
        pass
    if record.allow_asks:
        console.print("Asks:      pre-approved at submission (--allow-asks)")
    if record.log_path:
        console.print(f"Log:       [dim]{record.log_path}[/dim]")


@task_app.command("output")
def task_output_cmd(task_id: str = typer.Argument(..., help="Task id.")) -> None:
    """Print a task's result (or its log tail while it is still running)."""
    from .tasks import executor

    try:
        output = executor.task_output(task_id)
    except executor.TaskError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not output:
        console.print("[dim](no output yet)[/dim]")
        return
    console.print(output)


@task_app.command("send")
def task_send(
    task_id: str = typer.Argument(..., help="Task id."),
    message: str = typer.Argument(..., help="Steering message for the running task."),
) -> None:
    """Steer a queued/running task: the message is delivered into the task's
    thread before its next model call (ADR 0011)."""
    from .tasks import executor

    try:
        executor.send_to_task(task_id, message)
    except executor.TaskError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Steering note queued for task [bold]{task_id}[/bold] — delivered "
        "before its next model call."
    )


@task_app.command("cancel")
def task_cancel(task_id: str = typer.Argument(..., help="Task id.")) -> None:
    """Cancel a queued or running background task."""
    from .tasks import executor

    try:
        record = executor.cancel_task(task_id)
    except executor.TaskError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"Cancelled task [bold]{record.id}[/bold] (@{record.agent}).")


@app.command()
def receipts(
    days: int = typer.Option(7, "--days", help="Window in days."),
) -> None:
    """Your spend story: actual cost, estimated savings, and advisor activity.

    The savings figure is a clearly-labelled counterfactual — the window's
    token volumes priced as if every call had run on the premium model.
    """
    from .agents import usage

    r = usage.receipts(days=days)
    if r.calls == 0:
        console.print(f"[dim]No usage in the last {days} day(s).[/dim]")
        return
    console.print(f"[bold]Manta receipts — last {r.days} day(s)[/bold]")
    console.print(f"  Calls:            {r.calls}")
    console.print(f"  Tokens:           {_fmt_tokens(r.total_tokens)}")
    console.print(f"  Spend:            ${r.actual_usd:.2f}")
    console.print(
        f"  Premium baseline: ${r.premium_baseline_usd:.2f} "
        "[dim](same tokens, all-opus — counterfactual)[/dim]"
    )
    console.print(
        f"  Est. savings:     [green]${r.estimated_savings_usd:.2f}[/green]"
    )
    if r.advice_delivered:
        console.print(
            f"  Advisor:          {r.advice_delivered} recommendation(s), "
            f"{r.advice_accepted} decided via pause"
        )
    console.print(
        "[dim]Drill down: manta cost --by model • manta cost --advise[/dim]"
    )


@app.command()
def status(
    days: Optional[int] = typer.Option(1, "--days", help="Spend window in days."),
    events: int = typer.Option(12, "--events", help="Recent events to show."),
) -> None:
    """The chief-of-staff pane: tasks, recent activity, and spend in one view."""
    from .agents import usage
    from .tasks.executor import reconcile_stale_tasks
    from .tasks.store import list_tasks, recent_events

    reconcile_stale_tasks()
    active = [t for t in list_tasks(limit=50) if t.state in ("queued", "running")]
    finished = [t for t in list_tasks(limit=8) if t.state not in ("queued", "running")]
    shown = [*active, *finished][:12]
    if shown:
        console.print(_task_table(shown))
    else:
        console.print("[dim]No background tasks.[/dim]")

    recent = recent_events(limit=events)
    if recent:
        table = Table(title="Recent activity")
        table.add_column("When", justify="right")
        table.add_column("Agent")
        table.add_column("Event")
        table.add_column("Detail")
        for e in recent:
            kind_style = {"denied": "red", "approved": "yellow"}.get(e.kind, "")
            kind = f"[{kind_style}]{e.kind}[/{kind_style}]" if kind_style else e.kind
            table.add_row(
                f"{_fmt_age(e.ts)} ago",
                e.agent + (f" [dim]task {e.task_id}[/dim]" if e.task_id else ""),
                kind,
                e.detail[:60],
            )
        console.print(table)

    since = _since_timestamp(days)
    rows = usage.aggregate(by="agent", since=since)
    if rows:
        total = usage.totals(since=since)
        table = Table(title=f"Spend — last {days} day(s)")
        table.add_column("Agent", style="bold")
        table.add_column("Calls", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Cost (USD)", justify="right")
        for r in rows:
            table.add_row(
                r.key, str(r.calls), _fmt_tokens(r.total_tokens),
                _fmt_cost(r.cost_usd, r.cost_known),
            )
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]", str(total.calls),
            _fmt_tokens(total.total_tokens), _fmt_cost(total.cost_usd, total.cost_known),
        )
        console.print(table)


@app.command()
def watch(
    interval: float = typer.Option(2.0, "--interval", help="Refresh seconds."),
    days: Optional[int] = typer.Option(1, "--days", help="Window to display."),
) -> None:
    """Live per-agent cost/activity view — steering leverage for parallel agents.

    Tails the local usage ledger and refreshes a per-agent token/cost table so
    you can watch where spend is going while agents (including subagents) run in
    another terminal. Ctrl-C to stop.
    """
    import time

    from rich.live import Live

    from .agents import usage

    def _table() -> Table:
        since = _since_timestamp(days)
        rows = usage.aggregate(by="agent", since=since)
        total = usage.totals(since=since)
        table = Table(title=f"Manta live spend — last {days} day(s)  (Ctrl-C to stop)")
        table.add_column("Agent", style="bold")
        table.add_column("Calls", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Cost (USD)", justify="right")
        for r in rows:
            table.add_row(
                r.key, str(r.calls), _fmt_tokens(r.total_tokens),
                _fmt_cost(r.cost_usd, r.cost_known),
            )
        if not rows:
            table.add_row("[dim]waiting for activity…[/dim]", "", "", "")
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]", str(total.calls), _fmt_tokens(total.total_tokens),
            _fmt_cost(total.cost_usd, total.cost_known),
        )
        return table

    try:
        with Live(_table(), console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(max(0.2, interval))
                live.update(_table())
    except KeyboardInterrupt:
        console.print("[dim]stopped.[/dim]")


if __name__ == "__main__":
    main_entry()
