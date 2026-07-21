"""DuckHavenSqlClient: opens a session via the connector, drives statements through the
session cursor, qualifies catalog.schema.table, and maps connector errors to dlt errors."""

from datetime import datetime
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


@pytest.mark.parametrize(
    "value,expected_type",
    [
        ("2026-07-18T14:51:00+00:00", datetime),
        ("2026-07-18 14:51:00.123456", datetime),
        ("2026-07-18T14:51:00Z", datetime),
        ("not a date", str),
        ("2026-07-18", str),  # date-only, no time part -> left as-is
        (42, int),
        (None, type(None)),
    ],
)
def test_coerce_value(value, expected_type):
    # The results API returns untyped JSON; timestamps arrive as ISO strings and must be
    # coerced to datetime (dlt calls pendulum.instance on them).
    assert type(sql_client_mod._coerce_value(value)) is expected_type


def test_execute_query_coerces_timestamp_columns(monkeypatch):
    """A server reporting no types (pre-`column_schema`) falls back to sniffing."""
    cursor = _fake_cursor(
        description=[("ts", None, None, None, None, None, None)],
        rows=[("2026-07-18T14:51:00+00:00",)],
    )
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()
    with client.execute_query("SELECT ts FROM t") as curr:
        (row,) = curr.fetchall()
    assert isinstance(row[0], datetime)


# -- Type-directed coercion, when the server reports column types ---------------------


def _typed_cursor(types, row):
    cursor = _fake_cursor(
        description=[(f"c{i}", t, None, None, None, None, None) for i, t in enumerate(types)],
        rows=[row],
    )
    # _fake_cursor only stubs fetchall; the mask has to apply on every fetch path.
    cursor.fetchone.return_value = row
    cursor.fetchmany.return_value = [row]
    return cursor


def _fetch_one_row(monkeypatch, cursor, method="fetchall"):
    _patch_connect(monkeypatch, cursor)
    client = _client()
    client.open_connection()
    with client.execute_query("SELECT * FROM t") as curr:
        result = getattr(curr, method)()
    return result[0] if method != "fetchone" else result


def test_varchar_holding_an_iso_string_is_left_alone(monkeypatch):
    """The bug this replaces: a genuine VARCHAR that happens to hold an ISO-8601 string
    was silently landed in the destination as a datetime."""
    cursor = _typed_cursor(
        ["TIMESTAMP WITH TIME ZONE", "VARCHAR"],
        ("2024-01-02T03:04:05+00:00", "2024-05-06T07:08:09Z"),
    )
    ts, txt = _fetch_one_row(monkeypatch, cursor)
    assert isinstance(ts, datetime)
    assert txt == "2024-05-06T07:08:09Z"
    assert isinstance(txt, str)


@pytest.mark.parametrize(
    "type_code",
    ["DATE", "TIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP_NS", "timestamp"],
)
def test_temporal_types_are_recognized(type_code):
    assert sql_client_mod._is_temporal(type_code)


@pytest.mark.parametrize("type_code", ["VARCHAR", "BIGINT", "DECIMAL(18,4)", "BLOB", None])
def test_non_temporal_types_are_not(type_code):
    assert not sql_client_mod._is_temporal(type_code)


def test_date_only_value_on_a_date_column_is_converted(monkeypatch):
    cursor = _typed_cursor(["DATE"], ("2024-05-06",))
    (value,) = _fetch_one_row(monkeypatch, cursor)
    assert isinstance(value, datetime)


def test_null_in_a_temporal_column_stays_none(monkeypatch):
    cursor = _typed_cursor(["TIMESTAMP"], (None,))
    (value,) = _fetch_one_row(monkeypatch, cursor)
    assert value is None


def test_decimal_value_is_not_touched(monkeypatch):
    """Values are already through JSON; the client must not invent precision."""
    cursor = _typed_cursor(["DECIMAL(18,4)"], (12.3456,))
    (value,) = _fetch_one_row(monkeypatch, cursor)
    assert value == 12.3456


@pytest.mark.parametrize("method", ["fetchone", "fetchmany", "fetchall"])
def test_every_fetch_method_is_type_directed(monkeypatch, method):
    cursor = _typed_cursor(
        ["TIMESTAMP", "VARCHAR"], ("2024-01-02T03:04:05", "2024-05-06T07:08:09Z")
    )
    row = _fetch_one_row(monkeypatch, cursor, method)
    assert isinstance(row[0], datetime)
    assert isinstance(row[1], str)
