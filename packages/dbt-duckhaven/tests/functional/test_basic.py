"""dbt-tests-adapter conformance — the v1 basic suite.

Each class runs dbt-labs' standard adapter test against a live DuckHaven, so we inherit
coverage of table/view/seed/incremental/ephemeral materializations, singular and generic
tests, adapter-method introspection (which exercises our DESCRIBE-based
get_columns_in_relation), and connection validation.

Deferred (not subclassed) in v1:
  * BaseDocsGenerate / BaseDocsGenReferences — need a duckhaven__get_catalog override
    (native duckdb_columns() is broken for Iceberg REST catalogs).
  * Snapshot suites — snapshots are not supported in v1.
"""

from dbt.tests.adapter.basic.test_adapter_methods import BaseAdapterMethod
from dbt.tests.adapter.basic.test_base import BaseSimpleMaterializations
from dbt.tests.adapter.basic.test_empty import BaseEmpty
from dbt.tests.adapter.basic.test_ephemeral import BaseEphemeral
from dbt.tests.adapter.basic.test_generic_tests import BaseGenericTests
from dbt.tests.adapter.basic.test_incremental import BaseIncremental
from dbt.tests.adapter.basic.test_singular_tests import BaseSingularTests
from dbt.tests.adapter.basic.test_singular_tests_ephemeral import BaseSingularTestsEphemeral
from dbt.tests.adapter.basic.test_table_materialization import BaseTableMaterialization
from dbt.tests.adapter.basic.test_validate_connection import BaseValidateConnection


class TestSimpleMaterializationsDuckHaven(BaseSimpleMaterializations):
    pass


class TestEmptyDuckHaven(BaseEmpty):
    pass


class TestEphemeralDuckHaven(BaseEphemeral):
    pass


class TestSingularTestsDuckHaven(BaseSingularTests):
    pass


class TestSingularTestsEphemeralDuckHaven(BaseSingularTestsEphemeral):
    pass


class TestIncrementalDuckHaven(BaseIncremental):
    pass


class TestGenericTestsDuckHaven(BaseGenericTests):
    pass


class TestTableMaterializationDuckHaven(BaseTableMaterialization):
    pass


class TestAdapterMethodsDuckHaven(BaseAdapterMethod):
    pass


class TestValidateConnectionDuckHaven(BaseValidateConnection):
    pass
