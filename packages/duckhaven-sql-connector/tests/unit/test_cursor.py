import json

import httpx
import pytest
import respx

from duckhaven_sql_connector.dbapi import InterfaceError, OperationalError, ProgrammingError

from .dh_support import (
    QUERY_ID,
    QUERY_URL,
    ROWS_URL,
    STATEMENTS_URL,
    make_config,
    open_conn,
    steady_clock,
)


def _submit(status="queued", **over):
    body = {"id": QUERY_ID, "status": status}
    body.update(over)
    return respx.post(STATEMENTS_URL).mock(return_value=httpx.Response(202, json=body))


def _poll(*statuses):
    responses = [httpx.Response(200, json={"id": QUERY_ID, **s}) for s in statuses]
    return respx.get(QUERY_URL).mock(side_effect=responses)


@respx.mock
def test_transaction_control_statements_are_noops():
    # The session is autocommit, so a bare BEGIN/COMMIT/ROLLBACK must not be submitted
    # (there is no statements route registered here — respx would raise on any POST).
    conn = open_conn()
    cur = conn.cursor()
    for stmt in ("COMMIT", "begin", "  ROLLBACK ; ", "commit transaction"):
        cur.execute(stmt)
        assert cur.rowcount == -1
        assert cur.description is None
    # A real statement still goes through afterwards.
    _submit()
    _poll({"status": "done", "row_count": 0})
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cur.execute("create schema s")
    assert cur.rowcount == 0


@respx.mock
def test_execute_select_polls_then_fetches():
    conn = open_conn()
    _submit()
    poll = _poll({"status": "running"}, {"status": "done", "row_count": 2})
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [{"n": 1, "s": "a"}, {"n": 2, "s": "b"}],
                "columns": ["n", "s"],
                "cursor": None,
                "total": 2,
            },
        )
    )
    cur = conn.cursor()
    cur.execute("SELECT n, s FROM t")
    assert cur.rowcount == 2
    assert [d[0] for d in cur.description] == ["n", "s"]
    assert cur.fetchall() == [(1, "a"), (2, "b")]
    assert poll.call_count == 2


@respx.mock
def test_execute_iterates_rows():
    conn = open_conn()
    _submit(status="done", row_count=2)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [{"n": 1}, {"n": 2}], "columns": ["n"], "cursor": None, "total": 2}
        )
    )
    cur = conn.cursor()
    cur.execute("SELECT n FROM t")
    assert list(cur) == [(1,), (2,)]


@respx.mock
def test_failed_statement_raises_programming_error():
    conn = open_conn()
    _submit()
    _poll({"status": "running"}, {"status": "error", "error": "Binder Error: boom"})
    cur = conn.cursor()
    with pytest.raises(ProgrammingError, match="boom"):
        cur.execute("SELECT nope")


@respx.mock
def test_poll_timeout_cancels_and_raises():
    config = make_config()
    conn = open_conn(config, monotonic=steady_clock())
    _submit()
    _poll({"status": "running"}, {"status": "running"})
    cancel = respx.delete(QUERY_URL).mock(return_value=httpx.Response(204))
    cur = conn.cursor()
    with pytest.raises(OperationalError, match="timed out"):
        cur.execute("SELECT slow()")
    assert cancel.called


@respx.mock
def test_ddl_has_no_description_and_empty_rows():
    conn = open_conn()
    _submit(status="done", row_count=None)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (x INTEGER)")
    assert cur.description is None
    assert cur.rowcount == -1
    assert cur.fetchone() is None


@respx.mock
def test_result_pagination_follows_cursor():
    conn = open_conn()
    _submit(status="done", row_count=3)
    respx.get(ROWS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"rows": [{"n": 1}, {"n": 2}], "columns": ["n"], "cursor": "2", "total": 3},
            ),
            httpx.Response(
                200, json={"rows": [{"n": 3}], "columns": ["n"], "cursor": None, "total": 3}
            ),
        ]
    )
    cur = conn.cursor()
    cur.execute("SELECT n FROM t")
    assert cur.fetchall() == [(1,), (2,), (3,)]


@respx.mock
def test_execute_renders_parameters():
    conn = open_conn()
    statements = _submit(status="done", row_count=1)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cur = conn.cursor()
    cur.execute("INSERT INTO t VALUES (?, ?)", [1, "o'brien"])
    body = statements.calls.last.request.content.decode()
    assert "INSERT INTO t VALUES (1, 'o''brien')" in body


@respx.mock
def test_executemany_runs_once_per_param_set():
    conn = open_conn()
    statements = _submit(status="done", row_count=1)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cur = conn.cursor()
    cur.executemany("INSERT INTO t VALUES (?)", [[1], [2], [3]])
    assert statements.call_count == 3


@respx.mock
def test_fetchmany_respects_size_then_arraysize():
    conn = open_conn()
    _submit(status="done", row_count=3)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [{"n": 1}, {"n": 2}, {"n": 3}],
                "columns": ["n"],
                "cursor": None,
                "total": 3,
            },
        )
    )
    cur = conn.cursor()
    cur.execute("SELECT n FROM t")
    assert cur.fetchmany(2) == [(1,), (2,)]
    assert cur.fetchmany() == [(3,)]  # falls back to arraysize (1)
    assert cur.fetchmany() == []


@respx.mock
def test_malformed_rows_page_raises_interface_error():
    conn = open_conn()
    _submit(status="done", row_count=1)
    # Missing the "columns" key -> the pager can't shape rows.
    respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json={"rows": [{"n": 1}]}))
    cur = conn.cursor()
    with pytest.raises(InterfaceError):
        cur.execute("SELECT n FROM t")


@respx.mock
def test_fetch_before_execute_raises():
    conn = open_conn()
    with pytest.raises(ProgrammingError):
        conn.cursor().fetchone()


@respx.mock
def test_execute_on_closed_cursor_raises():
    conn = open_conn()
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError):
        cur.execute("SELECT 1")


@respx.mock
def test_columns_metadata_submits_a_wrapped_describe():
    conn = open_conn()
    statements = _submit(status="done", row_count=1)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "table_catalog": "sales",
                        "table_schema": "public",
                        "table_name": "orders",
                        "column_name": "id",
                        "ordinal_position": 1,
                        "data_type": "BIGINT",
                        "is_nullable": "YES",
                    }
                ],
                "columns": [
                    "table_catalog",
                    "table_schema",
                    "table_name",
                    "column_name",
                    "ordinal_position",
                    "data_type",
                    "is_nullable",
                ],
                "cursor": None,
                "total": 1,
            },
        )
    )
    cur = conn.cursor()
    cur.columns(catalog="sales", schema_name="public", table_name="orders")
    sent = json.loads(statements.calls.last.request.content)["sql"]
    assert 'FROM (DESCRIBE "sales"."public"."orders")' in sent
    assert "information_schema" not in sent
    assert "'sales' AS table_catalog" in sent  # qmark rendered client-side
    assert cur.fetchall()[0][5] == "BIGINT"


@respx.mock
def test_cancel_deletes_query():
    conn = open_conn()
    _submit(status="done", row_count=0)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cancel = respx.delete(QUERY_URL).mock(return_value=httpx.Response(204))
    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.cancel()
    assert cancel.called


@respx.mock
def test_query_id_is_tracked_while_the_statement_is_still_running():
    # The id must be recorded from the submit response, before polling completes, so a
    # cancel arriving mid-run reaches the actually-running statement. We capture the id
    # from inside a poll callback (i.e. while execute() is still blocked polling).
    conn = open_conn()
    _submit()
    cur = conn.cursor()
    seen = {}

    def _capture(_request):
        seen["id"] = cur._query_id
        return httpx.Response(200, json={"id": QUERY_ID, "status": "done", "row_count": 0})

    respx.get(QUERY_URL).mock(side_effect=_capture)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cur.execute("SELECT slow()")
    assert seen["id"] == QUERY_ID
