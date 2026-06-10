from __future__ import annotations

from types import SimpleNamespace

import pytest

from manta_code.databricks_tools import DatabricksTools, is_read_only_sql


def test_is_read_only_sql():
    assert is_read_only_sql("SELECT * FROM t")
    assert is_read_only_sql("  with cte as (select 1) select * from cte ")
    assert is_read_only_sql("SHOW TABLES")
    assert is_read_only_sql("DESCRIBE t")
    assert not is_read_only_sql("INSERT INTO t VALUES (1)")
    assert not is_read_only_sql("DROP TABLE t")
    assert not is_read_only_sql("with x as (select 1) insert into t select * from x")
    assert not is_read_only_sql("")
    # Comment-stripping doesn't smuggle writes past the check.
    assert not is_read_only_sql("-- ok\nDELETE FROM t")


class _FakeClient:
    def __init__(self) -> None:
        self.catalogs = SimpleNamespace(
            list=lambda: [SimpleNamespace(name="main", comment="primary")]
        )
        self.schemas = SimpleNamespace(
            list=lambda catalog_name: [SimpleNamespace(name="sales")]
        )
        self.tables = SimpleNamespace(
            list=lambda catalog_name, schema_name: [
                SimpleNamespace(name="orders", table_type="MANAGED")
            ],
            get=lambda full_name: SimpleNamespace(
                table_type="MANAGED",
                owner="me@example.com",
                comment="order facts",
                columns=[
                    SimpleNamespace(name="id", type_text="bigint", comment="pk"),
                    SimpleNamespace(name="amount", type_text="double", comment=None),
                ],
            ),
        )
        self.warehouses = SimpleNamespace(
            list=lambda: [SimpleNamespace(id="wh-1", state="RUNNING")]
        )
        self.statement_execution = SimpleNamespace(
            execute_statement=self._execute
        )
        self.jobs = SimpleNamespace(
            list=lambda: [
                SimpleNamespace(job_id=11, settings=SimpleNamespace(name="nightly-etl"))
            ],
            get_run=lambda run_id: SimpleNamespace(
                state=SimpleNamespace(
                    life_cycle_state="TERMINATED",
                    result_state="SUCCESS",
                    state_message="done",
                ),
                run_page_url="https://x/run/1",
            ),
            run_now=lambda job_id: SimpleNamespace(run_id=999),
        )
        self.last_statement = None

    def _execute(self, *, warehouse_id, statement, wait_timeout):
        self.last_statement = (warehouse_id, statement)
        return SimpleNamespace(
            status=SimpleNamespace(state="SUCCEEDED", error=None),
            manifest=SimpleNamespace(
                schema=SimpleNamespace(columns=[SimpleNamespace(name="n")])
            ),
            result=SimpleNamespace(data_array=[["1"], ["2"]]),
        )


@pytest.fixture
def tools():
    client = _FakeClient()
    return DatabricksTools(client_factory=lambda: client), client


def test_uc_browsing(tools):
    t, _ = tools
    assert "main" in t.uc_list_catalogs()
    assert "sales" in t.uc_list_schemas("main")
    assert "orders" in t.uc_list_tables("main", "sales")


def test_uc_describe_table(tools):
    t, _ = tools
    out = t.uc_describe_table("main.sales.orders")
    assert "main.sales.orders" in out
    assert "id: bigint" in out
    assert "Owner: me@example.com" in out


def test_sql_query_read_only_runs(tools):
    t, client = tools
    out = t.sql_query("SELECT n FROM main.sales.orders")
    assert "n" in out  # header
    assert client.last_statement[0] == "wh-1"  # resolved running warehouse


def test_sql_query_refuses_writes(tools):
    t, client = tools
    out = t.sql_query("DELETE FROM main.sales.orders")
    assert "Refused" in out
    assert client.last_statement is None  # never executed


def test_lineage_uses_system_table(tools):
    t, client = tools
    t.uc_table_lineage("main.sales.orders")
    assert "system.access.table_lineage" in client.last_statement[1]


def test_jobs_and_run(tools):
    t, _ = tools
    assert "nightly-etl" in t.list_jobs()
    assert "11" in t.list_jobs(name_filter="nightly")
    assert "SUCCESS" in t.get_run_status(1)
    assert "999" in t.run_job(11)


def test_error_is_returned_not_raised():
    def boom():
        raise RuntimeError("no auth")

    t = DatabricksTools(client_factory=boom)
    out = t.uc_list_catalogs()
    assert "Could not list catalogs" in out
    assert "no auth" in out


def test_as_tools_builds_langchain_tools(tools):
    pytest.importorskip("langchain_core.tools")
    t, _ = tools
    built = t.as_tools()
    names = {tool.name for tool in built}
    assert "uc_list_catalogs" in names
    assert "sql_query" in names
    assert "run_job" in names
    # run_job can be excluded.
    assert "run_job" not in {tool.name for tool in t.as_tools(include_run_job=False)}
