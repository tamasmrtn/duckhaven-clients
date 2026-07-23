"""``catalogs()``/``schemas()``/``tables()`` over DuckHaven's REST browse endpoints.

Engine-side enumeration (``information_schema.*``, ``duckdb_tables()``, ``SHOW``,
``PRAGMA show_tables``) is rejected with 403 once *any* catalog in the workspace uses a
scoped attachment — verified against a live server, including for sessions working only on
open catalogs. The browse endpoints filter their listings by grant instead, so they are
the one path that works in both modes.
"""

import httpx
import pytest
import respx

from duckhaven_sql_connector.dbapi import ProgrammingError

from .dh_support import BASE, WS, open_conn

CATALOGS_URL = f"{BASE}/workspaces/{WS}/catalogs"


def _schemas_url(catalog):
    return f"{CATALOGS_URL}/{catalog}/schemas"


def _tables_url(catalog, schema):
    return f"{_schemas_url(catalog)}/{schema}/tables"


def _mock_catalogs(*slugs):
    return respx.get(CATALOGS_URL).mock(
        return_value=httpx.Response(200, json=[{"slug": s} for s in slugs])
    )


def _mock_schemas(catalog, *names):
    return respx.get(_schemas_url(catalog)).mock(
        return_value=httpx.Response(200, json=[{"name": n, "catalog": catalog} for n in names])
    )


def _mock_tables(catalog, schema, *names, table_type="MANAGED"):
    return respx.get(_tables_url(catalog, schema)).mock(
        return_value=httpx.Response(
            200,
            json=[{"name": n, "schema_name": schema, "table_type": table_type} for n in names],
        )
    )


@respx.mock
def test_catalogs_lists_the_workspace_catalogs():
    conn = open_conn()
    _mock_catalogs("sales", "analytics")
    cur = conn.cursor().catalogs()
    assert [d[0] for d in cur.description] == ["catalog_name"]
    assert cur.fetchall() == [("analytics",), ("sales",)]
    assert cur.rowcount == 2


@respx.mock
def test_schemas_fans_out_over_every_catalog():
    conn = open_conn()
    _mock_catalogs("sales", "analytics")
    _mock_schemas("sales", "raw")
    _mock_schemas("analytics", "marts")
    cur = conn.cursor().schemas()
    assert [d[0] for d in cur.description] == ["catalog_name", "schema_name"]
    assert cur.fetchall() == [("analytics", "marts"), ("sales", "raw")]


@respx.mock
def test_schemas_with_a_catalog_makes_one_request():
    conn = open_conn()
    catalogs = _mock_catalogs("sales", "analytics")
    _mock_schemas("sales", "raw", "staging")
    cur = conn.cursor().schemas(catalog="sales")
    assert cur.fetchall() == [("sales", "raw"), ("sales", "staging")]
    # Naming the catalog skips the listing request entirely.
    assert not catalogs.called


@respx.mock
def test_tables_fans_out_over_catalogs_and_schemas():
    conn = open_conn()
    _mock_catalogs("sales")
    _mock_schemas("sales", "raw", "marts")
    _mock_tables("sales", "raw", "orders")
    _mock_tables("sales", "marts", "revenue")
    cur = conn.cursor().tables()
    assert [d[0] for d in cur.description] == [
        "table_catalog",
        "table_schema",
        "table_name",
        "table_type",
    ]
    assert cur.fetchall() == [
        ("sales", "marts", "revenue", "MANAGED"),
        ("sales", "raw", "orders", "MANAGED"),
    ]


@respx.mock
def test_tables_filters_by_like_pattern():
    conn = open_conn()
    _mock_schemas("sales", "raw")
    _mock_tables("sales", "raw", "orders", "order_items", "customers")
    cur = conn.cursor().tables(catalog="sales", table_name="order%")
    assert [r[2] for r in cur.fetchall()] == ["order_items", "orders"]


@respx.mock
def test_underscore_is_a_single_character_wildcard():
    conn = open_conn()
    _mock_schemas("sales", "raw")
    _mock_tables("sales", "raw", "t1", "t22")
    cur = conn.cursor().tables(catalog="sales", table_name="t_")
    assert [r[2] for r in cur.fetchall()] == ["t1"]


@respx.mock
def test_glob_metacharacters_in_a_name_are_literal():
    """A `[` in a table name must not open a character class in the translated pattern."""
    conn = open_conn()
    _mock_schemas("sales", "raw")
    _mock_tables("sales", "raw", "a[b]c", "abc")
    cur = conn.cursor().tables(catalog="sales", table_name="a[b]c")
    assert [r[2] for r in cur.fetchall()] == ["a[b]c"]


@respx.mock
def test_listing_reports_no_column_types():
    """These rows never went through a query, so there is no server-reported schema."""
    conn = open_conn()
    _mock_catalogs("sales")
    cur = conn.cursor().catalogs()
    assert all(d[1] is None for d in cur.description)
    assert cur.column_types is None


@respx.mock
def test_empty_listing_yields_no_rows_but_a_description():
    conn = open_conn()
    respx.get(CATALOGS_URL).mock(return_value=httpx.Response(200, json=[]))
    cur = conn.cursor().catalogs()
    assert cur.fetchall() == []
    assert [d[0] for d in cur.description] == ["catalog_name"]
    assert cur.rowcount == 0


@respx.mock
def test_metadata_on_a_closed_cursor_raises():
    conn = open_conn()
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="cursor is closed"):
        cur.catalogs()
