"""Manta's Python SDK (ADR 0011, Decision 3).

A thin, typed façade over the same stores and runners the CLI uses — no new
daemons, no new state, so automation and the terminal share one source of
truth by construction:

    from manta_code import sdk

    result = sdk.run("summarize the failing tests", timeout=300)
    print(result.output, result.cost_usd)

    handle = sdk.submit("swe", "land the refactor and run the suite")
    handle.send("prefer the adapter approach we discussed")
    record = handle.wait(timeout=1800)
    print(handle.output())

Everything enforced in the CLI is enforced here: tasks run through the same
detached runner (agent permissions, model pins, budgets, audit events), and
`run()` goes through the same bounded headless path as ``manta run``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

from .agents.usage import AggRow, aggregate
from .tasks import executor, store

__all__ = [
    "RunResult",
    "TaskHandle",
    "agents",
    "agent_workspace",
    "agent",
    "agent_details",
    "create_agent",
    "clone_agent",
    "edit_agent",
    "enable_agent",
    "validate_agent",
    "cost",
    "run",
    "submit",
    "tasks",
]


@dataclass(frozen=True)
class RunResult:
    """Outcome of a synchronous one-shot :func:`run`."""

    output: str
    exit_code: int
    #: Real ledger cost/tokens recorded for this run (tagged, so concurrent
    #: Manta activity cannot pollute it). 0.0 when pricing is unknown.
    cost_usd: float
    tokens: int
    run_tag: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run(
    prompt: str,
    *,
    agent: str | None = None,
    timeout: int = 600,
    max_turns: int = 50,
    profile: str | None = None,
) -> RunResult:
    """Run one task synchronously through the enforced headless path.

    Equivalent to ``manta run`` (boot shim, build hook, agents, economy
    middleware all active). ``agent`` addresses a specific Manta agent (its
    pin, permissions, and budget apply). Cost comes back from the local
    ledger, attributed via a namespaced run tag passed in the subprocess
    environment — process-global ``os.environ`` is never mutated, so
    concurrent ``run()`` calls cannot cross-tag.
    """
    run_tag = f"sdk-{uuid.uuid4().hex[:8]}"
    started = time.time()
    argv = [
        sys.executable,
        "-m",
        "manta_code.main",
        "run",
        prompt,
        "--timeout",
        str(timeout),
        "--max-turns",
        str(max_turns),
    ]
    if profile:
        argv += ["-p", profile]
    if agent:
        argv += ["-a", agent]
    env = dict(os.environ)
    env["MANTA_TASK_ID"] = run_tag
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, check=False, env=env
    )
    output = completed.stdout.strip()
    if completed.returncode != 0 and not output:
        output = completed.stderr.strip()[-4000:]
    cost_usd, tokens = _ledger_for_tag(run_tag, since=started)
    return RunResult(
        output=output,
        exit_code=completed.returncode,
        cost_usd=cost_usd,
        tokens=tokens,
        run_tag=run_tag,
    )


def _ledger_for_tag(tag: str, *, since: float) -> tuple[float, int]:
    try:
        rows = [r for r in aggregate(by="task", since=since) if r.key == tag]
    except Exception:  # noqa: BLE001 - a missing ledger only loses cost detail
        return 0.0, 0
    return sum(r.cost_usd for r in rows), sum(r.total_tokens for r in rows)


class TaskHandle:
    """Handle on a background task: status, output, steering, cancellation."""

    def __init__(self, task_id: str) -> None:
        self.id = task_id

    def status(self) -> store.TaskRecord:
        """Current task record (stale runners reconciled, like the CLI)."""
        try:
            executor.reconcile_stale_tasks()
        except Exception:  # noqa: BLE001 - reconciliation is best-effort
            pass
        record = store.get_task(self.id)
        if record is None:
            raise executor.TaskError(f"no task '{self.id}'")
        return record

    def output(self) -> str:
        """Final result, or the log tail while the task is still running."""
        return executor.task_output(self.id)

    def send(self, message: str) -> None:
        """Steer the running task: delivered before its next model call."""
        executor.send_to_task(self.id, message)

    def cancel(self) -> store.TaskRecord:
        return executor.cancel_task(self.id)

    def review(self) -> dict:
        """Observed diff, files, final answer, tests, artifacts and spend coverage."""
        return executor.review_task(self.id)

    def integrate(self, *, expected_patch_sha256: str | None = None) -> store.TaskRecord:
        """Apply a previously reviewed stopped result; rejects stale diffs."""
        return executor.integrate_task(self.id, expected_patch_sha256=expected_patch_sha256)

    def discard(self) -> store.TaskRecord:
        """Discard the owned task checkout and retain its review receipts."""
        return executor.discard_task(self.id)

    def retry(self) -> store.TaskRecord:
        """Retry a stopped failed attempt in its existing isolated workspace."""
        return executor.retry_task(self.id)

    def resume(self) -> store.TaskRecord:
        """Explicitly approve a saved budget continuation."""
        return executor.resume_task(self.id)

    def respond(self, *, pending_id: str, decision: str, value: str = "") -> store.TaskRecord:
        """Decide only the exact pending group previously inspected by the user."""
        from .tasks.approvals import respond_task
        return respond_task(self.id, pending_id=pending_id, decision=decision, value=value)

    def dependencies(self, task_ids: list[str]) -> store.TaskRecord:
        """Set dependencies while queued; cycle and missing-task checks apply."""
        store.set_dependencies(self.id, task_ids)
        executor.schedule_tasks()
        return self.status()

    def wait(
        self, *, poll_seconds: float = 5.0, timeout: float | None = None
    ) -> store.TaskRecord:
        """Block until the task reaches a terminal state (or ``timeout``)."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            record = self.status()
            if record.state not in store.ACTIVE_STATES:
                return record
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError(
                    f"task {self.id} still {record.state} after {timeout}s"
                )
            time.sleep(max(0.2, poll_seconds))

    def __repr__(self) -> str:  # pragma: no cover - debug nicety
        return f"TaskHandle({self.id!r})"


def submit(
    agent: str,
    prompt: str,
    *,
    timeout: int = executor.DEFAULT_TASK_TIMEOUT,
    max_turns: int = executor.DEFAULT_TASK_MAX_TURNS,
    profile: str | None = None,
    allow_asks: bool = False,
    parent_task_id: str | None = None,
    parent_thread_id: str = "",
    dependencies: list[str] | None = None,
    isolation: str = "auto",
    max_concurrency: int = 3,
    context_mode: str = "isolated",
    conversation_context: str = "",
) -> TaskHandle:
    """Hand a long-running task to a named agent; returns immediately.

    ``allow_asks=True`` pre-approves the agent's ask-gated tools for this
    managed run; explicit approval rules still pause for a human. Writable
    work gets its own checkout. Forked task context needs an explicit visible
    ``conversation_context`` snapshot, separate from inline agent context.
    """
    record = executor.submit_task(
        agent,
        prompt,
        timeout=timeout,
        max_turns=max_turns,
        profile=profile,
        allow_asks=allow_asks,
        parent_task_id=parent_task_id, parent_thread_id=parent_thread_id,
        dependencies=dependencies, isolation=isolation, max_concurrency=max_concurrency,
        context_mode=context_mode, conversation_context=conversation_context,
    )
    return TaskHandle(record.id)


def tasks(state: str | None = None, *, limit: int = 50) -> list[store.TaskRecord]:
    """Recent background tasks, newest first (optionally filtered by state)."""
    try:
        executor.reconcile_stale_tasks()
    except Exception:  # noqa: BLE001
        pass
    return store.list_tasks(state=state, limit=limit)


def agent_workspace():
    """Shared authoring/resolution service used by CLI, TUI and runtime."""
    from .agents.workspace import AgentWorkspace
    return AgentWorkspace()


def agents(*, include_disabled: bool = False) -> list:
    """All addressable Manta agents (built-ins + user registry), as AgentDefs."""
    return agent_workspace().list(include_disabled=include_disabled)


def agent(name: str, *, include_disabled: bool = False):
    return agent_workspace().resolve(name, include_disabled=include_disabled)


def agent_details(name: str, *, inherited_model: str | None = None,
                  inherited_tools: list[str] | None = None) -> dict:
    return agent_workspace().effective(name, inherited_model=inherited_model, inherited_tools=inherited_tools)


def create_agent(definition):
    from .agents.registry import AgentDef
    return agent_workspace().create(AgentDef.model_validate(definition))


def clone_agent(name: str, new_name: str):
    return agent_workspace().clone(name, new_name)


def edit_agent(name: str, changes: dict, *, expected_revision: str | None = None):
    return agent_workspace().update(name, changes, expected_revision=expected_revision)


def enable_agent(name: str, enabled: bool = True):
    return agent_workspace().set_enabled(name, enabled)


def validate_agent(name: str) -> dict:
    """Offline policy compilation; does not verify live Gateway inference."""
    return agent_workspace().test(name)


def cost(by: str = "agent", *, since_days: int | None = None) -> list[AggRow]:
    """Spend aggregation from the local ledger (same data as ``manta cost``)."""
    since = None if since_days is None else time.time() - since_days * 86400
    return aggregate(by=by, since=since)
