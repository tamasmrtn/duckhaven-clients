"""SQL builders for the cursor's catalog/schema/table/column metadata methods.

dbt and BI tools introspect relations through the DB-API metadata methods. Columns come
from ``DESCRIBE`` — the only thing that reports an attached Iceberg table's real schema
(see :func:`columns_query`) — wrapped in a ``SELECT`` so it can be projected and filtered.

Filters are bound as ``qmark`` parameters (rendered safely client-side): ``catalog`` is an
exact match; ``schema``/``table``/``column`` are ``LIKE`` patterns, matching the DB-API
convention. Relation names are quoted identifiers, not parameters.
"""

from __future__ import annotations

from typing import Any

from ._params import quote_identifier
from .dbapi import ProgrammingError

_Query = tuple[str, list[Any]]

# The six columns DuckDB's DESCRIBE returns, in order:
# (column_name, column_type, null, key, default, extra).
_DESCRIBE_COLUMNS = ("column_name", "column_type", "null")


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
    """Columns of one relation, via ``DESCRIBE``.

    ``information_schema.columns`` cannot introspect an attached Iceberg REST table:
    instead of the real columns it returns a single placeholder row (name ``__``, type
    ``UNKNOWN``). Worse, it is *inconsistent* — once something in the same session has
    touched a table, that table reports correctly while every other one still shows the
    placeholder — so it silently returns wrong data rather than failing. DuckDB loads an
    Iceberg schema lazily and the maintainers have declined to make it eager, so this is
    not expected to be fixed; ``DESCRIBE`` is DuckHaven's stated contract for columns.

    ``DESCRIBE`` answers for exactly one relation and reports no catalog/schema/ordinal,
    so those three are synthesized here to keep the result shape callers already consume.
    The ``SELECT * FROM (DESCRIBE …)`` wrapping is deliberate: it is what makes the output
    projectable and filterable, and it is recognized by the server's grant check on a
    scoped catalog (``metadata`` tier) exactly like a bare ``DESCRIBE``.
    """
    if not table_name:
        raise ProgrammingError(
            "columns() requires table_name: DuckHaven reports columns with DESCRIBE, "
            "which describes one relation (information_schema.columns cannot introspect "
            "Iceberg tables). Enumerate relations with tables() first."
        )
    if any(w in table_name for w in "%_"):
        raise ProgrammingError(
            f"columns() takes an exact table_name, not the pattern {table_name!r}: "
            "DESCRIBE names a single relation."
        )
    parts = [p for p in (catalog, schema_name, table_name) if p]
    relation = ".".join(quote_identifier(p) for p in parts)
    clauses = [
        "SELECT ? AS table_catalog, ? AS table_schema, ? AS table_name,",
        "column_name, row_number() OVER () AS ordinal_position,",
        'column_type AS data_type, "null" AS is_nullable',
        f"FROM (DESCRIBE {relation})",
    ]
    params: list[Any] = [catalog, schema_name, table_name]
    if column_name is not None:
        clauses.append("WHERE column_name LIKE ?")
        params.append(column_name)
    clauses.append("ORDER BY ordinal_position")
    return " ".join(clauses), params
