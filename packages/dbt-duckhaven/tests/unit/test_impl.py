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
