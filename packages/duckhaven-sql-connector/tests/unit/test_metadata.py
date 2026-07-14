from duckhaven_sql_connector._metadata import (
    catalogs_query,
    columns_query,
    schemas_query,
    tables_query,
)


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


def test_columns_query_all_filters_and_order():
    sql, params = columns_query("c", "s", "t", "col%")
    assert params == ["c", "s", "t", "col%"]
    assert "information_schema.columns" in sql
    assert sql.endswith("ORDER BY table_catalog, table_schema, table_name, ordinal_position")
