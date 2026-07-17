"""dbt-tests-adapter conformance — the v1 basic suite.

Each class runs dbt-labs' standard adapter test against a live DuckHaven, so we inherit
coverage of table/seed/incremental materializations, singular tests, and connection
validation.

Only the suites whose models DuckHaven can build are subclassed. dbt's *default*
materialization is `view`, and DuckDB's Iceberg REST catalog has no CREATE VIEW (views are
unsupported — see README), so every standard suite that builds a default-materialized
model fails with "Not implemented Error: Create View". Those are deliberately not
subclassed:

  * BaseSimpleMaterializations — builds a `view` model directly.
  * BaseEphemeral / BaseGenericTests / BaseSingularTestsEphemeral / BaseAdapterMethod —
    their shared fixtures build default-materialized (view) models.
  * BaseDocsGenerate / BaseDocsGenReferences — additionally need a duckhaven__get_catalog
    override (native duckdb_columns() is broken for Iceberg REST catalogs).
  * Snapshot suites — snapshots are not supported in v1.

Table + incremental coverage comes from BaseTableMaterialization and BaseIncremental; the
DuckHaven-specific table rebuild (drop-and-recreate, no `__dbt_tmp` rename) is exercised
end-to-end by tests/e2e/test_live_project.py.
"""

from dbt.tests.adapter.basic.test_empty import BaseEmpty
from dbt.tests.adapter.basic.test_incremental import BaseIncremental
from dbt.tests.adapter.basic.test_singular_tests import BaseSingularTests
from dbt.tests.adapter.basic.test_table_materialization import BaseTableMaterialization
from dbt.tests.adapter.basic.test_validate_connection import BaseValidateConnection


class TestEmptyDuckHaven(BaseEmpty):
    pass


class TestSingularTestsDuckHaven(BaseSingularTests):
    pass


class TestIncrementalDuckHaven(BaseIncremental):
    pass


class TestTableMaterializationDuckHaven(BaseTableMaterialization):
    pass


class TestValidateConnectionDuckHaven(BaseValidateConnection):
    pass
