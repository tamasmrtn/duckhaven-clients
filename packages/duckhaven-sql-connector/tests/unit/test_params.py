from datetime import date, datetime, time
from decimal import Decimal

import pytest

from duckhaven_sql_connector._params import quote_identifier, render_literal, render_qmark
from duckhaven_sql_connector.dbapi import ProgrammingError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (42, "42"),
        (3.5, "3.5"),
        (Decimal("1.50"), "1.50"),
        ("plain", "'plain'"),
        ("o'brien", "'o''brien'"),
        (b"\x00\xff", "unhex('00ff')"),
        (date(2020, 1, 2), "DATE '2020-01-02'"),
        (time(3, 4, 5), "TIME '03:04:05'"),
        (datetime(2020, 1, 2, 3, 4, 5), "TIMESTAMP '2020-01-02 03:04:05'"),
    ],
)
def test_render_literal(value, expected):
    assert render_literal(value) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), Decimal("NaN")])
def test_render_literal_rejects_non_finite(bad):
    with pytest.raises(ProgrammingError):
        render_literal(bad)


def test_render_literal_rejects_unknown_type():
    with pytest.raises(ProgrammingError):
        render_literal(object())


def test_quote_identifier_escapes_quotes():
    assert quote_identifier("a") == '"a"'
    assert quote_identifier('a"b') == '"a""b"'


@pytest.mark.parametrize(
    ("sql", "params", "expected"),
    [
        ("SELECT ?", [1], "SELECT 1"),
        ("INSERT INTO t VALUES (?, ?)", [1, "o'brien"], "INSERT INTO t VALUES (1, 'o''brien')"),
        ("SELECT '?'", [], "SELECT '?'"),
        ('SELECT "a?b"', [], 'SELECT "a?b"'),
        ("SELECT ? -- ?\n, ?", [1, 2], "SELECT 1 -- ?\n, 2"),
        ("SELECT ? /* ? */ ?", [1, 2], "SELECT 1 /* ? */ 2"),
        ("SELECT 'a''b', ?", [1], "SELECT 'a''b', 1"),
    ],
)
def test_render_qmark(sql, params, expected):
    assert render_qmark(sql, params) == expected


def test_render_qmark_too_few_params():
    with pytest.raises(ProgrammingError):
        render_qmark("SELECT ?", [])


def test_render_qmark_too_many_params():
    with pytest.raises(ProgrammingError):
        render_qmark("SELECT 1", [1])
