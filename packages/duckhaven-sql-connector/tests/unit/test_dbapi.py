from datetime import date, datetime, time

import duckhaven_sql_connector as dh
from duckhaven_sql_connector import dbapi


def test_module_globals():
    assert dh.apilevel == "2.0"
    assert dh.threadsafety == 1
    assert dh.paramstyle == "qmark"


def test_connect_and_exceptions_exported_at_package_root():
    assert callable(dh.connect)
    for name in ("Error", "InterfaceError", "OperationalError", "ProgrammingError"):
        assert hasattr(dh, name)


def test_type_objects_compare_by_type_name():
    assert dbapi.STRING == "VARCHAR"
    assert dbapi.STRING != "BLOB"
    assert dbapi.BINARY == "BLOB"
    assert dbapi.NUMBER == "BIGINT"
    assert dbapi.DATETIME == "TIMESTAMP"
    assert dbapi.ROWID == "ROWID"


def test_type_objects_match_a_type_by_its_base_type():
    """`description` carries DuckDB's full spelling, parameters included."""
    assert dbapi.NUMBER == "DECIMAL(18,4)"
    assert dbapi.NUMBER == "BIGNUM"
    assert dbapi.STRING == "VARCHAR(10)"
    assert dbapi.DATETIME == "TIMESTAMP WITH TIME ZONE"
    assert dbapi.NUMBER != "INTEGER[]"  # a list of numbers is not a number
    assert dbapi.STRING != "STRUCT(a VARCHAR)"
    assert dbapi.NUMBER != "NOT A TYPE ("
    assert dbapi.NUMBER != None  # noqa: E711 - PEP 249 type objects compare with ==


def test_constructors():
    assert dbapi.Date(2020, 1, 2) == date(2020, 1, 2)
    assert dbapi.Time(3, 4, 5) == time(3, 4, 5)
    assert dbapi.Timestamp(2020, 1, 2, 3, 4, 5) == datetime(2020, 1, 2, 3, 4, 5)
    assert dbapi.Binary(bytearray(b"xy")) == b"xy"
    assert isinstance(dbapi.DateFromTicks(0), date)
    assert isinstance(dbapi.TimeFromTicks(0), time)
    assert isinstance(dbapi.TimestampFromTicks(0), datetime)
