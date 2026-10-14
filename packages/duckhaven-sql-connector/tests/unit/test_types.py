"""Result values become the Python type their DuckDB column type calls for.

The same mapping must come out of an ``api_version`` 2 page (DECIMAL and wide integers
as JSON numbers) and an ``api_version`` 3 page (as strings).
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from duckhaven_sql_connector import InterfaceError
from duckhaven_sql_connector._types import DuckType, converter_for, parse_type

from .dh_support import QUERY_ID, ROWS_URL, STATEMENTS_URL, open_conn


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BIGINT", DuckType("BIGINT")),
        ("DECIMAL(18,4)", DuckType("DECIMAL")),
        ("TIMESTAMP WITH TIME ZONE", DuckType("TIMESTAMP WITH TIME ZONE")),
        ("ENUM('a', 'it''s', 'b,c')", DuckType("ENUM")),
        ("INTEGER[]", DuckType("LIST", [("", DuckType("INTEGER"))])),
        (
            "DECIMAL(38,0)[2][]",
            DuckType("LIST", [("", DuckType("LIST", [("", DuckType("DECIMAL"))]))]),
        ),
        (
            'STRUCT(x DECIMAL(38,0), HUGEINT VARCHAR, "we""ird" DATE)',
            DuckType(
                "STRUCT",
                [
                    ("x", DuckType("DECIMAL")),
                    ("HUGEINT", DuckType("VARCHAR")),
                    ('we"ird', DuckType("DATE")),
                ],
            ),
        ),
        (
            "MAP(VARCHAR, STRUCT(a BIGINT)[])",
            DuckType(
                "MAP",
                [
                    ("key", DuckType("VARCHAR")),
                    (
                        "value",
                        DuckType("LIST", [("", DuckType("STRUCT", [("a", DuckType("BIGINT"))]))]),
                    ),
                ],
            ),
        ),
    ],
)
def test_parse_type(text, expected):
    assert parse_type(text) == expected


@pytest.mark.parametrize(
    ("type_text", "v2", "v3", "expected"),
    [
        ("BIGINT", 9007199254740993, "9007199254740993", 9007199254740993),
        ("HUGEINT", None, "12345678901234567890123", 12345678901234567890123),
        ("UBIGINT", 18446744073709551615, "18446744073709551615", 18446744073709551615),
        ("DECIMAL(18,4)", 12.3456, "12.3456", Decimal("12.3456")),
        (
            "DECIMAL(38,10)",
            None,
            "123456789012345678.1234567891",
            Decimal("123456789012345678.1234567891"),
        ),
        ("DATE", "2024-01-02", "2024-01-02", date(2024, 1, 2)),
        ("TIMESTAMP", "2024-01-02T03:04:05", "2024-01-02T03:04:05", datetime(2024, 1, 2, 3, 4, 5)),
        (
            "TIMESTAMP WITH TIME ZONE",
            "2024-01-02T03:04:05+00:00",
            "2024-01-02T03:04:05+00:00",
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        ),
        ("TIME", "03:04:05", "03:04:05", time(3, 4, 5)),
        (
            "UUID",
            "12345678-1234-5678-1234-567812345678",
            "12345678-1234-5678-1234-567812345678",
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ),
        ("BLOB", "616263", "616263", b"abc"),
        ("VARCHAR", "2024-01-02", "2024-01-02", "2024-01-02"),
        ("DOUBLE", 1.5, 1.5, 1.5),
        ("BOOLEAN", True, True, True),
    ],
)
def test_values_convert_the_same_from_either_api_version(type_text, v2, v3, expected):
    convert = converter_for(type_text)
    for raw in (v2, v3):
        if raw is None:
            continue
        value = raw if convert is None else convert(raw)
        assert value == expected
        assert type(value) is type(expected)


def test_a_v2_float_decimal_keeps_its_short_spelling():
    """Decimal(0.1) would be the float's binary expansion, not 0.1."""
    assert converter_for("DECIMAL(10,1)")(0.1) == Decimal("0.1")


def test_nested_values_convert_by_their_member_types():
    convert = converter_for("STRUCT(amt DECIMAL(10,2), at DATE, tags VARCHAR[], ids BIGINT[])")
    assert convert({"amt": "1.50", "at": "2024-01-02", "tags": ["a"], "ids": ["1", 2]}) == {
        "amt": Decimal("1.50"),
        "at": date(2024, 1, 2),
        "tags": ["a"],
        "ids": [1, 2],
    }
    assert converter_for("MAP(BIGINT, DECIMAL(5,1))")({"1": "2.5"}) == {1: Decimal("2.5")}
    assert converter_for("DATE[]")(["2024-01-02", None]) == [date(2024, 1, 2), None]


def test_types_needing_no_conversion_have_no_converter():
    for type_text in (
        "VARCHAR",
        "DOUBLE",
        "BOOLEAN",
        "STRUCT(a VARCHAR)",
        "UNION(a INTEGER)",
        "JSON",
    ):
        assert converter_for(type_text) is None


def _query(page: dict) -> list[tuple]:
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done", "row_count": 1})
    )
    respx.get(ROWS_URL).mock(return_value=httpx.Response(200, json=page))
    cur = open_conn().cursor()
    cur.execute("SELECT * FROM t")
    return cur.fetchall()


_SCHEMA = [
    {"name": "n", "type": "BIGINT"},
    {"name": "amt", "type": "DECIMAL(18,2)"},
    {"name": "ts", "type": "TIMESTAMP WITH TIME ZONE"},
]


@respx.mock
def test_a_v2_page_and_a_v3_page_fetch_the_same_row():
    expected = [(5, Decimal("12.50"), datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(0))))]
    for row in (
        {"n": 5, "amt": 12.5, "ts": "2024-01-02T03:04:05+00:00"},
        {"n": "5", "amt": "12.50", "ts": "2024-01-02T03:04:05+00:00"},
    ):
        page = {"rows": [row], "columns": ["n", "amt", "ts"], "cursor": None, "total": 1}
        assert _query({**page, "column_schema": _SCHEMA}) == expected


@respx.mock
def test_a_value_of_the_wrong_shape_is_an_interface_error():
    page = {
        "rows": [{"n": "five", "amt": "1", "ts": "2024-01-02T03:04:05+00:00"}],
        "columns": ["n", "amt", "ts"],
        "cursor": None,
        "total": 1,
        "column_schema": _SCHEMA,
    }
    with pytest.raises(InterfaceError, match="could not decode"):
        _query(page)
