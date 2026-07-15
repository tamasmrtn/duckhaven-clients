"""Adapter-level overrides that correct dbt-duckdb defaults for a remote Iceberg backend."""

from dbt.adapters.base.impl import ConstraintSupport
from dbt.adapters.capability import Capability, Support
from dbt.adapters.duckhaven.impl import DuckHavenAdapter
from dbt_common.contracts.constraints import ConstraintType


def test_constraints_reflect_iceberg_reality():
    # Polaris/Iceberg enforces NOT NULL on write but not unique/pk/fk; check is unsupported.
    cs = DuckHavenAdapter.CONSTRAINT_SUPPORT
    assert cs[ConstraintType.not_null] == ConstraintSupport.ENFORCED
    assert cs[ConstraintType.check] == ConstraintSupport.NOT_SUPPORTED
    assert cs[ConstraintType.unique] == ConstraintSupport.NOT_ENFORCED
    assert cs[ConstraintType.primary_key] == ConstraintSupport.NOT_ENFORCED
    assert cs[ConstraintType.foreign_key] == ConstraintSupport.NOT_ENFORCED


def test_does_not_advertise_concurrent_microbatch():
    # Inherited dbt-duckdb value is Support.Full; concurrent batches would oversubscribe
    # the agent's admission slots and microbatch is not a v1 materialization.
    support = DuckHavenAdapter._capabilities[Capability.MicrobatchConcurrency].support
    assert support == Support.NotImplemented


def test_valid_incremental_strategies_are_v1_only():
    strategies = DuckHavenAdapter.valid_incremental_strategies(DuckHavenAdapter)
    assert set(strategies) == {"append", "delete+insert"}
