"""DuckHavenSqlClient: opens a session via the connector, drives statements through the
session cursor, qualifies catalog.schema.table, and maps connector errors to dlt errors."""

from unittest.mock import MagicMock

import dlt_duckhaven.sql_client as sql_client_mod
import pytest
from dlt.destinations.exceptions import (
    DatabaseTerminalException,
    DatabaseTransientException,
    DatabaseUndefinedRelation,
)
from dlt_duckhaven.factory import duckhaven
from dlt_duckhaven.sql_client import DuckHavenSqlClient

import duckhaven_sql_connector as dbapi


def _client(catalog="raw", dataset="analytics"):
    factory = duckhaven(
        host="https://h", workspace="ws", agent=None, catalog=catalog, credentials="dh_pat_x"
    )
    config = factory.configuration(factory.spec(), accept_partial=True)
    return DuckHavenSqlClient(dataset, f"{dataset}_staging", config, factory.capabilities())


def _fake_cursor(description=None, rows=None):
    cursor = MagicMock()
    cursor.description = description
    cursor.fetchall.return_value = rows or []
    return cursor


def _patch_connect(monkeypatch, cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(sql_client_mod.dbapi, "connect", connect)
    return connect, conn


def test_open_connection_maps_config(monkeypatch):
    connect, conn = _patch_connect(monkeypatch, _fake_cursor())
    client = _client()
    assert client.open_connection() is conn
    kwargs = connect.call_args.kwargs
    assert kwargs["host"] == "https://h"
    assert kwargs["workspace"] == "ws"
    assert kwargs["token"] == "dh_pat_x"
    assert kwargs["catalog"] == "raw"
    # No USE of a maybe-missing schema — relations are fully qualified.
    assert kwargs["schema"] is None
    assert kwargs["application"].startswith("dlt-duckhaven/")


def test_execute_sql_converts_pyformat_params(monkeypatch):
    cursor = _fake_cursor(description=[("n", None, None, None, None, None, None)], rows=[(1,)])
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()

    rows = client.execute_sql("SELECT x FROM t WHERE a = %s", 5)

    cursor.execute.assert_called_once_with("SELECT x FROM t WHERE a = ?", [5])
    assert rows == [(1,)]


def test_execute_sql_without_description_returns_none(monkeypatch):
    cursor = _fake_cursor(description=None)
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()
    assert client.execute_sql("CREATE TABLE t (x int)") is None


def test_execute_sql_splits_multi_statement_ddl(monkeypatch):
    cursor = _fake_cursor(description=None)
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()

    client.execute_sql("CREATE TABLE a (x int); CREATE TABLE b (y int)")

    executed = [call.args[0].strip() for call in cursor.execute.call_args_list]
    assert executed == ["CREATE TABLE a (x int)", "CREATE TABLE b (y int)"]


def test_qualified_table_name():
    client = _client(catalog="raw", dataset="analytics")
    assert client.make_qualified_table_name("orders") == '"raw"."analytics"."orders"'


@pytest.mark.parametrize(
    "exc,expected",
    [
        (dbapi.ProgrammingError("Table orders does not exist"), DatabaseUndefinedRelation),
        (dbapi.ProgrammingError("Catalog Error: missing"), DatabaseUndefinedRelation),
        (dbapi.ProgrammingError("statement_not_allowed"), DatabaseTerminalException),
        (dbapi.OperationalError("session reaped"), DatabaseTransientException),
    ],
)
def test_exception_mapping(exc, expected):
    assert isinstance(DuckHavenSqlClient._make_database_exception(exc), expected)


def test_execute_query_closes_cursor(monkeypatch):
    cursor = _fake_cursor(description=[("n", None, None, None, None, None, None)], rows=[(1,)])
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()
    with client.execute_query("SELECT 1") as curr:
        assert curr.fetchall() == [(1,)]
    cursor.close.assert_called_once()
