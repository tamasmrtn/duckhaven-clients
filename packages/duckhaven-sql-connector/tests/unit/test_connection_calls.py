"""Connection calls that run no statement: cancel_query, ping and metadata."""

import httpx
import pytest
import respx

from duckhaven_sql_connector.connection import Connection
from duckhaven_sql_connector.dbapi import InterfaceError, OperationalError, ProgrammingError

from .dh_support import (
    BASE,
    QUERY_ID,
    QUERY_URL,
    SESSION_URL,
    WS,
    make_config,
    make_transport,
    open_conn,
    session_json,
)

METADATA_URL = f"{BASE}/workspaces/{WS}/metadata"


def _auto() -> Connection:
    config = make_config(mode="auto")
    return Connection.open(config, transport=make_transport(config))


@respx.mock
def test_cancel_query_needs_no_session():
    conn = _auto()
    deleted = respx.delete(QUERY_URL).mock(return_value=httpx.Response(204))
    conn.cancel_query(QUERY_ID)
    assert deleted.called
    assert conn._session_id is None


@respx.mock
def test_cancel_query_with_an_unknown_id_raises():
    conn = _auto()
    respx.delete(QUERY_URL).mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "Query not found"})
    )
    with pytest.raises(ProgrammingError):
        conn.cancel_query(QUERY_ID)


def test_cancel_query_on_a_closed_connection_raises():
    with respx.mock:
        conn = _auto()
        conn.close()
        with pytest.raises(ProgrammingError, match="closed"):
            conn.cancel_query(QUERY_ID)


@respx.mock
def test_ping_without_a_session_checks_the_token():
    conn = _auto()
    me = respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"id": "u"}))
    conn.ping()
    assert me.called


@respx.mock
def test_ping_with_a_revoked_token_raises():
    conn = _auto()
    respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized", "message": "revoked"})
    )
    with pytest.raises(InterfaceError):
        conn.ping()


@respx.mock
def test_ping_checks_the_session_is_still_open():
    conn = open_conn()
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=session_json()))
    conn.ping()


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json=session_json(status="closed", error="idle_timeout")),
        httpx.Response(404, json={"error": "not_found", "message": "Session not found"}),
    ],
)
@respx.mock
def test_ping_on_a_session_that_ended_marks_the_connection_dead(answer):
    conn = open_conn()
    respx.get(SESSION_URL).mock(return_value=answer)
    with pytest.raises(OperationalError, match="open a new connection"):
        conn.ping()
    with pytest.raises(OperationalError, match="no longer open"):
        conn.cursor()


@respx.mock
def test_ping_passes_other_errors_through():
    conn = open_conn()
    respx.get(SESSION_URL).mock(
        return_value=httpx.Response(403, json={"error": "forbidden", "message": "no"})
    )
    with pytest.raises(ProgrammingError):
        conn.ping()


@respx.mock
def test_metadata_returns_the_catalogs_the_token_may_see():
    conn = _auto()
    catalogs = [
        {
            "name": "sales",
            "kind": "ducklake",
            "access_mode": "scoped",
            "schemas": [
                {"name": "analytics", "tables": [{"name": "orders", "table_type": "BASE TABLE"}]}
            ],
        }
    ]
    route = respx.get(METADATA_URL).mock(
        return_value=httpx.Response(200, json={"catalogs": catalogs})
    )
    assert conn.metadata() == catalogs
    assert dict(route.calls[0].request.url.params) == {"columns": "false"}


@respx.mock
def test_metadata_passes_its_filters():
    conn = _auto()
    route = respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json={"catalogs": []}))
    conn.metadata(catalog="sales", schema="analytics", table="orders", columns=True)
    assert dict(route.calls[0].request.url.params) == {
        "columns": "true",
        "catalog": "sales",
        "schema": "analytics",
        "table": "orders",
    }
