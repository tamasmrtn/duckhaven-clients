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

_VIEW = "select * from {{ ref('people') }}"

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
  - name: people_view
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
            "people_view.sql": _VIEW,
            "people_incr.sql": _INCREMENTAL,
            "schema.yml": _SCHEMA_YML,
        }

    def test_seed_run_test(self, project):
        assert len(run_dbt(["seed"])) == 1
        assert len(run_dbt(["run"])) == 2
        run_dbt(["test"])

        view_count = project.run_sql("select count(*) from {schema}.people_view", fetch="one")
        assert view_count[0] == 2

        # A second incremental run with no new source rows must not duplicate.
        run_dbt(["run"])
        incr_count = project.run_sql("select count(*) from {schema}.people_incr", fetch="one")
        assert incr_count[0] == 2
