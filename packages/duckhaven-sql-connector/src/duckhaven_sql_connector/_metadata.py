"""Backends for the cursor's catalog/schema/table/column metadata methods.

dbt and BI tools introspect relations through the DB-API metadata methods. Neither of the
two obvious SQL routes works on DuckHaven, so this module takes a different one for each:

* **Columns** come from ``DESCRIBE`` (see :func:`columns_query`), wrapped in a ``SELECT``
  so they can be projected and filtered. ``information_schema.columns`` reports a
  placeholder for attached Iceberg tables.
* **Catalog, schema and table listings** come from DuckHaven's REST browse endpoints.
  Engine-side enumeration — ``information_schema.*``, ``duckdb_tables()`` and friends,
  ``SHOW``, the enumerating ``PRAGMA``s — is rejected outright once *any* catalog in the
  workspace uses a scoped attachment, because DuckDB computes those listings across every
  attachment and cannot narrow them to the caller's grants. The REST endpoints can, and
  they behave identically on open catalogs, so they are the single path here.

Column filters are bound as ``qmark`` parameters (rendered safely client-side) and
relation names are quoted identifiers; the REST listings are filtered client-side by
:func:`_like_match`. ``catalog`` is an exact match; ``schema``/``table``/``column`` are
``LIKE`` patterns, matching the DB-API convention.
"""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING, Any

from ._params import quote_identifier
from .dbapi import ProgrammingError

if TYPE_CHECKING:
    from .client import Transport

_Query = tuple[str, list[Any]]
# Column names and rows for a listing the client assembled itself.
_Rows = tuple[list[str], list[tuple[Any, ...]]]


def _like_match(value: str, pattern: str | None) -> bool:
    """Match ``value`` against a SQL ``LIKE`` pattern, case-sensitively.

    The listings come back as JSON rather than a result set, so the ``LIKE`` filters the
    DB-API convention expects are applied here instead of by the engine. ``%``/``_``
    translate to ``fnmatch``'s ``*``/``?``; the glob's own metacharacters are escaped
    first so a literal ``[`` in a name cannot open a character class.
    """
    if pattern is None:
        return True
    translated = fnmatch.translate(pattern.replace("[", "[[]").replace("%", "*").replace("_", "?"))
    return re.match(translated, value) is not None


def _catalog_slugs(transport: Transport, workspace: str, catalog: str | None) -> list[str]:
    if catalog is not None:
        return [catalog]
    listing = transport.get(f"/workspaces/{workspace}/catalogs").json()
    return sorted(c["slug"] for c in listing)


def fetch_catalogs(transport: Transport, workspace: str) -> _Rows:
    """Catalogs attached to the workspace (``GET /workspaces/{ws}/catalogs``)."""
    return ["catalog_name"], [(slug,) for slug in _catalog_slugs(transport, workspace, None)]


def fetch_schemas(
    transport: Transport,
    workspace: str,
    catalog: str | None = None,
    schema_name: str | None = None,
) -> _Rows:
    """Schemas, one request per catalog in scope."""
    rows: list[tuple[Any, ...]] = []
    for slug in _catalog_slugs(transport, workspace, catalog):
        listing = transport.get(f"/workspaces/{workspace}/catalogs/{slug}/schemas").json()
        rows += [(slug, s["name"]) for s in listing if _like_match(s["name"], schema_name)]
    return ["catalog_name", "schema_name"], sorted(rows)


def fetch_tables(
    transport: Transport,
    workspace: str,
    catalog: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
) -> _Rows:
    """Tables, one request per catalog plus one per schema in scope."""
    _, schemas = fetch_schemas(transport, workspace, catalog, schema_name)
    rows: list[tuple[Any, ...]] = []
    for cat, schema in schemas:
        listing = transport.get(
            f"/workspaces/{workspace}/catalogs/{cat}/schemas/{schema}/tables"
        ).json()
        rows += [
            (cat, schema, t["name"], t.get("table_type"))
            for t in listing
            if _like_match(t["name"], table_name)
        ]
    return ["table_catalog", "table_schema", "table_name", "table_type"], sorted(rows)


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
