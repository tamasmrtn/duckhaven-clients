"""The ``duckhaven`` adapter.

Subclasses dbt-duckdb's adapter and swaps only the connection manager. The DuckDB
dialect, Relation/Column classes, and (via macro dispatch) the materializations and
macros are all inherited.
"""

from __future__ import annotations

from collections.abc import Sequence

from dbt_common.contracts.constraints import ConstraintType
from packaging.version import Version

from dbt.adapters.base.impl import ConstraintSupport, available
from dbt.adapters.capability import (
    Capability,
    CapabilityDict,
    CapabilitySupport,
    Support,
)
from dbt.adapters.duckdb.column import DuckDBColumn
from dbt.adapters.duckdb.impl import DuckDBAdapter

from .connections import DuckHavenConnectionManager

# `merge` and `microbatch` write to Iceberg through duckdb-iceberg, which only gained
# MERGE INTO in 1.5.3 (UPDATE/DELETE landed in 1.4.2). dbt-duckdb's own gate is 1.4.0-dev0,
# which is a *core DuckDB* MERGE gate, not an Iceberg one — inheriting it would advertise
# `merge` on an agent that then dies with a raw binder error mid-run, after the temp table
# was already built. Gate on the version that can actually serve the statement.
DUCKHAVEN_MERGE_MIN_VERSION = "1.5.3"


class DuckHavenAdapter(DuckDBAdapter):
    ConnectionManager = DuckHavenConnectionManager

    # dbt-duckdb inherits all-ENFORCED, which assumes a local DuckDB. Polaris/Iceberg
    # enforces no relational constraints except NOT NULL on write, so advertise the truth
    # (model contracts must not promise enforcement the backend does not provide).
    CONSTRAINT_SUPPORT = {
        ConstraintType.check: ConstraintSupport.NOT_SUPPORTED,
        ConstraintType.not_null: ConstraintSupport.ENFORCED,
        ConstraintType.unique: ConstraintSupport.NOT_ENFORCED,
        ConstraintType.primary_key: ConstraintSupport.NOT_ENFORCED,
        ConstraintType.foreign_key: ConstraintSupport.NOT_ENFORCED,
    }

    # dbt-duckdb advertises MicrobatchConcurrency=Full, which assumes a local DuckDB file.
    # Here each concurrent batch would take its own DuckHaven session (an agent admission
    # slot), and those sessions would race to commit to the same Iceberg table — Iceberg's
    # optimistic concurrency turns that into commit conflicts. Batches run serially.
    _capabilities = CapabilityDict(
        {
            Capability.MicrobatchConcurrency: CapabilitySupport(support=Support.NotImplemented),
        }
    )

    def valid_incremental_strategies(self) -> Sequence[str]:
        # `duckdb_version` is inherited from dbt-duckdb: a cached `select version()` against
        # the agent, so this reflects the compute actually serving the statements.
        strategies = ["append", "delete+insert"]
        if self.duckdb_version >= Version(DUCKHAVEN_MERGE_MIN_VERSION):
            strategies += ["merge", "microbatch"]
        return strategies

    @available.parse(lambda *a, **k: [])
    def get_column_schema_from_query(self, sql: str) -> list[DuckDBColumn]:
        """Describe a query's result columns, wrapping DESCRIBE in a SELECT.

        dbt-duckdb issues a bare ``DESCRIBE (<sql>)``, which cannot survive a DuckHaven
        session: DuckDB reports DESCRIBE as a SELECT statement, so the agent materializes it
        with ``COPY (<sql>) TO … (FORMAT PARQUET)`` — and ``COPY (DESCRIBE …)`` is a parser
        error. Selecting from the DESCRIBE makes it a genuine SELECT that wraps cleanly, the
        same shape duckhaven__get_columns_in_relation relies on. Columns come back in the
        same (name, type, …) order, so the rest matches dbt-duckdb.

        Snapshots reach this on every run via check_time_data_types.
        """
        describe_sql = f"select * from (describe ({sql}))"
        _, cursor = self.connections.add_select_query(describe_sql)
        flattened_columns: list[DuckDBColumn] = []
        for row in cursor.fetchall():
            name, dtype = row[0], row[1]
            column = DuckDBColumn(column=name, dtype=dtype)
            flattened_columns.extend(column.flatten())
        return flattened_columns
