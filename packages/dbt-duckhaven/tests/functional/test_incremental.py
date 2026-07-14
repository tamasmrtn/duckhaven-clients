"""Incremental-strategy conformance beyond the basic suite.

v1 supports the `append` and `delete+insert` strategies. `merge` and the on-schema-change
(ALTER-driven) flow are deferred, so those suites are intentionally not subclassed here.
"""

from dbt.tests.adapter.basic.test_incremental import BaseIncrementalNotSchemaChange


class TestIncrementalNotSchemaChangeDuckHaven(BaseIncrementalNotSchemaChange):
    pass
