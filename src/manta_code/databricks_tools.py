"""Databricks-native agent tools — Manta's reason to switch (ADR 0008, Phase 2).

These give the agent first-class access to *your* lakehouse, governed by *your*
Unity Catalog permissions (the Databricks SDK authenticates with the active
profile; UC enforces every grant — Manta adds no privilege):

- **Unity Catalog context** — browse catalogs/schemas/tables and read a table's
  columns/owner/comment, so the agent is grounded without pasted DDL.
- **SQL execution** — run *read-only* queries against a SQL warehouse and get a
  compact result summary. Writes are refused with a clear message (mutations go
  through a separately-gated tool, not the default set).
- **Lineage** — upstream/downstream tables via ``system.access.table_lineage``.
- **Jobs** — list/get jobs, trigger a run, and poll run status + result, so the
  agent can ship and *verify* on Databricks (closed loop).

Design for reliability + testability: the ``WorkspaceClient`` is built lazily
and cached on :class:`DatabricksTools`, so importing this module and *building*
the tool list never touches the network or requires auth — only *calling* a tool
does. Tool functions return human-readable strings (never raise) so a failure
becomes context the model can react to. A custom client can be injected, which
is how the unit tests exercise every tool without a workspace.
"""

from __future__ import annotations

import re
from typing import Any

#: SQL statements that only read. A query must start with one of these (after
#: stripping comments/whitespace) to run via :meth:`DatabricksTools.sql_query`.
_READ_ONLY_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")

#: Mutating keywords that are refused by the read-only SQL tool.
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|"
    r"replace|copy|write)\b",
    re.IGNORECASE,
)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def is_read_only_sql(query: str) -> bool:
    """Return ``True`` if ``query`` is a single read-only statement.

    Conservative by design: strips comments, requires a read-only leading
    keyword, and rejects anything containing a mutating keyword. False negatives
    (refusing an exotic-but-safe query) are preferred over running a write.
    """
    stripped = _COMMENT_RE.sub(" ", query).strip().rstrip(";").strip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].lower()
    if first not in _READ_ONLY_PREFIXES:
        return False
    # `WITH ... SELECT` is fine, but a CTE feeding an INSERT is not.
    if _WRITE_KEYWORDS.search(stripped):
        return False
    return True


class DatabricksTools:
    """Lazily-connected Databricks tool implementations.

    ``client_factory`` defaults to constructing a ``databricks.sdk.WorkspaceClient``
    from the ambient profile/env; tests pass a factory returning a fake. The
    client is built once on first use and cached.
    """

    def __init__(
        self,
        *,
        client_factory: Any | None = None,
        default_warehouse_id: str | None = None,
        max_rows: int = 100,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None
        self._default_warehouse_id = default_warehouse_id
        self._max_rows = max_rows

    # ---- connection -------------------------------------------------------

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    # ---- Unity Catalog ----------------------------------------------------

    def uc_list_catalogs(self) -> str:
        """List Unity Catalog catalogs visible to you."""
        try:
            catalogs = list(self.client.catalogs.list())
        except Exception as exc:  # noqa: BLE001
            return _error("list catalogs", exc)
        if not catalogs:
            return "No catalogs visible (check your Unity Catalog grants)."
        return "Catalogs:\n" + "\n".join(
            f"- {c.name}" + (f" — {c.comment}" if getattr(c, "comment", None) else "")
            for c in catalogs
        )

    def uc_list_schemas(self, catalog: str) -> str:
        """List schemas in a catalog."""
        try:
            schemas = list(self.client.schemas.list(catalog_name=catalog))
        except Exception as exc:  # noqa: BLE001
            return _error(f"list schemas in {catalog}", exc)
        if not schemas:
            return f"No schemas in catalog '{catalog}' (or no grants)."
        return f"Schemas in {catalog}:\n" + "\n".join(f"- {s.name}" for s in schemas)

    def uc_list_tables(self, catalog: str, schema_name: str) -> str:
        """List tables in a schema (``schema_name`` is the schema within ``catalog``)."""
        try:
            tables = list(self.client.tables.list(catalog_name=catalog, schema_name=schema_name))
        except Exception as exc:  # noqa: BLE001
            return _error(f"list tables in {catalog}.{schema_name}", exc)
        if not tables:
            return f"No tables in {catalog}.{schema_name} (or no grants)."
        return f"Tables in {catalog}.{schema_name}:\n" + "\n".join(
            f"- {t.name} ({getattr(t, 'table_type', '?')})" for t in tables
        )

    def uc_describe_table(self, full_name: str) -> str:
        """Describe a table: columns, types, owner, and comment.

        ``full_name`` is ``catalog.schema.table``.
        """
        try:
            table = self.client.tables.get(full_name=full_name)
        except Exception as exc:  # noqa: BLE001
            return _error(f"describe {full_name}", exc)
        lines = [f"Table: {full_name}"]
        if getattr(table, "table_type", None):
            lines.append(f"Type: {table.table_type}")
        if getattr(table, "owner", None):
            lines.append(f"Owner: {table.owner}")
        if getattr(table, "comment", None):
            lines.append(f"Comment: {table.comment}")
        columns = getattr(table, "columns", None) or []
        if columns:
            lines.append("Columns:")
            for col in columns:
                ctype = getattr(col, "type_text", None) or getattr(col, "type_name", "?")
                comment = f" — {col.comment}" if getattr(col, "comment", None) else ""
                lines.append(f"  - {col.name}: {ctype}{comment}")
        return "\n".join(lines)

    def uc_table_lineage(self, full_name: str, warehouse_id: str = "") -> str:
        """Show upstream and downstream tables for ``full_name`` via system tables.

        Requires a SQL warehouse and read access to ``system.access.table_lineage``.
        """
        query = (
            "SELECT source_table_full_name, target_table_full_name "
            "FROM system.access.table_lineage "
            f"WHERE source_table_full_name = '{full_name}' "
            f"OR target_table_full_name = '{full_name}' "
            "LIMIT 200"
        )
        return self.sql_query(query, warehouse_id=warehouse_id, row_limit=200)

    # ---- SQL --------------------------------------------------------------

    def _resolve_warehouse(self, warehouse_id: str) -> str | None:
        if warehouse_id:
            return warehouse_id
        if self._default_warehouse_id:
            return self._default_warehouse_id
        try:
            warehouses = list(self.client.warehouses.list())
        except Exception:  # noqa: BLE001
            return None
        # Prefer a running warehouse, else the first one.
        running = [w for w in warehouses if str(getattr(w, "state", "")).upper().endswith("RUNNING")]
        chosen = (running or warehouses or [None])[0]
        return getattr(chosen, "id", None) if chosen else None

    def sql_query(self, query: str, warehouse_id: str = "", row_limit: int = 100) -> str:
        """Run a READ-ONLY SQL query against a SQL warehouse and summarize rows.

        Writes (INSERT/UPDATE/DROP/...) are refused — describe the change instead.
        """
        if not is_read_only_sql(query):
            return (
                "Refused: this tool only runs read-only queries (SELECT/SHOW/"
                "DESCRIBE/EXPLAIN/WITH). The statement looks like it mutates "
                "data or schema. Describe the change or use an explicitly "
                "write-enabled, approval-gated path."
            )
        wh = self._resolve_warehouse(warehouse_id)
        if not wh:
            return "No SQL warehouse available (set one or check your grants)."
        limit = min(max(row_limit, 1), self._max_rows)
        try:
            resp = self.client.statement_execution.execute_statement(
                warehouse_id=wh,
                statement=query,
                wait_timeout="50s",
            )
        except Exception as exc:  # noqa: BLE001
            return _error("execute SQL", exc)
        return _format_statement_result(resp, limit)

    # ---- Jobs (ship + verify) --------------------------------------------

    def list_jobs(self, name_filter: str = "") -> str:
        """List jobs, optionally filtered by a substring of the job name."""
        try:
            jobs = list(self.client.jobs.list())
        except Exception as exc:  # noqa: BLE001
            return _error("list jobs", exc)
        rows = []
        for job in jobs:
            name = getattr(getattr(job, "settings", None), "name", None) or "(unnamed)"
            if name_filter and name_filter.lower() not in name.lower():
                continue
            rows.append(f"- {getattr(job, 'job_id', '?')}: {name}")
        if not rows:
            return "No matching jobs."
        return "Jobs:\n" + "\n".join(rows)

    def get_run_status(self, run_id: int) -> str:
        """Get a job run's lifecycle/result state and any result message."""
        try:
            run = self.client.jobs.get_run(run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            return _error(f"get run {run_id}", exc)
        state = getattr(run, "state", None)
        life = getattr(state, "life_cycle_state", "?")
        result = getattr(state, "result_state", None)
        message = getattr(state, "state_message", "") or ""
        url = getattr(run, "run_page_url", "")
        lines = [f"Run {run_id}: lifecycle={life} result={result or '-'}"]
        if message:
            lines.append(f"Message: {message}")
        if url:
            lines.append(f"URL: {url}")
        return "\n".join(lines)

    def get_run_diagnostics(self, run_id: int) -> str:
        """Diagnose a job run end-to-end: per-task states, errors, traces, log tails.

        The debugging workhorse (ADR 0012): for a failed run it pulls each
        failed task's error, error trace, and the tail of its logs so the
        agent can correlate with the code and propose a fix.
        """
        try:
            run = self.client.jobs.get_run(run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            return _error(f"get run {run_id}", exc)
        lines = [self.get_run_status(run_id)]
        tasks = list(getattr(run, "tasks", None) or [])
        if not tasks:
            tasks = [run]  # single-task / legacy runs: the run itself has output
        for task in tasks:
            task_key = getattr(task, "task_key", None) or "(run)"
            task_run_id = getattr(task, "run_id", None) or run_id
            state = getattr(task, "state", None)
            result = getattr(state, "result_state", None)
            lines.append(f"\nTask {task_key}: result={result or '-'} (run_id={task_run_id})")
            if result is not None and str(result) in ("RunResultState.SUCCESS", "SUCCESS"):
                continue
            try:
                output = self.client.jobs.get_run_output(run_id=task_run_id)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  (output unavailable: {exc})")
                continue
            error = getattr(output, "error", None)
            if error:
                lines.append(f"  Error: {error}")
            trace = getattr(output, "error_trace", None)
            if trace:
                lines.append("  Trace (tail):")
                lines.append("    " + "\n    ".join(str(trace).splitlines()[-15:]))
            logs = getattr(output, "logs", None)
            if logs:
                lines.append("  Logs (tail):")
                lines.append("    " + "\n    ".join(str(logs).splitlines()[-25:]))
        return "\n".join(lines)

    def run_job(self, job_id: int) -> str:
        """Trigger a job run (a mutation — gate behind approval). Returns the run id."""
        try:
            wait = self.client.jobs.run_now(job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            return _error(f"run job {job_id}", exc)
        run_id = getattr(wait, "run_id", None)
        if run_id is None and hasattr(wait, "response"):
            run_id = getattr(wait.response, "run_id", None)
        return (
            f"Triggered job {job_id}; run_id={run_id}. "
            f"Poll with get_run_status({run_id})."
        )

    # ---- tool list --------------------------------------------------------

    def as_tools(self, *, include_run_job: bool = True) -> list[Any]:
        """Wrap the implementations as LangChain ``StructuredTool``s.

        ``include_run_job`` controls whether the mutating ``run_job`` tool is
        exposed; the read-only tools are always included.
        """
        from langchain_core.tools import StructuredTool

        specs = [
            (self.uc_list_catalogs, "uc_list_catalogs"),
            (self.uc_list_schemas, "uc_list_schemas"),
            (self.uc_list_tables, "uc_list_tables"),
            (self.uc_describe_table, "uc_describe_table"),
            (self.uc_table_lineage, "uc_table_lineage"),
            (self.sql_query, "sql_query"),
            (self.list_jobs, "list_jobs"),
            (self.get_run_status, "get_run_status"),
            (self.get_run_diagnostics, "get_run_diagnostics"),
        ]
        if include_run_job:
            specs.append((self.run_job, "run_job"))
        return [
            StructuredTool.from_function(func, name=name, description=(func.__doc__ or "").strip())
            for func, name in specs
        ]


def _error(action: str, exc: Exception) -> str:
    return f"Could not {action}: {type(exc).__name__}: {exc}"


def _format_statement_result(resp: Any, limit: int) -> str:
    """Render a statement-execution response into a compact text table."""
    status = getattr(resp, "status", None)
    state = str(getattr(status, "state", "")).upper()
    if "FAILED" in state or "CANCELED" in state or "CLOSED" in state:
        err = getattr(status, "error", None)
        return f"Query {state}: {getattr(err, 'message', '') if err else ''}".strip()

    result = getattr(resp, "result", None)
    manifest = getattr(resp, "manifest", None)
    columns: list[str] = []
    if manifest is not None:
        schema = getattr(manifest, "schema", None)
        cols = getattr(schema, "columns", None) or []
        columns = [getattr(c, "name", f"col{i}") for i, c in enumerate(cols)]

    data = getattr(result, "data_array", None) if result is not None else None
    if not data:
        return "Query succeeded; 0 rows." + (f" Columns: {', '.join(columns)}" if columns else "")

    header = " | ".join(columns) if columns else ""
    body_rows = data[:limit]
    rendered = [" | ".join("" if v is None else str(v) for v in row) for row in body_rows]
    out = []
    if header:
        out.append(header)
        out.append("-" * len(header))
    out.extend(rendered)
    if len(data) > limit:
        out.append(f"... ({len(data)} rows total, showing {limit})")
    return "\n".join(out)


def build_default_databricks_tools() -> list[Any]:
    """Return the default Databricks tool set for the main agent.

    Read-only UC + SQL + lineage + job inspection, plus the approval-gated
    ``run_job`` (HITL handles confirmation by tool name). Returns ``[]`` when the
    Databricks SDK or LangChain tool support is unavailable, so the hook adds
    nothing rather than failing.
    """
    try:
        import databricks.sdk  # noqa: F401
        import langchain_core.tools  # noqa: F401
    except Exception:  # noqa: BLE001
        return []
    return DatabricksTools().as_tools(include_run_job=True)
