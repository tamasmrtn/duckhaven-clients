"""``column_schema`` → PEP 249 ``description[1]`` (``type_code``).

DuckHaven reports a result's column types as ``column_schema: [{name, type}]`` on the rows
page, spelled the way DuckDB prints a logical type. The field is additive, so every test
here has a counterpart asserting the behaviour against a server that does not send it.
"""

import httpx
import respx

from .dh_support import QUERY_ID, ROWS_URL, STATEMENTS_URL, open_conn

# A real capture from a live DuckHaven (server carrying the column_schema field).
LIVE_SCHEMA = [
    {"name": "id", "type": "BIGINT"},
    {"name": "amt", "type": "DECIMAL(18,4)"},
    {"name": "ts", "type": "TIMESTAMP WITH TIME ZONE"},
    {"name": "txt", "type": "VARCHAR"},
    {"name": "st", "type": "STRUCT(a INTEGER, b VARCHAR)"},
    {"name": "li", "type": "INTEGER[]"},
]
LIVE_ROW = {
    "id": 1,
    "amt": 12.3456,
    "ts": "2024-01-02T03:04:05+00:00",
    "txt": "2024-05-06T07:08:09Z",
    "st": {"a": 1, "b": "x"},
    "li": [1, 2, 3],
}


def _submit_done():
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done", "row_count": 1})
    )


def _rows(**over):
    page = {
        "rows": [LIVE_ROW],
        "columns": list(LIVE_ROW),
        "cursor": None,
        "total": 1,
        "column_schema": LIVE_SCHEMA,
    }
    page.update(over)
    return respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json=page))


def _execute():
    conn = open_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM t")
    return cur


@respx.mock
def test_description_carries_the_duckdb_type():
    _submit_done()
    _rows()
    cur = _execute()
    assert [(d[0], d[1]) for d in cur.description] == [
        ("id", "BIGINT"),
        ("amt", "DECIMAL(18,4)"),
        ("ts", "TIMESTAMP WITH TIME ZONE"),
        ("txt", "VARCHAR"),
        ("st", "STRUCT(a INTEGER, b VARCHAR)"),
        ("li", "INTEGER[]"),
    ]


@respx.mock
def test_description_keeps_the_pep249_seven_tuple():
    _submit_done()
    _rows()
    cur = _execute()
    # Only name and type_code are known; the other five stay None (precision and scale
    # are inside the type string, and DuckDB relations carry no reliable nullability).
    assert cur.description[1] == ("amt", "DECIMAL(18,4)", None, None, None, None, None)
    assert all(len(d) == 7 for d in cur.description)


@respx.mock
def test_column_types_property():
    _submit_done()
    _rows()
    cur = _execute()
    assert cur.column_types == [
        "BIGINT",
        "DECIMAL(18,4)",
        "TIMESTAMP WITH TIME ZONE",
        "VARCHAR",
        "STRUCT(a INTEGER, b VARCHAR)",
        "INTEGER[]",
    ]


# -- Degradation against a server that does not report column_schema ------------------


@respx.mock
def test_older_server_omitting_the_field_leaves_type_code_none():
    """A pre-#175 server sends no ``column_schema`` key at all. That is not a malformed
    page: the names still arrive and ``type_code`` is None, exactly as before."""
    _submit_done()
    page = {"rows": [LIVE_ROW], "columns": list(LIVE_ROW), "cursor": None, "total": 1}
    respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json=page))
    cur = _execute()
    assert [d[0] for d in cur.description] == list(LIVE_ROW)
    assert all(d[1] is None for d in cur.description)
    assert cur.column_types is None
    assert cur.fetchall() == [tuple(LIVE_ROW.values())]


@respx.mock
def test_explicit_null_column_schema_is_treated_as_absent():
    """An agent older than the field makes the server send an explicit null."""
    _submit_done()
    _rows(column_schema=None)
    cur = _execute()
    assert all(d[1] is None for d in cur.description)
    assert cur.column_types is None


@respx.mock
def test_ddl_reports_no_description_at_all():
    """DDL/DML has no result schema: the server sends empty columns and a null schema."""
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done", "row_count": 0})
    )
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"rows": [], "columns": [], "cursor": None, "total": 0, "column_schema": None},
        )
    )
    conn = open_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (x INTEGER)")
    assert cur.description is None
    assert cur.column_types is None


@respx.mock
def test_schema_is_read_from_the_first_page_only():
    """Paging must not re-read (or lose) the schema; only the first page sets it."""
    _submit_done()
    first = {
        "rows": [{"id": 1}],
        "columns": ["id"],
        "cursor": "c1",
        "total": 2,
        "column_schema": [{"name": "id", "type": "BIGINT"}],
    }
    second = {"rows": [{"id": 2}], "columns": ["id"], "cursor": None, "total": 2}
    respx.get(ROWS_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    conn = open_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM t")
    assert cur.description[0][1] == "BIGINT"
    assert cur.fetchall() == [(1,), (2,)]
    assert cur.column_types == ["BIGINT"]
