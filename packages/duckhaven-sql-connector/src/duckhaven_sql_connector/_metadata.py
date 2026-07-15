"""SQL builders for the cursor's catalog/schema/table/column metadata methods.

dbt and BI tools introspect relations through the DB-API metadata methods. DuckHaven has
no dedicated metadata endpoint, so
these run ordinary ``SELECT``s (allowed by the statement policy) against the ANSI-standard
``information_schema`` views the agent's DuckDB exposes over the attached catalogs.

Filters are bound as ``qmark`` parameters (rendered safely client-side): ``catalog`` is an
exact match; ``schema``/``table``/``column`` are ``LIKE`` patterns, matching the DB-API
convention.
"""

from __future__ import annotations

from typing import Any

_Query = tuple[str, list[Any]]


def _build(select: str, table: str, filters: list[tuple[str, str, Any]], order: str) -> _Query:
    clauses = [f"SELECT {select} FROM {table} WHERE TRUE"]
    params: list[Any] = []
    for column, op, value in filters:
        if value is not None:
            clauses.append(f"AND {column} {op} ?")
            params.append(value)
    clauses.append(f"ORDER BY {order}")
    return " ".join(clauses), params


def catalogs_query() -> _Query:
    return (
        "SELECT DISTINCT catalog_name FROM information_schema.schemata ORDER BY catalog_name",
        [],
    )


def schemas_query(catalog: str | None = None, schema_name: str | None = None) -> _Query:
    return _build(
        "catalog_name, schema_name",
        "information_schema.schemata",
        [("catalog_name", "=", catalog), ("schema_name", "LIKE", schema_name)],
        "catalog_name, schema_name",
    )


def tables_query(
    catalog: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
) -> _Query:
    return _build(
        "table_catalog, table_schema, table_name, table_type",
        "information_schema.tables",
        [
            ("table_catalog", "=", catalog),
            ("table_schema", "LIKE", schema_name),
            ("table_name", "LIKE", table_name),
        ],
        "table_catalog, table_schema, table_name",
    )


def columns_query(
    catalog: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
    column_name: str | None = None,
) -> _Query:
    return _build(
        "table_catalog, table_schema, table_name, column_name, "
        "ordinal_position, data_type, is_nullable",
        "information_schema.columns",
        [
            ("table_catalog", "=", catalog),
            ("table_schema", "LIKE", schema_name),
            ("table_name", "LIKE", table_name),
            ("column_name", "LIKE", column_name),
        ],
        "table_catalog, table_schema, table_name, ordinal_position",
    )
