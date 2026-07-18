"""Destination-managed staging: vend a scoped credential, upload the local file under the
session's staging prefix, and build the fsspec filesystem per storage provider."""

from unittest.mock import MagicMock

import dlt_duckhaven._staging as staging
import fsspec
import pytest

from duckhaven_sql_connector import StagingCredentials


def test_stage_file_uploads_and_returns_remote_uri(monkeypatch):
    conn = MagicMock()
    conn.vend_staging_credentials.return_value = StagingCredentials(
        uri="s3://bucket/sales/_staging/sess",
        credentials={"provider": "s3", "access_key_id": "AK", "secret_access_key": "SK"},
    )
    fs = MagicMock()
    monkeypatch.setattr(staging, "_open_filesystem", MagicMock(return_value=fs))

    uri = staging.stage_file(conn, "/tmp/orders.abc.0.parquet", "load1")

    assert uri == "s3://bucket/sales/_staging/sess/load1/orders.abc.0.parquet"
    fs.put_file.assert_called_once_with("/tmp/orders.abc.0.parquet", uri)


def test_open_filesystem_s3_passes_endpoint_and_keys(monkeypatch):
    captured = {}

    def fake_filesystem(protocol, **kwargs):
        captured["protocol"] = protocol
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(fsspec, "filesystem", fake_filesystem)
    staging._open_filesystem(
        {
            "provider": "s3",
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "session_token": "TOK",
            "endpoint_url": "http://minio:9000",
            "region": "us-east-1",
        }
    )
    assert captured["protocol"] == "s3"
    kwargs = captured["kwargs"]
    assert kwargs["key"] == "AK"
    assert kwargs["secret"] == "SK"
    assert kwargs["token"] == "TOK"
    assert kwargs["client_kwargs"] == {
        "endpoint_url": "http://minio:9000",
        "region_name": "us-east-1",
    }


def test_open_filesystem_azure(monkeypatch):
    captured = {}

    def fake_filesystem(protocol, **kwargs):
        captured["protocol"] = protocol
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(fsspec, "filesystem", fake_filesystem)
    staging._open_filesystem({"provider": "azure", "account_name": "acct", "sas_token": "sas"})
    assert captured["protocol"] == "az"
    assert captured["kwargs"]["account_name"] == "acct"
    assert captured["kwargs"]["sas_token"] == "sas"


def test_open_filesystem_defaults_to_s3(monkeypatch):
    # Missing provider defaults to s3 (the bundled MinIO/external S3 backend).
    captured = {}
    monkeypatch.setattr(
        fsspec, "filesystem", lambda protocol, **kw: captured.setdefault("protocol", protocol)
    )
    staging._open_filesystem({"access_key_id": "AK"})
    assert captured["protocol"] == "s3"


def test_open_filesystem_unsupported_provider():
    with pytest.raises(ValueError):
        staging._open_filesystem({"provider": "gcs"})
