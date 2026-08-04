"""Load jobs for the DuckHaven destination.

``DuckHavenCopyJob`` stages a load's Parquet to the workspace object storage and issues an
``INSERT INTO … SELECT * FROM read_parquet(…)`` through the session; the agent runs it and
writes the Iceberg table. Bulk data goes through storage; the load command goes through the
API.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

from dlt.common.destination.client import HasFollowupJobs, RunnableLoadJob
from dlt.destinations.exceptions import DatabaseTransientException
from dlt.destinations.job_impl import ReferenceFollowupJobRequest

from dlt_duckhaven import _staging, _telemetry
from dlt_duckhaven.sql_client import is_commit_conflict

if TYPE_CHECKING:
    from dlt_duckhaven.client import DuckHavenJobClient

# Source file extension -> (DuckDB table function, extra options).
_READERS = {
    "parquet": ("read_parquet", ", union_by_name=true"),
    "jsonl": ("read_json", ""),
    "json": ("read_json", ""),
}


# dlt runs load jobs in parallel (20 workers by default), so two jobs for the same table --
# one resource whose data spans several Parquet files is enough -- routinely commit to the
# same Iceberg table at once and Polaris rejects the loser. Retrying here rather than only
# letting dlt retry the job keeps the staged Parquet upload out of the retry: dlt would
# re-run `run()` from the top, re-uploading the file each time.
_COMMIT_CONFLICT_ATTEMPTS = 5
_BACKOFF_BASE = 0.2
_BACKOFF_MAX = 5.0


def _backoff_delay(attempt: int) -> float:
    """Capped exponential backoff with full jitter for retry ``attempt`` (0-based).

    Jitter matters more than the curve here: the racing jobs were started together, so a
    fixed delay would just line them up to collide again.
    """
    return random.uniform(0, min(_BACKOFF_MAX, _BACKOFF_BASE * (2**attempt)))


def _reader_for(uri: str) -> tuple[str, str]:
    # Strip any presigned-URL query string before reading the file extension.
    path = uri.split("?", 1)[0]
    ext = path.rsplit(".", 1)[-1].lower()
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

        with _telemetry.load_span(
            "dlt_duckhaven.load_job",
            {"dlt.table": self.load_table_name, "dlt.load_id": self._load_id},
        ):
            if ReferenceFollowupJobRequest.is_reference_job(self._file_path):
                # An explicit filesystem staging destination already uploaded the file.
                remote_uri = ReferenceFollowupJobRequest.resolve_reference(self._file_path)
            else:
                # Destination-managed staging: upload the local file, then read its
                # presigned GET URL. The agent fetches it over httpfs — no credential
                # travels in the load SQL.
                remote_uri = _staging.stage_file(
                    self._sql_client.native_connection, self._file_path
                )

            source_format, options = _reader_for(remote_uri)
            # BY NAME/union_by_name tolerates column evolution across files.
            self._insert_with_commit_retry(
                f"INSERT INTO {qualified_table_name} BY NAME"
                f" SELECT * FROM {source_format}('{remote_uri}'{options})"
            )

    def _insert_with_commit_retry(self, sql: str) -> None:
        """Run the load statement, retrying a lost Iceberg commit race.

        Only commit conflicts are retried: they are resolved by re-reading the table's
        refreshed metadata, which is what re-running the statement does. Every other
        transient failure (a reaped session, an agent that went away) leaves this
        connection unusable, so it propagates to dlt, which retries the whole job against
        a fresh one.
        """
        for attempt in range(_COMMIT_CONFLICT_ATTEMPTS):
            try:
                self._sql_client.execute_sql(sql)
                return
            except DatabaseTransientException as ex:
                if not is_commit_conflict(ex) or attempt == _COMMIT_CONFLICT_ATTEMPTS - 1:
                    # Still transient, so dlt's own bounded retry stays the outer net.
                    raise
                time.sleep(_backoff_delay(attempt))
