# ADR 0012 — Pilot readiness: memory that compounds, jobs debugging, felt economy

## Status

Accepted. Closes out the stickiness assessment of 2026-06-11 ("what is Manta
missing to be truly useful and sticky") so a pilot can start. Each item is
deliberately the smallest version that delivers the effect; deeper versions
are future work.

## Decisions

1. **Self-writing memory** — a `manta_remember` tool (orchestrator + inherited
   by agents) writes durable, redacted notes to the active agent's namespace;
   the base orchestrator now also *recalls* its own namespace, so bare
   sessions compound too. The orchestrator prompt nudges saving conventions,
   decisions, and gotchas. This is the compounding-personalization lever.
2. **Job-run debugging** — `get_run_diagnostics(run_id)`: per-task states,
   errors, error traces, and log tails from the Jobs API, so "why did my job
   fail" is answerable end-to-end inside a session. Read-only.
3. **Task completion notifications** — the runner fires a best-effort desktop
   notification (macOS `osascript` / Linux `notify-send`; `MANTA_NOTIFY=0`
   disables) at each task's terminal state. Tasks now report back.
4. **Felt economy** — `manta receipts [--days N]`: actual spend vs a
   clearly-labelled all-premium counterfactual baseline (the honest "what did
   cheap-by-default save me"), plus advisor activity. An in-TUI live spend
   line is future work (it requires a new upstream patch surface).
5. **Model-compat probing** — `manta doctor --probe` live-tests every pinned
   model (plus the default) in the real agent loop. Three pins in this repo's
   history failed only at runtime with cryptic errors; this converts those
   into a preflight FAIL with a repin hint. Opt-in (spends ~a cent per model).
6. **Distribution** — install/quickstart/feedback guidance in the README
   (initially shipped as PILOT.md, folded into the README on request) and a
   tagged `v0.1.0` release. PyPI publication is future work.
7. **CI recipe** — `examples/ci/manta-pr-review.yml`: the enforced, budgeted
   `@review` agent reviewing PRs headlessly and posting findings, the org
   habit loop. Shipped as an example, not enabled on this repo.

## Consequences

New: `agents/memory.py` write-path (`manta_remember`, `build_memory_tools`,
`orchestrator_memory_middleware`), `databricks_tools.get_run_diagnostics`,
`tasks/notify.py`, `usage.receipts` + `manta receipts`, `doctor --probe`,
README quickstart, CI example. Future work recorded: in-TUI spend line, PyPI release,
note-outcome capture for learned routing, deeper DAB authoring/deploy tools.
