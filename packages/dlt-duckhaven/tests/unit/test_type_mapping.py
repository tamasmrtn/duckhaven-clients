"""DuckHaven→Iceberg type mapping: each dlt type maps to an Iceberg-safe DuckDB type,
and Iceberg-incompatible shapes (nanosecond timestamps, 128-bit ints) are rejected."""

import pytest
from dlt.common.exceptions import TerminalValueError
from dlt_duckhaven.factory import duckhaven


@pytest.fixture
def mapper():
    return duckhaven().capabilities().get_type_mapper()


def _col(**kwargs):
    return {"name": "c", **kwargs}


_TABLE = {"name": "t"}


@pytest.mark.parametrize(
    "data_type,expected",
    [
        ("text", "VARCHAR"),
        ("json", "VARCHAR"),  # Iceberg has no JSON type
        ("bool", "BOOLEAN"),
        ("double", "DOUBLE"),
        ("date", "DATE"),
        ("time", "TIME"),
        ("binary", "BLOB"),
        ("bigint", "BIGINT"),
    ],
)
def test_unbound_types(mapper, data_type, expected):
    assert mapper.to_destination_type(_col(data_type=data_type), _TABLE) == expected


def test_timestamp_tz_and_precision(mapper):
    assert (
        mapper.to_destination_type(_col(data_type="timestamp"), _TABLE)
        == "TIMESTAMP WITH TIME ZONE"
    )
    assert (
        mapper.to_destination_type(_col(data_type="timestamp", timezone=False), _TABLE)
        == "TIMESTAMP"
    )


def test_decimal_and_wei(mapper):
    assert (
        mapper.to_destination_type(_col(data_type="decimal", precision=10, scale=2), _TABLE)
        == "DECIMAL(10,2)"
    )
    assert mapper.to_destination_type(_col(data_type="wei"), _TABLE) == "DECIMAL(38,0)"


def test_sized_integers(mapper):
    assert mapper.to_destination_type(_col(data_type="bigint", precision=8), _TABLE) == "TINYINT"
    assert mapper.to_destination_type(_col(data_type="bigint", precision=16), _TABLE) == "SMALLINT"
    assert mapper.to_destination_type(_col(data_type="bigint", precision=32), _TABLE) == "INTEGER"
    assert mapper.to_destination_type(_col(data_type="bigint", precision=64), _TABLE) == "BIGINT"


def test_hugeint_rejected(mapper):
    with pytest.raises(TerminalValueError):
        mapper.to_destination_type(_col(data_type="bigint", precision=128), _TABLE)


def test_nanosecond_timestamp_rejected(mapper):
    with pytest.raises(TerminalValueError):
        mapper.to_destination_type(_col(data_type="timestamp", precision=9, timezone=False), _TABLE)


def test_wei_roundtrips_from_decimal_38_0(mapper):
    assert mapper.from_destination_type("DECIMAL", 38, 0)["data_type"] == "wei"
    assert mapper.from_destination_type("DECIMAL(10,2)", 10, 2)["data_type"] == "decimal"
