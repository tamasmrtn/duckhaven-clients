"""Incremental-strategy conformance beyond the basic suite.

Covers `append`, `delete+insert`, `merge`, and `microbatch`. The on-schema-change
(ALTER-driven) flow is still deferred, so that suite is intentionally not subclassed here.
"""

import pytest
from dbt.tests.adapter.basic.test_incremental import BaseIncrementalNotSchemaChange
from dbt.tests.adapter.incremental.test_incremental_merge_exclude_columns import (
    BaseMergeExcludeColumns,
)
from dbt.tests.adapter.incremental.test_incremental_microbatch import BaseMicrobatch

# dbt-tests-adapter's stock microbatch model sets `unique_key='id'`, but dbt-duckdb's
# duckdb__get_incremental_microbatch_sql raises a compiler error when unique_key is set
# ("use incremental_strategy='merge'") because it renders microbatch as DELETE + INSERT on
# event_time rather than a merge. That is an upstream disagreement between dbt-tests-adapter
# and dbt-duckdb, not a DuckHaven limitation, so drop unique_key and keep the rest.
_MICROBATCH_MODEL = """
{{ config(
    materialized='incremental',
    incremental_strategy='microbatch',
    event_time='event_time',
    batch_size='day',
    begin=modules.datetime.datetime(2020, 1, 1, 0, 0, 0),
) }}
select * from {{ ref('input_model') }}
"""

# The stock fixtures write `TIMESTAMP '2020-01-01 00:00:00-0'`. DuckDB 1.5 rejects an offset
# in a TIMESTAMP literal outright ("has a timestamp that is not UTC" — it wants TIMESTAMPTZ),
# even with ICU loaded, so the suite's input model cannot build. Nothing to do with DuckHaven
# or microbatch; use plain UTC literals. The batch predicate still compares correctly: dbt
# renders its bounds as '…+00:00' strings and DuckDB casts those against a naive TIMESTAMP.
_INPUT_MODEL = """
{{ config(materialized='table', event_time='event_time') }}
select 1 as id, TIMESTAMP '2020-01-01 00:00:00' as event_time
union all
select 2 as id, TIMESTAMP '2020-01-02 00:00:00' as event_time
union all
select 3 as id, TIMESTAMP '2020-01-03 00:00:00' as event_time
"""


class TestIncrementalNotSchemaChangeDuckHaven(BaseIncrementalNotSchemaChange):
    pass


class TestMergeExcludeColumnsDuckHaven(BaseMergeExcludeColumns):
    """The `merge` strategy against Iceberg, including merge_exclude_columns.

    The suite's `--full-refresh` run builds the model in a fresh schema where it does not
    exist yet, so it takes the create_table_as path rather than the rename swap.
    """


class TestMicrobatchDuckHaven(BaseMicrobatch):
    @pytest.fixture(scope="class")
    def microbatch_model_sql(self) -> str:
        return _MICROBATCH_MODEL

    @pytest.fixture(scope="class")
    def input_model_sql(self) -> str:
        return _INPUT_MODEL

    @pytest.fixture(scope="class")
    def insert_two_rows_sql(self, project) -> str:
        relation = project.adapter.Relation.create(
            database=project.database, schema=project.test_schema
        )
        return (
            f"insert into {relation}.input_model (id, event_time) values "
            f"(4, TIMESTAMP '2020-01-04 00:00:00'), (5, TIMESTAMP '2020-01-05 00:00:00')"
        )
