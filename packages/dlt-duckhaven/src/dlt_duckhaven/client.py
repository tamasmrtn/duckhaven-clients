"""``DuckHavenJobClient`` — the dlt job client that drives loads through the session.

Built on ``InsertValuesJobClient`` (a staging-dataset-aware SQL job client that also
provides the ``insert_values`` fallback) plus ``SupportsStagingDestination`` so an explicit
filesystem staging destination can feed it. Schema/DDL, state sync, and the load-package
machinery are inherited; this class wires the DuckHaven SQL client, the staged copy job,
and the Iceberg type mapping.
"""

from __future__ import annotations

from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.client import LoadJob, PreparedTableSchema, SupportsStagingDestination
from dlt.common.schema import Schema
from dlt.common.schema.typing import TColumnType
from dlt.destinations.insert_job_client import InsertValuesJobClient

from dlt_duckhaven.configuration import DuckHavenClientConfiguration
from dlt_duckhaven.load_jobs import DuckHavenCopyJob
from dlt_duckhaven.sql_client import DuckHavenSqlClient


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

    def should_truncate_table_before_load_on_staging_destination(self, table_name: str) -> bool:
        return self.config.truncate_tables_on_staging_destination_before_load
