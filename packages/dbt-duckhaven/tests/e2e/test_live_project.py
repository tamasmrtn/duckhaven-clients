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


class TestDuckHavenEndToEnd:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"people.csv": _SEED}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "people_tbl.sql": _TABLE,
            "people_incr.sql": _INCREMENTAL,
            "schema.yml": _SCHEMA_YML,
        }

    def test_seed_run_test(self, project):
        assert len(run_dbt(["seed"])) == 1
        assert len(run_dbt(["run"])) == 2
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

        # No stale backup/temp relations left behind by the rename swap.
        leftovers = project.run_sql(
            "select count(*) from information_schema.tables "
            "where table_schema = '{schema}' "
            "and (table_name like '%__dbt_backup' or table_name like '%__dbt_tmp')",
            fetch="one",
        )
        assert leftovers[0] == 0
