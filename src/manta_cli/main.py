from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import init_project, load_config, project_manta_dir
from .context_broker import ContextBroker
from .pipeline import MantaPipeline
from .roles import default_roles
from .routing import HeuristicRouter
from .session import MantaSession

app = typer.Typer(help="Manta CLI — budget-aware multi-model developer agent")
console = Console()


@app.command()
def doctor() -> None:
    """Check local Manta setup."""
    console.print(f"Manta CLI {__version__}")
    console.print(f"Project: {Path.cwd()}")
    console.print(f"Manta dir: {project_manta_dir()}")
    console.print("Status: OK")


@app.command()
def init(overwrite: bool = typer.Option(False, help="Overwrite existing .manta/config.toml")) -> None:
    """Initialize .manta project config and state directories."""
    path = init_project(overwrite=overwrite)
    console.print(f"Initialized Manta config: [bold]{path}[/bold]")


@app.command()
def route(prompt: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """Route a task through the cheap heuristic router."""
    decision = HeuristicRouter().route(prompt)
    if json_output:
        console.print(decision.model_dump_json(indent=2))
        return
    table = Table(title="Manta Route Decision")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in decision.model_dump(mode="json").items():
        table.add_row(key, json.dumps(value) if isinstance(value, list) else str(value))
    console.print(table)


@app.command()
def run(
    prompt: str = typer.Argument(...),
    dry_run: bool = typer.Option(True, help="Use mock runtime; no model calls"),
    max_usd: Optional[float] = typer.Option(None, help="Maximum budget for this run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a task through the Manta pipeline."""
    if not dry_run:
        console.print("[yellow]Real model runtime is planned for Sprint 3. Running dry-run scaffold.[/yellow]")
    result = MantaPipeline().dry_run(prompt, max_usd=max_usd)
    if json_output:
        console.print(json.dumps(result, indent=2))
        return
    route_info = result["route"]
    console.print("[bold]Manta run[/bold]")
    console.print(f"Session: {result['session_id']}")
    console.print(f"Route: {route_info['route']}")
    console.print(f"Pipeline: {' → '.join(route_info['pipeline'])}")
    console.print(f"Budget: ${route_info['max_budget_usd']:.2f}")
    console.print(f"Reason: {route_info['reason']}")
    console.print("[green]Dry-run completed.[/green]")


@app.command()
def status() -> None:
    """Show latest session path."""
    path = MantaSession.last_session_path()
    if not path:
        console.print("No sessions found.")
        return
    console.print(f"Latest session: {path}")
    console.print(path.read_text(encoding="utf-8").splitlines()[-1])


@app.command()
def budget() -> None:
    """Show budget ledger path and rough usage."""
    ledger = project_manta_dir() / "ledger.jsonl"
    if not ledger.exists():
        console.print("No budget ledger yet.")
        return
    total = 0.0
    count = 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        total += float(item.get("estimated_cost_usd", 0))
        count += 1
    console.print(f"Ledger: {ledger}")
    console.print(f"Calls: {count}")
    console.print(f"Estimated cost: ${total:.4f}")


@app.command()
def roles() -> None:
    """List configured roles and model bindings."""
    cfg = load_config()
    table = Table(title="Manta Roles")
    table.add_column("Role")
    table.add_column("Model")
    table.add_column("Tools")
    for role in default_roles(cfg).values():
        table.add_row(role.name, role.model, ", ".join(role.tools) or "-")
    console.print(table)


@app.command()
def context(prompt: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """Show context manifest for a prompt."""
    decision = HeuristicRouter().route(prompt)
    manifest = ContextBroker().build_manifest("preview", decision, prompt)
    if json_output:
        console.print(manifest.model_dump_json(indent=2))
        return
    console.print(f"Selected files for route [bold]{decision.route}[/bold]:")
    for file in manifest.selected_files:
        console.print(f"- {file}")


if __name__ == "__main__":
    app()
