"""The ``duckhaven`` adapter.

Subclasses dbt-duckdb's adapter and swaps only the connection manager. The DuckDB
dialect, Relation/Column classes, and (via macro dispatch) the materializations and
macros are all inherited.
"""

from __future__ import annotations

from collections.abc import Sequence

from dbt_common.contracts.constraints import ConstraintType

from dbt.adapters.base.impl import ConstraintSupport
from dbt.adapters.capability import (
    Capability,
    CapabilityDict,
    CapabilitySupport,
    Support,
)
from dbt.adapters.duckdb.impl import DuckDBAdapter

from .connections import DuckHavenConnectionManager


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

    # dbt-duckdb advertises MicrobatchConcurrency=Full. Microbatch is not a v1
    # materialization here and running batches concurrently would open one DuckHaven
    # session (admission slot) per batch, so do not advertise concurrent microbatch.
    _capabilities = CapabilityDict(
        {
            Capability.MicrobatchConcurrency: CapabilitySupport(support=Support.NotImplemented),
        }
    )

    def valid_incremental_strategies(self) -> Sequence[str]:
        # v1 supports append + delete+insert. `merge` is deferred (it needs Iceberg
        # MERGE / temp-relation semantics still stabilizing on the remote session), and
        # the local duckdb version must not silently enable it.
        return ["append", "delete+insert"]
