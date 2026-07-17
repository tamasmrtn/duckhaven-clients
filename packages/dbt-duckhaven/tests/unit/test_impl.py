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


def test_get_column_schema_from_query_wraps_describe_in_a_select():
    # dbt-duckdb emits a bare `DESCRIBE (<sql>)`. DuckDB reports DESCRIBE as a SELECT, so the
    # agent materializes it via `COPY (<sql>) TO ...`, and `COPY (DESCRIBE ...)` is a parser
    # error. Selecting from the DESCRIBE makes it a real SELECT that wraps cleanly.
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
