"""Snapshot conformance against a live DuckHaven.

Both strategies are covered: `timestamp` (BaseSimpleSnapshot, BaseSnapshotTimestamp) and
`check` (BaseSnapshotCheck, BaseSnapshotCheckCols). These suites are view-free — their
fixtures build `fact` as a table and their helpers use drop/create table as, update, delete,
and insert, all of which the Iceberg REST catalog supports.

These are the suites that actually prove the snapshot overrides: that dbt-core's MERGE-based
snapshot_merge_sql runs on Iceberg, and that the staging table works as a session-local temp
table. BaseSimpleSnapshot.test_new_column_captured_by_snapshot additionally exercises
ALTER TABLE ADD COLUMN, which duckdb-iceberg documents from 1.5.3 but which nothing else in
this adapter's suites has run.
"""

from dbt.tests.adapter.basic.test_snapshot_check_cols import BaseSnapshotCheckCols
from dbt.tests.adapter.basic.test_snapshot_timestamp import BaseSnapshotTimestamp
from dbt.tests.adapter.simple_snapshot.test_snapshot import BaseSimpleSnapshot, BaseSnapshotCheck


class TestSimpleSnapshotDuckHaven(BaseSimpleSnapshot):
    pass


class TestSnapshotCheckDuckHaven(BaseSnapshotCheck):
    pass


class TestSnapshotTimestampDuckHaven(BaseSnapshotTimestamp):
    pass


class TestSnapshotCheckColsDuckHaven(BaseSnapshotCheckCols):
    pass
