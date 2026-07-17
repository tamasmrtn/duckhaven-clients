"""The ``duckhaven`` adapter.

Subclasses dbt-duckdb's adapter and swaps only the connection manager. The DuckDB
dialect, Relation/Column classes, and (via macro dispatch) the materializations and
macros are all inherited.
"""

from __future__ import annotations

from collections.abc import Sequence

from dbt_common.contracts.constraints import ConstraintType
from packaging.version import Version

from dbt.adapters.base.impl import ConstraintSupport
from dbt.adapters.capability import (
    Capability,
    CapabilityDict,
    CapabilitySupport,
    Support,
)
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
