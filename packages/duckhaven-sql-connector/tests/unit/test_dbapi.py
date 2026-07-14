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


def test_constructors():
    assert dbapi.Date(2020, 1, 2) == date(2020, 1, 2)
    assert dbapi.Time(3, 4, 5) == time(3, 4, 5)
    assert dbapi.Timestamp(2020, 1, 2, 3, 4, 5) == datetime(2020, 1, 2, 3, 4, 5)
    assert dbapi.Binary(bytearray(b"xy")) == b"xy"
    assert isinstance(dbapi.DateFromTicks(0), date)
    assert isinstance(dbapi.TimeFromTicks(0), time)
    assert isinstance(dbapi.TimestampFromTicks(0), datetime)
