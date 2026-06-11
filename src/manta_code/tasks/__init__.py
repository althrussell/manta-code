"""Long-running agent tasks: store, executor, runner, events, tools (ADR 0010).

This package is the "hand it work and keep working" half of vision pillar 4 and
the data plane behind the chief of staff (pillar 5):

- :mod:`~manta_code.tasks.store` — a local SQLite store of tasks and
  lightweight events (``~/.manta/.state/tasks.db``), in the same local-only,
  never-raises style as the usage ledger.
- :mod:`~manta_code.tasks.executor` — submits a task by spawning a **detached
  runner subprocess** (no daemon: nothing to install or supervise, and the
  task survives the session that submitted it), and cancels by signalling the
  runner's process group.
- :mod:`~manta_code.tasks.runner` — the detached entry
  (``python -m manta_code.tasks.runner <task-id>``): marks the task running,
  drives the enforced headless path (``dcode.run_headless``) as the addressed
  agent, then records the outcome.
- :mod:`~manta_code.tasks.events` — middleware appending tool-call /
  approval / denial events, the feed behind ``manta status``.
- :mod:`~manta_code.tasks.tools` — LangChain tools (submit/status/output/list)
  given to the orchestrator and the ``chief`` agent so task management works
  in-session, not just from the CLI.
"""
