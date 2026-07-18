"""Load jobs for the DuckHaven destination.

``DuckHavenCopyJob`` stages a load's Parquet to the workspace object storage and issues an
``INSERT INTO … SELECT * FROM read_parquet(…)`` through the session; the agent runs it and
writes the Iceberg table. Bulk data goes through storage; the load command goes through the
API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlt.common.destination.client import HasFollowupJobs, RunnableLoadJob
from dlt.destinations.job_impl import ReferenceFollowupJobRequest

from dlt_duckhaven import _staging

if TYPE_CHECKING:
    from dlt_duckhaven.client import DuckHavenJobClient

# Source file extension -> (DuckDB table function, extra options).
_READERS = {
    "parquet": ("read_parquet", ", union_by_name=true"),
    "jsonl": ("read_json", ""),
    "json": ("read_json", ""),
}


def _reader_for(uri: str) -> tuple[str, str]:
    ext = uri.rsplit(".", 1)[-1].lower()
    reader = _READERS.get(ext)
    if reader is None:
        raise ValueError(f"unsupported staging file format: {ext!r} ({uri})")
    return reader


class DuckHavenCopyJob(RunnableLoadJob, HasFollowupJobs):
    def __init__(self, file_path: str) -> None:
        super().__init__(file_path)
        self._job_client: DuckHavenJobClient = None

    def run(self) -> None:
        self._sql_client = self._job_client.sql_client
        qualified_table_name = self._sql_client.make_qualified_table_name(self.load_table_name)

        if ReferenceFollowupJobRequest.is_reference_job(self._file_path):
            # An explicit filesystem staging destination already uploaded the file.
            remote_uri = ReferenceFollowupJobRequest.resolve_reference(self._file_path)
        else:
            # Destination-managed staging: upload the local file with a vended credential.
            remote_uri = _staging.stage_file(
                self._sql_client.native_connection, self._file_path, self._load_id
            )

        source_format, options = _reader_for(remote_uri)
        # The agent reads the staged prefix with its own catalog-scoped access — no
        # credential travels in the load SQL. BY NAME/union_by_name tolerates column
        # evolution across files.
        self._sql_client.execute_sql(
            f"INSERT INTO {qualified_table_name} BY NAME"
            f" SELECT * FROM {source_format}('{remote_uri}'{options})"
        )
