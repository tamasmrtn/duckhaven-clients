"""End-to-end: a real dbt project seed → run → test against a live DuckHaven.

Beyond the conformance suites, this proves the whole loop on a hand-written project and
verifies rows actually land in the Iceberg tables and that an incremental re-run appends
rather than duplicates.
"""

import pytest
from dbt.tests.util import run_dbt

_SEED = """id,name
1,alice
2,bob
"""

# A table (not a view): DuckDB's Iceberg REST catalog does not implement CREATE VIEW.
_TABLE = """
{{ config(materialized='table') }}
select * from {{ ref('people') }}
"""

_INCREMENTAL = """
{{ config(materialized='incremental') }}
select * from {{ ref('people') }}
{% if is_incremental() %}
where id > (select max(id) from {{ this }})
{% endif %}
"""

_SCHEMA_YML = """
version: 2
models:
  - name: people_tbl
    columns:
      - name: id
        data_tests: [not_null, unique]
"""

# The `merge` strategy: the second run must update alice in place, not append a duplicate.
_MERGE = """
{{ config(materialized='incremental', incremental_strategy='merge', unique_key='id') }}
{% if is_incremental() %}
select 1 as id, 'alice-updated' as name
{% else %}
select * from {{ ref('people') }}
{% endif %}
"""

_SNAPSHOT = """
{% snapshot people_snap %}
{{ config(target_schema=schema, unique_key='id', strategy='check', check_cols=['name']) }}
select * from {{ ref('people_tbl') }}
{% endsnapshot %}
"""


class TestDuckHavenEndToEnd:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"people.csv": _SEED}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "people_tbl.sql": _TABLE,
            "people_incr.sql": _INCREMENTAL,
            "people_merge.sql": _MERGE,
            "schema.yml": _SCHEMA_YML,
        }

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"people_snap.sql": _SNAPSHOT}

    def test_seed_reruns_over_an_existing_seed(self, project):
        """`dbt seed` twice over the same seed, without `--full-refresh`.

        dbt's seed reset emits `TRUNCATE TABLE`, which the statement policy used to
        reject, making this the documented "use --full-refresh" caveat. The policy now
        admits it, so the plain re-run must work. Runs first so it owns a fresh seed,
        before the shared-project tests below build on it.
        """
        assert len(run_dbt(["seed"])) == 1
        assert len(run_dbt(["seed"])) == 1
        count = project.run_sql("select count(*) from {schema}.people", fetch="one")
        assert count[0] == 2

    def test_seed_run_test(self, project):
        assert len(run_dbt(["seed"])) == 1
        assert len(run_dbt(["run"])) == 3
        run_dbt(["test"])

        tbl_count = project.run_sql("select count(*) from {schema}.people_tbl", fetch="one")
        assert tbl_count[0] == 2

        # A second run rebuilds the `table` model, which exercises the rename path:
        # dbt renames the existing relation to a backup (ALTER ... RENAME TO), renames the
        # freshly built temp relation into place, then drops the backup. This is the
        # end-to-end verification that ALTER ... RENAME TO and drop_relation (without
        # CASCADE) both work on the Iceberg REST catalog. It also re-runs the incremental,
        # which must not duplicate rows.
        run_dbt(["run"])

        # people_tbl survived the rename/drop cycle with its rows intact.
        tbl_count = project.run_sql("select count(*) from {schema}.people_tbl", fetch="one")
        assert tbl_count[0] == 2

        incr_count = project.run_sql("select count(*) from {schema}.people_incr", fetch="one")
        assert incr_count[0] == 2

        # The merge model's second run updates id=1 in place rather than appending it.
        merge_rows = project.run_sql(
            "select id, name from {schema}.people_merge order by id", fetch="all"
        )
        assert merge_rows == [(1, "alice-updated"), (2, "bob")]

        # No stale backup/temp relations left behind. This also catches the snapshot staging
        # table regressing from a session-local temp table to a real Iceberg one.
        leftovers = project.run_sql(
            "select count(*) from information_schema.tables "
            "where table_schema = '{schema}' "
            "and (table_name like '%__dbt_backup' or table_name like '%__dbt_tmp%')",
            fetch="one",
        )
        assert leftovers[0] == 0

    def test_incremental_full_refresh_rebuilds(self, project):
        """`dbt run --full-refresh` over an *existing* incremental model.

        dbt-duckdb's incremental materialization rebuilds via a rename swap (rename target to
        backup, rename the intermediate into place). On an Iceberg REST catalog a rename moves
        the catalog entry but not the storage location — the same failure the `table`
        materialization is overridden to avoid. Nothing exercised this path before, so it is
        unproven rather than known-broken; this pins whichever way it actually behaves.

        Relies on test_seed_run_test having built the project — the tests in this class share
        one schema and run in definition order.
        """
        run_dbt(["run", "--select", "people_incr", "--full-refresh"])

        count = project.run_sql("select count(*) from {schema}.people_incr", fetch="one")
        assert count[0] == 2

    def test_snapshot_captures_changes(self, project):
        # Builds on the project from test_seed_run_test (shared class schema, definition
        # order).
        assert len(run_dbt(["snapshot"])) == 1
        current = project.run_sql(
            "select count(*) from {schema}.people_snap where dbt_valid_to is null", fetch="one"
        )
        assert current[0] == 2

        # Change a tracked column at the source, then re-snapshot: the old row must be closed
        # out (dbt_valid_to set) and a new current row inserted. This is what proves dbt-core's
        # MERGE-based snapshot_merge_sql actually runs against Iceberg.
        project.run_sql("update {schema}.people_tbl set name = 'alice2' where id = 1")
        run_dbt(["snapshot"])

        current = project.run_sql(
            "select count(*) from {schema}.people_snap where dbt_valid_to is null", fetch="one"
        )
        assert current[0] == 2
        closed = project.run_sql(
            "select count(*) from {schema}.people_snap where dbt_valid_to is not null", fetch="one"
        )
        assert closed[0] == 1
