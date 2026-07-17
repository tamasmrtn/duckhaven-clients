import json

import httpx
import pytest
import respx

from duckhaven_sql_connector import connect
from duckhaven_sql_connector.connection import Connection
from duckhaven_sql_connector.dbapi import InterfaceError, OperationalError, ProgrammingError

from .dh_support import (
    AGENT_ID,
    QUERY_ID,
    QUERY_URL,
    ROWS_URL,
    SESSION_URL,
    SESSIONS_URL,
    STATEMENTS_URL,
    make_config,
    make_transport,
    mock_open_session,
    open_conn,
    session_json,
)


@respx.mock
def test_open_populates_session_fields():
    conn = open_conn()
    assert conn.agent_id == AGENT_ID
    assert conn.active_catalog == "sales"
    assert conn.staging_uri.endswith("/_staging/abc")


@respx.mock
def test_close_deletes_session_and_is_idempotent():
    delete = respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    conn.close()  # second close is a no-op, no second DELETE
    assert delete.call_count == 1


@respx.mock
def test_context_manager_closes():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    with open_conn() as conn:
        assert conn._closed is False
    assert conn._closed is True


@respx.mock
def test_cursor_on_closed_connection_raises():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    with pytest.raises(ProgrammingError):
        conn.cursor()


@respx.mock
def test_session_gone_marks_connection_dead():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            409, json={"detail": {"error": "session_not_open", "detail": "reaped"}}
        )
    )
    cur = conn.cursor()
    with pytest.raises(OperationalError):
        cur.execute("SELECT 1")
    # The connection is now dead: opening another cursor fails fast.
    with pytest.raises(OperationalError):
        conn.cursor()


@respx.mock
def test_open_failure_closes_transport():
    respx.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
    config = make_config()
    transport = make_transport(config)
    with pytest.raises(OperationalError):
        Connection.open(config, transport=transport)
    assert transport._client.is_closed


@respx.mock
def test_schema_default_issues_quoted_use():
    mock_open_session(active_catalog="sales")
    statements = respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            202, json={"id": "22222222-2222-2222-2222-222222222222", "status": "done"}
        )
    )
    respx.get("https://dh.test/api/queries/22222222-2222-2222-2222-222222222222/rows").mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )

    config = make_config(schema="analytics")
    Connection.open(config, transport=make_transport(config))

    sent_sql = json.loads(statements.calls.last.request.content)["sql"]
    assert sent_sql == 'USE "sales"."analytics"'


def test_agent_must_be_uuid():
    with pytest.raises(InterfaceError):
        make_config(agent="warehouse-a")


@respx.mock
def test_open_sends_agent_and_catalog_in_body():
    open_route = respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(201, json=session_json())
    )
    config = make_config(agent=AGENT_ID, catalog="raw")
    Connection.open(config, transport=make_transport(config))
    body = json.loads(open_route.calls.last.request.content)
    assert body == {"agent_id": AGENT_ID, "catalog": "raw"}


@respx.mock
def test_top_level_connect_opens_over_a_real_transport():
    mock_open_session()
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    with connect(host="https://dh.test", workspace="analytics", token="dh_pat_x") as conn:
        assert conn.active_catalog == "sales"


@respx.mock
def test_cancel_cancels_the_in_flight_statement():
    # dbt aborts a run by cancelling the connection from another thread; that must reach
    # the cursor's active statement and DELETE it on the query API.
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done"})
    )
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cancel = respx.delete(QUERY_URL).mock(return_value=httpx.Response(204))
    cur = conn.cursor()
    cur.execute("SELECT 1")
    conn.cancel()
    assert cancel.called


@respx.mock
def test_cancel_is_a_noop_without_any_statement():
    # No statement was ever submitted, so there is nothing to DELETE; no query route is
    # registered, so respx would raise if cancel tried to call one.
    conn = open_conn()
    conn.cursor()
    conn.cancel()  # must not raise
