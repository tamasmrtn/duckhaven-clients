"""Results read as Arrow (``GET /queries/{id}/result``) when pyarrow is installed and
the server is ``api_version`` 3 or later — and the same Python values either way."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pyarrow as pa
import respx

from .dh_support import (
    QUERY_ID,
    RESULT_URL,
    ROWS_URL,
    STATEMENTS_URL,
    mock_api_version,
    open_conn,
)

SCHEMA = [
    {"name": "n", "type": "BIGINT"},
    {"name": "amt", "type": "DECIMAL(18,2)"},
    {"name": "big", "type": "HUGEINT"},
    {"name": "at", "type": "TIMESTAMP WITH TIME ZONE"},
    {"name": "day", "type": "DATE"},
    {"name": "tags", "type": "MAP(VARCHAR, BIGINT)"},
    {"name": "rec", "type": "STRUCT(x DECIMAL(4,1), h HUGEINT)"},
]
# What the agent's Parquet carries for those columns, as the API re-encodes it.
ARROW = pa.table(
    {
        "n": pa.array([5], pa.int64()),
        "amt": pa.array([Decimal("12.50")], pa.decimal128(18, 2)),
        "big": pa.array([Decimal("12345678901234567890123")], pa.decimal128(38, 0)),
        "at": pa.array([datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
        "day": pa.array([date(2024, 1, 2)], pa.date32()),
        "tags": pa.array([[("a", 1)]], pa.map_(pa.string(), pa.int64())),
        "rec": pa.array(
            [{"x": Decimal("1.5"), "h": Decimal(7)}],
            pa.struct([("x", pa.decimal128(4, 1)), ("h", pa.decimal128(38, 0))]),
        ),
    }
)
# The same row as an api_version 3 JSON page sends it.
JSON_ROW = {
    "n": "5",
    "amt": "12.50",
    "big": "12345678901234567890123",
    "at": "2024-01-02T03:04:05+00:00",
    "day": "2024-01-02",
    "tags": {"a": "1"},
    "rec": {"x": "1.5", "h": "7"},
}
EXPECTED = (
    5,
    Decimal("12.50"),
    12345678901234567890123,
    datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
    date(2024, 1, 2),
    {"a": 1},
    {"x": Decimal("1.5"), "h": 7},
)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _done(schema=SCHEMA) -> respx.Route:
    return respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": QUERY_ID, "status": "done", "row_count": 1, "column_schema": schema},
        )
    )


@respx.mock
def test_a_current_server_streams_arrow_and_takes_no_json_page():
    mock_api_version(3)
    submit = _done()
    result = respx.get(RESULT_URL).mock(return_value=httpx.Response(200, content=_ipc(ARROW)))
    rows = respx.get(ROWS_URL)
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    assert json.loads(submit.calls.last.request.content)["first_page_limit"] == 0
    assert cur.fetchall() == [EXPECTED]
    assert result.calls.last.request.url.params["format"] == "arrow"
    assert not rows.called


@respx.mock
def test_arrow_and_json_give_the_same_python_values():
    mock_api_version(2)
    _done()
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [JSON_ROW],
                "columns": list(JSON_ROW),
                "cursor": None,
                "total": 1,
                "column_schema": SCHEMA,
            },
        )
    )
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    assert cur.fetchall() == [EXPECTED]


@respx.mock
def test_an_older_server_pages_json():
    mock_api_version(2)
    _done()
    result = respx.get(RESULT_URL)
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"rows": [JSON_ROW], "columns": list(JSON_ROW), "cursor": None, "total": 1},
        )
    )
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    cur.fetchall()
    assert not result.called


@respx.mock
def test_fetch_arrow_table_hands_over_the_arrow_data():
    mock_api_version(3)
    _done()
    respx.get(RESULT_URL).mock(return_value=httpx.Response(200, content=_ipc(ARROW)))
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    table = cur.fetch_arrow_table()
    assert table.schema.field("big").type == pa.decimal128(38, 0)
    assert table.num_rows == 1


@respx.mock
def test_description_comes_from_the_query_without_reading_the_stream():
    mock_api_version(3)
    _done()
    result = respx.get(RESULT_URL).mock(return_value=httpx.Response(200, content=_ipc(ARROW)))
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    assert [d[0] for d in cur.description] == [c["name"] for c in SCHEMA]
    assert not result.called


@respx.mock
def test_a_statement_without_a_grid_has_no_description():
    mock_api_version(3)
    _done(schema=None)
    respx.get(RESULT_URL).mock(return_value=httpx.Response(204))
    cur = open_conn().cursor()
    cur.execute("CREATE TABLE t (x INTEGER)")
    assert cur.description is None
    assert cur.fetchall() == []


@respx.mock
def test_without_a_schema_the_stream_names_the_columns():
    """An agent older than column_schema still answers; the names come from Arrow."""
    mock_api_version(3)
    _done(schema=None)
    respx.get(RESULT_URL).mock(
        return_value=httpx.Response(200, content=_ipc(pa.table({"answer": [42]})))
    )
    cur = open_conn().cursor()
    cur.execute("SELECT 42 AS answer")
    assert [d[0] for d in cur.description] == ["answer"]
    assert cur.fetchall() == [(42,)]
