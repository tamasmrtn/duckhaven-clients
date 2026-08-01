"""Adapter-level overrides that correct dbt-duckdb defaults for a remote Iceberg backend."""

from types import SimpleNamespace

import pytest
from dbt.adapters.base.impl import ConstraintSupport
from dbt.adapters.capability import Capability, Support
from dbt.adapters.duckhaven.impl import DuckHavenAdapter
from dbt_common.contracts.constraints import ConstraintType
from packaging.version import Version


def _adapter_on(duckdb_version: str):
    """A stand-in for an adapter bound to an agent running ``duckdb_version``.

    ``valid_incremental_strategies`` only reads ``self.duckdb_version`` (dbt-duckdb resolves
    that with a live ``select version()``), so a namespace is enough to exercise the gate
    without a server.
    """
    return SimpleNamespace(duckdb_version=Version(duckdb_version))


def test_constraints_reflect_iceberg_reality():
    # Polaris/Iceberg enforces NOT NULL on write but not unique/pk/fk; check is unsupported.
    cs = DuckHavenAdapter.CONSTRAINT_SUPPORT
    assert cs[ConstraintType.not_null] == ConstraintSupport.ENFORCED
    assert cs[ConstraintType.check] == ConstraintSupport.NOT_SUPPORTED
    assert cs[ConstraintType.unique] == ConstraintSupport.NOT_ENFORCED
    assert cs[ConstraintType.primary_key] == ConstraintSupport.NOT_ENFORCED
    assert cs[ConstraintType.foreign_key] == ConstraintSupport.NOT_ENFORCED


def test_does_not_advertise_concurrent_microbatch():
    # Inherited dbt-duckdb value is Support.Full. Microbatch IS supported here, but each
    # concurrent batch would take its own session (admission slot) and race the others to
    # commit to the same Iceberg table, so batches must stay serial.
    support = DuckHavenAdapter._capabilities[Capability.MicrobatchConcurrency].support
    assert support == Support.NotImplemented


def test_merge_and_microbatch_enabled_on_iceberg_capable_agent():
    strategies = DuckHavenAdapter.valid_incremental_strategies(_adapter_on("1.5.4"))
    assert set(strategies) == {"append", "delete+insert", "merge", "microbatch"}


@pytest.mark.parametrize("duckdb_version", ["1.5.2", "1.4.5"])
def test_merge_and_microbatch_gated_below_iceberg_merge_support(duckdb_version):
    # duckdb-iceberg only gained MERGE INTO in 1.5.3. dbt-duckdb gates at 1.4.0-dev0, which
    # is a core-DuckDB gate — inheriting it would advertise `merge` on an agent that cannot
    # serve it. 1.4.5 pins that we are deliberately stricter than dbt-duckdb.
    strategies = DuckHavenAdapter.valid_incremental_strategies(_adapter_on(duckdb_version))
    assert set(strategies) == {"append", "delete+insert"}


def test_list_relation_names_uses_the_connector_browse_listing():
    """SQL enumeration (information_schema, duckdb_tables(), SHOW, PRAGMA show_tables) is
    rejected on a workspace with any scoped catalog attached, so the listing has to come
    from the connector's tables(), which reads the grant-filtered REST endpoint."""
    captured = {}

    class _Cursor:
        closed = False

        def tables(self, catalog=None, schema_name=None):
            captured["filters"] = (catalog, schema_name)

        def fetchall(self):
            return [
                ("sales", "analytics", "orders", "MANAGED"),
                ("sales", "analytics", "customers", "MANAGED"),
            ]

        def close(self):
            type(self).closed = True

    cursor = _Cursor()
    connection = SimpleNamespace(handle=SimpleNamespace(cursor=lambda: cursor))
    adapter = SimpleNamespace(connections=SimpleNamespace(get_thread_connection=lambda: connection))

    relations = DuckHavenAdapter.list_relation_names(adapter, "sales", "analytics")

    assert captured["filters"] == ("sales", "analytics")
    assert relations == [
        {"table_name": "orders", "table_type": "MANAGED"},
        {"table_name": "customers", "table_type": "MANAGED"},
    ]
    assert _Cursor.closed


def test_list_schema_names_uses_the_connector_browse_listing():
    """information_schema.schemata is rejected on a workspace with any scoped catalog
    attached, so the listing has to come from the connector's schemas(), which reads the
    grant-filtered REST endpoint."""
    captured = {}

    class _Cursor:
        closed = False

        def schemas(self, catalog=None, schema_name=None):
            captured["filters"] = (catalog, schema_name)

        def fetchall(self):
            return [("sales", "analytics"), ("sales", "raw")]

        def close(self):
            type(self).closed = True

    cursor = _Cursor()
    connection = SimpleNamespace(handle=SimpleNamespace(cursor=lambda: cursor))
    adapter = SimpleNamespace(connections=SimpleNamespace(get_thread_connection=lambda: connection))

    names = DuckHavenAdapter.list_schema_names(adapter, "sales")

    assert captured["filters"] == ("sales", None)
    assert names == ["analytics", "raw"]
    assert _Cursor.closed


def test_get_catalog_rows_uses_list_relation_names_and_describe():
    """duckdb_tables()/duckdb_views()/duckdb_columns() are all rejected on a workspace with
    any scoped catalog attached, so this must go through list_relation_names (REST) and
    DESCRIBE (grant-checked per relation) instead, and return a real agate.Table."""
    executed = []

    class _Cursor:
        closed = False

        def execute(self, sql):
            executed.append(sql)

        def fetchall(self):
            # One call per relation; alternate between the two relations' columns.
            if "orders" in executed[-1]:
                return [("id", "BIGINT"), ("total", "DECIMAL(10,2)")]
            return [("id", "BIGINT")]

        def close(self):
            type(self).closed = True

    relation_lookups = {
        "analytics": [{"table_name": "orders", "table_type": "MANAGED"}],
        "raw": [{"table_name": "events_view", "table_type": "VIEW"}],
    }

    cursor = _Cursor()
    connection = SimpleNamespace(handle=SimpleNamespace(cursor=lambda: cursor))
    adapter = SimpleNamespace(
        connections=SimpleNamespace(get_thread_connection=lambda: connection),
        list_relation_names=lambda database, schema: relation_lookups[schema],
        quote=lambda value: f'"{value}"',
    )

    table = DuckHavenAdapter.get_catalog_rows(adapter, "sales", ["analytics", "raw"])

    assert table.column_names == (
        "table_database",
        "table_schema",
        "table_name",
        "table_type",
        "table_comment",
        "column_name",
        "column_index",
        "column_type",
        "column_comment",
        "table_owner",
    )
    rows = [tuple(row) for row in table.rows]
    assert rows == [
        ("sales", "analytics", "orders", "BASE TABLE", None, "id", 1, "BIGINT", None, None),
        (
            "sales",
            "analytics",
            "orders",
            "BASE TABLE",
            None,
            "total",
            2,
            "DECIMAL(10,2)",
            None,
            None,
        ),
        ("sales", "raw", "events_view", "VIEW", None, "id", 1, "BIGINT", None, None),
    ]
    assert all("describe" in sql.lower() for sql in executed)
    assert all("information_schema" not in sql.lower() for sql in executed)
    assert _Cursor.closed


def test_get_column_schema_from_query_wraps_describe_in_a_select():
    # dbt-duckdb emits a bare `DESCRIBE (<sql>)`. Selecting from it is the spelling that
    # works everywhere: an older agent materialized results via `COPY (<sql>) TO ...`, and
    # `COPY (DESCRIBE ...)` is a parser error, and DuckHaven grant-checks the wrapped form
    # as metadata-only on a scoped catalog.
    captured = {}

    class _Connections:
        def add_select_query(self, sql):
            captured["sql"] = sql
            return None, SimpleNamespace(fetchall=lambda: [("id", "INTEGER")])

    adapter = SimpleNamespace(connections=_Connections())
    columns = DuckHavenAdapter.get_column_schema_from_query(adapter, "select 1 as id")

    assert captured["sql"] == "select * from (describe (select 1 as id))"
    assert not captured["sql"].lstrip().lower().startswith("describe")
    assert [(c.column, c.dtype) for c in columns] == [("id", "INTEGER")]
