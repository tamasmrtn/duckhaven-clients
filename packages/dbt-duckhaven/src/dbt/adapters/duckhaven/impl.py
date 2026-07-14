"""The ``duckhaven`` adapter.

Subclasses dbt-duckdb's adapter and swaps only the connection manager. The DuckDB
dialect, Relation/Column classes, and (via macro dispatch) the materializations and
macros are all inherited.
"""

from __future__ import annotations

from collections.abc import Sequence

from dbt.adapters.duckdb.impl import DuckDBAdapter

from .connections import DuckHavenConnectionManager


class DuckHavenAdapter(DuckDBAdapter):
    ConnectionManager = DuckHavenConnectionManager

    def valid_incremental_strategies(self) -> Sequence[str]:
        # v1 supports append + delete+insert. `merge` is deferred (it needs Iceberg
        # MERGE / temp-relation semantics still stabilizing on the remote session), and
        # the local duckdb version must not silently enable it.
        return ["append", "delete+insert"]
