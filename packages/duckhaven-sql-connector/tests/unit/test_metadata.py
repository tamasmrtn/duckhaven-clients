import pytest

from duckhaven_sql_connector._metadata import (
    catalogs_query,
    columns_query,
    schemas_query,
    tables_query,
)
from duckhaven_sql_connector.dbapi import ProgrammingError


def test_catalogs_query_has_no_params():
    sql, params = catalogs_query()
    assert "information_schema.schemata" in sql
    assert "DISTINCT catalog_name" in sql
    assert params == []


def test_schemas_query_applies_filters():
    sql, params = schemas_query(catalog="sales", schema_name="pub%")
    assert "catalog_name = ?" in sql
    assert "schema_name LIKE ?" in sql
    assert params == ["sales", "pub%"]


def test_tables_query_without_filters_binds_nothing():
    sql, params = tables_query()
    assert "information_schema.tables" in sql
    assert "WHERE TRUE" in sql
    assert "?" not in sql
    assert params == []


def test_tables_query_partial_filter():
    sql, params = tables_query(schema_name="public")
    assert "table_schema LIKE ?" in sql
    assert "table_catalog = ?" not in sql
    assert params == ["public"]


def test_columns_query_describes_the_qualified_relation():
    sql, params = columns_query("c", "s", "t")
    # DESCRIBE, not information_schema.columns: the latter returns a ('__', 'UNKNOWN')
    # placeholder for attached Iceberg tables.
    assert 'FROM (DESCRIBE "c"."s"."t")' in sql
    assert "information_schema" not in sql
    # catalog/schema/table are synthesized, since DESCRIBE reports none of them.
    assert params == ["c", "s", "t"]
    assert sql.endswith("ORDER BY ordinal_position")


def test_columns_query_unqualified_relation():
    sql, params = columns_query(table_name="t")
    assert 'FROM (DESCRIBE "t")' in sql
    assert params == [None, None, "t"]


def test_columns_query_quotes_the_relation_identifiers():
    sql, _ = columns_query(None, 'we"ird', "t")
    assert 'FROM (DESCRIBE "we""ird"."t")' in sql


def test_columns_query_filters_by_column_name():
    sql, params = columns_query("c", "s", "t", "col%")
    assert "WHERE column_name LIKE ?" in sql
    assert params == ["c", "s", "t", "col%"]


def test_columns_query_requires_a_table_name():
    with pytest.raises(ProgrammingError, match="requires table_name"):
        columns_query("c", "s")


def test_columns_query_rejects_a_table_pattern():
    # DESCRIBE names one relation; a LIKE pattern would silently describe nothing.
    with pytest.raises(ProgrammingError, match="exact table_name"):
        columns_query("c", "s", "orders%")
