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
) -> TaskHandle:
    """Hand a long-running task to a named agent; returns immediately.

    ``allow_asks=True`` pre-approves the agent's ask-gated tools for this
    unattended run (audited as ``auto_approved``); the default fails closed.
    """
    record = executor.submit_task(
        agent,
        prompt,
        timeout=timeout,
        max_turns=max_turns,
        profile=profile,
        allow_asks=allow_asks,
    )
    return TaskHandle(record.id)


def tasks(state: str | None = None, *, limit: int = 50) -> list[store.TaskRecord]:
    """Recent background tasks, newest first (optionally filtered by state)."""
    try:
        executor.reconcile_stale_tasks()
    except Exception:  # noqa: BLE001
        pass
    return store.list_tasks(state=state, limit=limit)


def agents() -> list:
    """All addressable Manta agents (built-ins + user registry), as AgentDefs."""
    from .agents.defaults import merged_agents
    from .agents.registry import list_agents

    return merged_agents(list_agents())


def cost(by: str = "agent", *, since_days: int | None = None) -> list[AggRow]:
    """Spend aggregation from the local ledger (same data as ``manta cost``)."""
    since = None if since_days is None else time.time() - since_days * 86400
    return aggregate(by=by, since=since)
