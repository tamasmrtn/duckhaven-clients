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


def _rows_page(**over):
    body = {"rows": [], "columns": [], "cursor": None, "total": 0}
    body.update(over)
    return respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json=body))


@respx.mock
def test_a_statement_already_done_on_submit_is_never_polled():
    """The server holds the submit call until the statement finishes, so the usual
    answer is terminal and the poll route must not be touched at all. This is the
    whole point of the wait: a statement cost four status round trips and up to a
    poll interval of lateness purely to notice it had already finished."""
    conn = open_conn()
    submit = _submit(status="done", row_count=0)
    poll = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json={"id": QUERY_ID}))
    _rows_page()

    cur = conn.cursor().execute("SELECT 1")

    assert submit.call_count == 1
    assert poll.call_count == 0, "a finished statement must not be polled"
    # The submit body IS the result when it comes back terminal. Keeping only its id
    # and status silently dropped row_count for every statement that finished inside
    # the wait -- rowcount -1 is a PEP 249 regression dbt and dlt both read.
    assert cur.rowcount == 0


@respx.mock
def test_statement_wait_is_sent_on_submit_and_on_the_fallback_poll():
    """Both legs carry the budget: the submit call, and the status route for a
    statement that outran it."""
    conn = open_conn(make_config(statement_wait=12.0))
    submit = _submit(status="running")
    poll = _poll({"status": "done", "row_count": 0})
    _rows_page()

    conn.cursor().execute("SELECT 1")

    assert json.loads(submit.calls[0].request.content)["wait_timeout_s"] == 12.0
    assert poll.calls[0].request.url.params["wait_timeout_s"] == "12.0"


@respx.mock
def test_statement_wait_unset_sends_nothing_so_the_server_default_wins():
    """None means "no opinion" -- an operator's SQL_STATEMENT_WAIT_TIMEOUT_S is not
    overridden by a client that never asked for anything."""
    conn = open_conn()
    submit = _submit(status="running")
    poll = _poll({"status": "done", "row_count": 0})
    _rows_page()

    conn.cursor().execute("SELECT 1")

    assert "wait_timeout_s" not in json.loads(submit.calls[0].request.content)
    assert "wait_timeout_s" not in poll.calls[0].request.url.params


@respx.mock
def test_statement_wait_zero_is_sent_and_restores_submit_then_poll():
    """0 is a real value, not "unset": it asks the server never to hold the call.
    Distinguishing the two is what makes an A/B of the wait possible on one server."""
    conn = open_conn(make_config(statement_wait=0))
    submit = _submit(status="running")
    poll = _poll({"status": "done", "row_count": 0})
    _rows_page()

    conn.cursor().execute("SELECT 1")

    assert json.loads(submit.calls[0].request.content)["wait_timeout_s"] == 0
    assert poll.call_count == 1


def test_statement_wait_beyond_the_socket_timeout_is_rejected():
    """The server holds the response for the whole wait, so a socket deadline inside
    it would abort the un-retried POST the wait exists to serve."""
    with pytest.raises(InterfaceError, match="statement_wait must be less than http_timeout"):
        make_config(statement_wait=60.0, http_timeout=60.0)


def test_negative_statement_wait_is_rejected():
    with pytest.raises(InterfaceError, match="statement_wait must not be negative"):
        make_config(statement_wait=-1.0)


@respx.mock
def test_a_statement_failing_on_submit_reports_the_real_error():
    """A failure that lands inside the wait must carry its message. The submit body
    holds it; discarding that left every such failure raising the generic
    "statement failed (failed)", which tells a user nothing about what went wrong."""
    conn = open_conn()
    _submit(status="failed", error="IO Error: Could not connect to server")
    poll = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json={"id": QUERY_ID}))

    with pytest.raises(ProgrammingError, match="IO Error: Could not connect to server"):
        conn.cursor().execute("SELECT 1")
    assert poll.call_count == 0


@respx.mock
def test_row_count_and_errors_still_come_from_the_poll_when_submit_is_pending():
    """The other half of the same contract: when submit is still running, the terminal
    poll response is what carries them, exactly as before."""
    conn = open_conn()
    _submit(status="running")
    _poll({"status": "done", "row_count": 7})
    _rows_page()

    cur = conn.cursor().execute("SELECT 1")
    assert cur.rowcount == 7


def _page(rows, columns, cursor=None, total=None, schema=None):
    body = {
        "rows": rows,
        "columns": columns,
        "cursor": cursor,
        "total": total if total is not None else len(rows),
    }
    if schema is not None:
        body["column_schema"] = schema
    return body


@respx.mock
def test_an_inlined_first_page_costs_no_rows_request():
    """The saving this exists for. Without it every statement -- `SELECT 1` included,
    and even for a caller that reads nothing -- makes a second HTTP call purely so
    `.description` has column names."""
    conn = open_conn()
    submit = _submit(
        status="done",
        row_count=2,
        first_page=_page([{"n": 1}, {"n": 2}], ["n"], schema=[{"name": "n", "type": "INTEGER"}]),
    )
    rows_route = respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json=_page([], [])))

    cur = conn.cursor().execute("SELECT n FROM t")

    assert submit.call_count == 1
    assert rows_route.call_count == 0, "fetched rows the submit response already carried"
    assert cur.fetchall() == [(1,), (2,)]
    assert cur.description[0][0] == "n"
    assert cur.description[0][1] == "INTEGER"
    assert cur.rowcount == 2


@respx.mock
def test_an_inlined_page_shorter_than_the_result_still_pages():
    """An inlined page is an ordinary page: if it carries a cursor there are more
    rows, and paging continues from it rather than restarting."""
    conn = open_conn()
    _submit(status="done", row_count=3, first_page=_page([{"n": 1}], ["n"], cursor="1", total=3))
    rest = respx.get(ROWS_URL).mock(
        return_value=httpx.Response(200, json=_page([{"n": 2}, {"n": 3}], ["n"], total=3))
    )

    cur = conn.cursor().execute("SELECT n FROM t")

    assert cur.fetchall() == [(1,), (2,), (3,)]
    assert rest.call_count == 1
    assert rest.calls[0].request.url.params["cursor"] == "1"


@respx.mock
def test_the_first_page_limit_is_sent_and_bounded_by_fetch_size():
    """Asking for more rows than the caller will buffer is pointless, so the request
    is the smaller of the two."""
    conn = open_conn(make_config(first_page_limit=200, fetch_size=25))
    submit = _submit(status="done", row_count=0, first_page=_page([], []))

    conn.cursor().execute("SELECT 1")

    assert json.loads(submit.calls[0].request.content)["first_page_limit"] == 25


@respx.mock
def test_first_page_limit_zero_restores_the_separate_rows_request():
    """0 opts out -- the pre-inline behaviour, and the control arm for measuring it."""
    conn = open_conn(make_config(first_page_limit=0))
    submit = _submit(status="done", row_count=0)
    rows_route = _rows_page()

    conn.cursor().execute("SELECT 1")

    assert "first_page_limit" not in json.loads(submit.calls[0].request.content)
    assert rows_route.call_count == 1


@respx.mock
def test_a_server_that_ignores_the_field_still_works():
    """Against a server too old to inline, the response simply has no `first_page`
    and the cursor fetches rows the way it always did."""
    conn = open_conn()
    _submit(status="done", row_count=1)
    rows_route = respx.get(ROWS_URL).mock(
        return_value=httpx.Response(200, json=_page([{"n": 7}], ["n"]))
    )

    cur = conn.cursor().execute("SELECT n FROM t")

    assert rows_route.call_count == 1
    assert cur.fetchall() == [(7,)]
