"""Typed server-side parameters against DuckHaven ``api_version`` 3 or later."""

import json
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import httpx
import pytest
import respx

from duckhaven_sql_connector._params import server_parameter
from duckhaven_sql_connector.dbapi import ProgrammingError

from .dh_support import QUERY_ID, STATEMENTS_URL, mock_api_version, open_conn


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (9007199254740993, 9007199254740993),
        (0.5, 0.5),
        ("it's", "it's"),
        (Decimal("12.50"), {"type": "DECIMAL(4,2)", "value": "12.50"}),
        (Decimal("1E+3"), {"type": "DECIMAL(4,0)", "value": "1E+3"}),
        (Decimal("0.001"), {"type": "DECIMAL(3,3)", "value": "0.001"}),
        (datetime(2024, 1, 2, 3, 4, 5), {"type": "TIMESTAMP", "value": "2024-01-02 03:04:05"}),
        (
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
            {"type": "TIMESTAMPTZ", "value": "2024-01-02 03:04:05+00:00"},
        ),
        (date(2024, 1, 2), {"type": "DATE", "value": "2024-01-02"}),
        (time(3, 4, 5), {"type": "TIME", "value": "03:04:05"}),
        (b"abc", {"type": "BLOB", "value": "YWJj"}),
        (uuid.UUID(int=1), {"type": "UUID", "value": "00000000-0000-0000-0000-000000000001"}),
        ([1, None, 3], {"type": "BIGINT[]", "value": [1, None, 3]}),
        (("a", "b"), {"type": "VARCHAR[]", "value": ["a", "b"]}),
        ([date(2024, 1, 2)], {"type": "DATE[]", "value": ["2024-01-02"]}),
    ],
)
def test_values_map_to_typed_parameters(value, expected):
    assert server_parameter(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        Decimal("NaN"),
        Decimal(10) ** 40,
        [1, "a"],
        [None],
        {"a": 1},
        time(1, tzinfo=UTC),
    ],
)
def test_what_cannot_be_typed_is_refused(value):
    with pytest.raises(ProgrammingError):
        server_parameter(value)


def _submitted(api_version: int, sql: str, params: list) -> dict:
    mock_api_version(api_version)
    submit = respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(200, json={"id": QUERY_ID, "status": "done", "row_count": 0})
    )
    respx.get(f"https://dh.test/api/queries/{QUERY_ID}/rows").mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    respx.get(f"https://dh.test/api/queries/{QUERY_ID}/result").mock(
        return_value=httpx.Response(204)
    )
    open_conn().cursor().execute(sql, params)
    return json.loads(submit.calls.last.request.content)


@respx.mock
def test_a_current_server_gets_the_values_and_the_sql_untouched():
    body = _submitted(
        3, "SELECT * FROM t WHERE amt > ? AND day = ?", [Decimal("1.5"), date(2024, 1, 2)]
    )
    assert body["sql"] == "SELECT * FROM t WHERE amt > ? AND day = ?"
    assert body["parameters"] == [
        {"type": "DECIMAL(2,1)", "value": "1.5"},
        {"type": "DATE", "value": "2024-01-02"},
    ]


@respx.mock
def test_an_older_server_gets_rendered_sql():
    body = _submitted(2, "SELECT * FROM t WHERE a = ?", ["x"])
    assert body["sql"] == "SELECT * FROM t WHERE a = 'x'"
    assert "parameters" not in body
