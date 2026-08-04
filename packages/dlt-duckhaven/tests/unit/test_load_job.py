"""DuckHavenCopyJob: stage the local file then INSERT via read_parquet on the presigned
GET URL; use the resolved remote path for an external-staging reference job; never put a
credential in the load SQL; retry a lost Iceberg commit race without re-staging."""

from unittest.mock import MagicMock

import dlt_duckhaven.load_jobs as load_jobs
import pytest
from dlt.destinations.exceptions import DatabaseTerminalException, DatabaseTransientException
from dlt_duckhaven.load_jobs import DuckHavenCopyJob


def _job(file_path, sql_client):
    job_client = MagicMock()
    job_client.sql_client = sql_client
    job = DuckHavenCopyJob(file_path)
    job._job_client = job_client
    job._load_table = {"name": "orders"}
    job._load_id = "load1"
    return job


def _sql_client():
    client = MagicMock()
    client.make_qualified_table_name.return_value = '"raw"."analytics"."orders"'
    client.native_connection = MagicMock()
    return client


def test_local_file_is_staged_then_inserted_from_get_url(monkeypatch):
    sql_client = _sql_client()
    get_url = "http://minio:9000/bucket/sales/_staging/sess/orders.abc.0.parquet?sig=get"
    stage = MagicMock(return_value=get_url)
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)

    _job("/tmp/orders.abc.0.parquet", sql_client).run()

    stage.assert_called_once_with(sql_client.native_connection, "/tmp/orders.abc.0.parquet")
    sql = sql_client.execute_sql.call_args.args[0]
    assert sql.startswith('INSERT INTO "raw"."analytics"."orders" BY NAME')
    assert f"read_parquet('{get_url}', union_by_name=true)" in sql
    assert "credential" not in sql.lower()


def test_reference_job_uses_resolved_remote_without_staging(monkeypatch):
    sql_client = _sql_client()
    stage = MagicMock()
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)
    monkeypatch.setattr(
        load_jobs.ReferenceFollowupJobRequest, "is_reference_job", staticmethod(lambda p: True)
    )
    monkeypatch.setattr(
        load_jobs.ReferenceFollowupJobRequest,
        "resolve_reference",
        staticmethod(lambda p: "s3://external/orders.parquet"),
    )

    # A real reference file drops the original format from its name (table.file_id.retry
    # .reference); the source format is derived from the resolved remote path instead.
    _job("/tmp/orders.abc.0.reference", sql_client).run()

    stage.assert_not_called()
    sql = sql_client.execute_sql.call_args.args[0]
    assert "read_parquet('s3://external/orders.parquet', union_by_name=true)" in sql


def test_jsonl_uses_read_json(monkeypatch):
    sql_client = _sql_client()
    monkeypatch.setattr(
        load_jobs._staging,
        "stage_file",
        MagicMock(return_value="http://minio:9000/bucket/_staging/sess/orders.abc.0.jsonl?sig=get"),
    )

    _job("/tmp/orders.abc.0.jsonl", sql_client).run()

    sql = sql_client.execute_sql.call_args.args[0]
    assert "read_json('http://minio:9000/bucket/_staging/sess/orders.abc.0.jsonl?sig=get')" in sql


# -- Iceberg commit-conflict retry ------------------------------------------------------

_CONFLICT_SQL = (
    "TransactionContext Error: Failed to commit: Failed to commit Iceberg transaction:"
    " Request to 'http://polaris:8181/api/catalog/v1/raw/transactions/commit' returned a"
    " non-200 status code (Conflict_409). \n message: Cannot commit to table"
    " analytics.orders ... because it has been concurrently modified\n"
    " type: CommitFailedException\n reason: Conflict"
)


def _conflict():
    return DatabaseTransientException(Exception(_CONFLICT_SQL))


@pytest.fixture
def staged(monkeypatch):
    """Stage to a fixed URL and make backoff sleeps instant."""
    monkeypatch.setattr(load_jobs.time, "sleep", MagicMock())
    stage = MagicMock(return_value="http://minio:9000/b/_staging/sess/orders.abc.0.parquet?sig=get")
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)
    return stage


def test_commit_conflict_retries_without_restaging(staged):
    sql_client = _sql_client()
    sql_client.execute_sql.side_effect = [_conflict(), _conflict(), None]

    _job("/tmp/orders.abc.0.parquet", sql_client).run()

    assert sql_client.execute_sql.call_count == 3
    # The retry is around the statement, not the whole job: the Parquet is uploaded once.
    staged.assert_called_once()


def test_commit_conflict_gives_up_after_bounded_attempts(staged):
    sql_client = _sql_client()
    sql_client.execute_sql.side_effect = _conflict()

    with pytest.raises(DatabaseTransientException):
        _job("/tmp/orders.abc.0.parquet", sql_client).run()

    # Still transient on the way out, so dlt's own retry remains the outer bound.
    assert sql_client.execute_sql.call_count == load_jobs._COMMIT_CONFLICT_ATTEMPTS


def test_non_conflict_transient_is_not_retried_here(staged):
    # A reaped session leaves this connection unusable; only dlt can rebuild it.
    sql_client = _sql_client()
    sql_client.execute_sql.side_effect = DatabaseTransientException(Exception("session reaped"))

    with pytest.raises(DatabaseTransientException):
        _job("/tmp/orders.abc.0.parquet", sql_client).run()

    assert sql_client.execute_sql.call_count == 1


def test_terminal_error_is_not_retried(staged):
    sql_client = _sql_client()
    sql_client.execute_sql.side_effect = DatabaseTerminalException(Exception("syntax error"))

    with pytest.raises(DatabaseTerminalException):
        _job("/tmp/orders.abc.0.parquet", sql_client).run()

    assert sql_client.execute_sql.call_count == 1


def test_backoff_is_jittered_and_capped(monkeypatch):
    monkeypatch.setattr(load_jobs.random, "uniform", lambda a, b: b)  # take the ceiling
    delays = [load_jobs._backoff_delay(i) for i in range(6)]
    assert delays == [0.2, 0.4, 0.8, 1.6, 3.2, 5.0]  # doubles, then capped at _BACKOFF_MAX
