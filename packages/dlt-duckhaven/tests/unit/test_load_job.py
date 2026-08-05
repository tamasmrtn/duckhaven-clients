"""DuckHavenCopyJob: stage the local file then INSERT via read_parquet on the presigned
GET URL; use the resolved remote path for an external-staging reference job; never put a
credential in the load SQL."""

from unittest.mock import MagicMock

import dlt_duckhaven.load_jobs as load_jobs
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
