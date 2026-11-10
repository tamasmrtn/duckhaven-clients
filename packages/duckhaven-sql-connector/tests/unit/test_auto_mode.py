"""``mode="auto"``: one-shot queries by default, a session only once a statement needs one."""

import json

import httpx
import pytest
import respx

from duckhaven_sql_connector import connect
from duckhaven_sql_connector.config import ClientConfig
from duckhaven_sql_connector.connection import Connection
from duckhaven_sql_connector.cursor import _needs_session
from duckhaven_sql_connector.dbapi import (
    InterfaceError,
    NotSupportedError,
    OperationalError,
)

from .dh_support import (
    QUERY_ID,
    QUERY_URL,
    ROWS_URL,
    SESSION_ID,
    SESSION_URL,
    SESSIONS_URL,
    STATEMENTS_URL,
    VERSION_URL,
    WS,
    make_config,
    make_transport,
    mock_open_session,
    session_json,
)

QUERIES_URL = f"https://dh.test/api/workspaces/{WS}/queries"


def _auto(**over) -> Connection:
    # A server predating GET /version unless the test says otherwise: JSON results.
    probe = httpx.Request("GET", VERSION_URL)
    if not any(route.pattern.match(probe) for route in respx.routes):
        respx.get(VERSION_URL).mock(return_value=httpx.Response(404))
    config = make_config(mode="auto", **over)
    return Connection.open(config, transport=make_transport(config))


def _one_shot(status="done", **over) -> respx.Route:
    body = {"id": QUERY_ID, "status": status, "row_count": 1, "has_result": True}
    body.update(over)
    return respx.post(QUERIES_URL).mock(return_value=httpx.Response(200, json=body))


def _rows(rows=({"n": 1},), columns=("n",)) -> respx.Route:
    return respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"rows": list(rows), "columns": list(columns), "cursor": None, "total": len(rows)},
        )
    )


def _version(features=()) -> None:
    respx.get(VERSION_URL).mock(
        return_value=httpx.Response(
            200, json={"version": "t", "api_version": 2, "features": list(features)}
        )
    )


def _body(route: respx.Route, call: int = -1) -> dict:
    return json.loads(route.calls[call].request.content)


@respx.mock
def test_connecting_sends_nothing():
    # No routes are registered, so any request would raise.
    conn = _auto()
    assert conn._session_id is None
    conn.close()


@respx.mock
def test_a_select_runs_as_a_one_shot_query():
    conn = _auto(catalog="sales", agent="33333333-3333-3333-3333-333333333333", timeout=42.0)
    submit = _one_shot()
    _rows()
    cur = conn.cursor()
    cur.execute("SELECT 1 AS n")
    assert cur.fetchall() == [(1,)]
    assert cur.rowcount == 1
    assert _body(submit) == {
        "sql": "SELECT 1 AS n",
        "timeout_s": 42.0,
        "catalog": "sales",
        "agent_id": "33333333-3333-3333-3333-333333333333",
        # Held server-side, so a quick query answers on this one call. No
        # `first_page_limit`: that is a session statement's field.
        "wait_timeout_s": 30.0,
    }


@respx.mock
def test_the_one_shot_wait_stays_inside_the_socket_deadline():
    conn = _auto(http_timeout=20.0)
    submit = _one_shot()
    _rows()
    conn.cursor().execute("SELECT 1")
    assert _body(submit)["wait_timeout_s"] == 10.0


@respx.mock
def test_statement_wait_applies_to_one_shot_queries_and_their_polls():
    conn = _auto(statement_wait=5.0)
    submit = _one_shot(status="running")
    poll = respx.get(QUERY_URL).mock(
        return_value=httpx.Response(200, json={"id": QUERY_ID, "status": "done", "row_count": 0})
    )
    _rows(rows=(), columns=())
    conn.cursor().execute("SELECT 1")
    assert _body(submit)["wait_timeout_s"] == 5.0
    assert poll.calls[0].request.url.params["wait_timeout_s"] == "5.0"


@respx.mock
def test_a_failed_one_shot_query_raises_its_error():
    conn = _auto()
    _one_shot(status="failed", error="Catalog Error: Table t does not exist")
    with pytest.raises(Exception, match="Table t does not exist"):
        conn.cursor().execute("SELECT * FROM t")


@respx.mock
def test_a_conflict_on_a_one_shot_query_leaves_the_connection_usable():
    conn = _auto()
    respx.post(QUERIES_URL).mock(
        side_effect=[
            httpx.Response(409, json={"error": "catalog_read_only", "message": "migrating"}),
            httpx.Response(200, json={"id": QUERY_ID, "status": "done", "has_result": False}),
        ]
    )
    with pytest.raises(OperationalError):
        conn.cursor().execute("INSERT INTO t VALUES (1)")
    conn.cursor().execute("INSERT INTO t VALUES (1)")


@pytest.mark.parametrize(
    "sql",
    [
        "SET threads = 4",
        "  reset threads",
        "USE sales.marts",
        "PRAGMA enable_progress_bar",
        "ATTACH 'x.db' AS x",
        "DETACH x",
        "CREATE TEMP TABLE t AS SELECT 1",
        "create or replace temporary view v as select 1",
        "-- a comment\n/* and another */ SET threads = 1",
    ],
)
def test_statements_that_set_state_need_a_session(sql):
    assert _needs_session(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "CREATE TABLE t AS SELECT 1",
        "CREATE VIEW temp AS SELECT 1",
        "CREATE OR REPLACE VIEW temporary AS SELECT 1",
        "INSERT INTO settings VALUES ('SET')",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "DESCRIBE t",
        "",
        "-- only a comment",
    ],
)
def test_other_statements_run_one_shot(sql):
    assert not _needs_session(sql)


@respx.mock
def test_a_stateful_statement_opens_a_session_and_the_connection_stays_on_it():
    conn = _auto()
    mock_open_session()
    statements = respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            200, json={"id": QUERY_ID, "status": "done", "has_result": False}
        )
    )
    one_shot = respx.post(QUERIES_URL)
    cur = conn.cursor()
    cur.execute("SET threads = 1")
    cur.execute("SELECT 1")
    assert statements.call_count == 2
    assert not one_shot.called
    assert conn.agent_id == session_json()["agent_id"]
    assert conn.active_catalog == "sales"


@respx.mock
def test_closing_deletes_only_a_session_that_was_opened():
    conn = _auto()
    mock_open_session()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            200, json={"id": QUERY_ID, "status": "done", "has_result": False}
        )
    )
    deleted = respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn.cursor().execute("USE sales.marts")
    conn.close()
    assert deleted.called


@respx.mock
def test_sessions_being_disabled_says_what_to_do():
    conn = _auto()
    respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(
            404, json={"error": "not_found", "message": "SQL sessions are not enabled"}
        )
    )
    with pytest.raises(NotSupportedError, match="SQL sessions are not enabled on this server"):
        conn.cursor().execute("SET threads = 1")
    # Statements that need no session still run.
    _one_shot()
    _rows()
    conn.cursor().execute("SELECT 1")


@respx.mock
def test_staging_files_open_a_session():
    conn = _auto()
    mock_open_session()
    staged = respx.post(f"{SESSION_URL}/staging-files").mock(
        return_value=httpx.Response(201, json={"files": [], "expires_at": None})
    )
    conn.stage_files(["a.parquet"])
    assert staged.called
    assert conn._session_id == SESSION_ID


@respx.mock
def test_a_schema_rides_on_the_one_shot_query_when_the_server_applies_it():
    _version(features=["query_schema"])
    conn = _auto(schema="marts")
    submit = _one_shot()
    _rows()
    conn.cursor().execute("SELECT * FROM orders")
    assert _body(submit)["schema"] == "marts"


@pytest.mark.parametrize("features", [None, ()])
@respx.mock
def test_a_schema_the_server_cannot_apply_moves_the_connection_to_a_session(features):
    """Sent to a server without `query_schema`, `schema` would be ignored and names
    would resolve in its default schema, writes included."""
    if features is None:
        respx.get(VERSION_URL).mock(return_value=httpx.Response(404))
    else:
        _version(features)
    conn = _auto(schema="marts")
    mock_open_session()
    statements = respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            200, json={"id": QUERY_ID, "status": "done", "has_result": False}
        )
    )
    one_shot = respx.post(QUERIES_URL)
    conn.cursor().execute("CREATE TABLE orders AS SELECT 1")
    sent = [json.loads(c.request.content)["sql"] for c in statements.calls]
    assert sent == ['USE "sales"."marts"', "CREATE TABLE orders AS SELECT 1"]
    assert not one_shot.called


def test_an_unknown_mode_is_refused():
    with pytest.raises(InterfaceError, match="mode"):
        ClientConfig(host="https://dh.test", workspace=WS, token="t", mode="stateless")


@respx.mock
def test_connect_passes_the_mode_through():
    conn = connect("https://dh.test", WS, "dh_pat_x", mode="auto")
    assert conn._config.mode == "auto"
    assert conn._session_id is None
    conn.close()


@respx.mock
def test_server_version_reports_features():
    _version(features=["query_schema"])
    conn = _auto()
    assert conn.server_version().features == frozenset({"query_schema"})


@respx.mock
def test_a_server_without_features_reports_none():
    respx.get(VERSION_URL).mock(
        return_value=httpx.Response(200, json={"version": "t", "api_version": 3})
    )
    assert _auto().server_version().features == frozenset()


@respx.mock
def test_a_session_that_fails_to_open_raises_why():
    conn = _auto(compute_wait=0)
    respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(
            503, json={"error": "compute_unavailable", "message": "Could not start compute"}
        )
    )
    with pytest.raises(OperationalError, match="Could not start compute"):
        conn.cursor().execute("SET threads = 1")
    assert conn._session_id is None
