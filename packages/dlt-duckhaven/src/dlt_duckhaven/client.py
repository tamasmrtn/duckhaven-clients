"""``DuckHavenJobClient`` — the dlt job client that drives loads through the session.

Built on ``InsertValuesJobClient`` (a staging-dataset-aware SQL job client that also
provides the ``insert_values`` fallback) plus ``SupportsStagingDestination`` so an explicit
filesystem staging destination can feed it. Schema/DDL, state sync, and the load-package
machinery are inherited; this class wires the DuckHaven SQL client, the staged copy job,
and the Iceberg type mapping.
"""

from __future__ import annotations

from collections.abc import Iterable

from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.client import LoadJob, PreparedTableSchema, SupportsStagingDestination
from dlt.common.schema import Schema
from dlt.common.schema.typing import TColumnType, TTableSchemaColumns
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.destinations.insert_job_client import InsertValuesJobClient

from dlt_duckhaven.configuration import DuckHavenClientConfiguration
from dlt_duckhaven.load_jobs import DuckHavenCopyJob
from dlt_duckhaven.sql_client import DuckHavenSqlClient


def _parse_duckdb_type(type_text: str) -> tuple[str, int | None, int | None]:
    """Split a DuckDB DESCRIBE type into (base type, precision, scale).

    e.g. ``DECIMAL(10,2)`` -> ``("DECIMAL", 10, 2)``; ``VARCHAR`` -> ``("VARCHAR", None,
    None)``; ``TIMESTAMP WITH TIME ZONE`` -> ``("TIMESTAMP WITH TIME ZONE", None, None)``.
    """
    base = type_text.split("(", 1)[0].strip()
    precision = scale = None
    if "(" in type_text and ")" in type_text:
        inner = type_text[type_text.index("(") + 1 : type_text.rindex(")")]
        parts = [p.strip() for p in inner.split(",")]
        if parts and parts[0].isdigit():
            precision = int(parts[0])
        if len(parts) > 1 and parts[1].isdigit():
            scale = int(parts[1])
    return base, precision, scale


class DuckHavenJobClient(InsertValuesJobClient, SupportsStagingDestination):
    def __init__(
        self,
        schema: Schema,
        config: DuckHavenClientConfiguration,
        capabilities: DestinationCapabilitiesContext,
    ) -> None:
        dataset_name, staging_dataset_name = InsertValuesJobClient.create_dataset_names(
            schema, config
        )
        sql_client = DuckHavenSqlClient(dataset_name, staging_dataset_name, config, capabilities)
        super().__init__(schema, config, sql_client)
        self.config: DuckHavenClientConfiguration = config
        self.sql_client: DuckHavenSqlClient = sql_client
        self.type_mapper = self.capabilities.get_type_mapper()

    def create_load_job(
        self, table: PreparedTableSchema, file_path: str, load_id: str, restore: bool = False
    ) -> LoadJob:
        # Base handles .sql jobs and the insert_values fallback; else stage + COPY.
        job = super().create_load_job(table, file_path, load_id, restore)
        if not job:
            job = DuckHavenCopyJob(file_path)
        return job

    def _from_db_type(self, db_type: str, precision: int | None, scale: int | None) -> TColumnType:
        return self.type_mapper.from_destination_type(db_type, precision, scale)

    def get_storage_tables(
        self, table_names: Iterable[str]
    ) -> Iterable[tuple[str, TTableSchemaColumns]]:
        """Introspect existing table columns via ``DESCRIBE`` instead of INFORMATION_SCHEMA.

        DuckDB's ``information_schema.columns`` is unreliable for attached Iceberg (Polaris)
        catalogs, so schema evolution (diffing new columns against the live table) reads
        columns with ``DESCRIBE <catalog.schema.table>``. A missing table yields no columns,
        which dlt reads as "table does not exist" and creates it.
        """
        for table_name in table_names:
            columns: TTableSchemaColumns = {}
            qualified = self.sql_client.make_qualified_table_name(table_name)
            try:
                # DESCRIBE wrapped in a SELECT. A bare DESCRIBE also works on a current
                # server, but the wrapped form is what to keep: it is projectable, it is
                # what DuckHaven's grant check recognizes as metadata-only on a scoped
                # catalog, and it is the one spelling that works on every server
                # generation — older agents materialize results as `COPY (<sql>) TO`,
                # which a bare DESCRIBE is not valid inside.
                rows = self.sql_client.execute_sql(f"SELECT * FROM (DESCRIBE {qualified})")
            except DatabaseUndefinedRelation:
                yield table_name, {}
                continue
            for row in rows or []:
                # DuckDB DESCRIBE: (column_name, column_type, null, key, default, extra).
                col_name, col_type = row[0], row[1]
                nullable = len(row) < 3 or str(row[2]).strip().upper() != "NO"
                base, precision, scale = _parse_duckdb_type(col_type)
                columns[col_name] = {
                    "name": col_name,
                    "nullable": nullable,
                    **self._from_db_type(base, precision, scale),
                }
            yield table_name, columns

    def should_truncate_table_before_load_on_staging_destination(self, table_name: str) -> bool:
        return self.config.truncate_tables_on_staging_destination_before_load
