"""Destination-managed staging via presigned URLs: presign the file, HTTP PUT the local
Parquet to the ``put_url``, and return the ``get_url`` for the load command."""

from unittest.mock import MagicMock

import dlt_duckhaven._staging as staging

from duckhaven_sql_connector import StagedFile, StagingFiles


def _staged(name="orders.abc.0.parquet"):
    return StagingFiles(
        files=[
            StagedFile(
                name=name,
                key=f"s3://bucket/sales/_staging/sess/{name}",
                put_url=f"http://minio.ext:9000/bucket/sales/_staging/sess/{name}?sig=put",
                get_url=f"http://minio:9000/bucket/sales/_staging/sess/{name}?sig=get",
            )
        ],
        expires_at="2026-07-18T00:15:00Z",
    )


def test_stage_file_puts_and_returns_get_url(monkeypatch):
    conn = MagicMock()
    conn.stage_files.return_value = _staged()
    put = MagicMock()
    monkeypatch.setattr(staging, "_put_file", put)

    get_url = staging.stage_file(conn, "/tmp/orders.abc.0.parquet")

    # Presigns by bare file name; uploads to the client-facing put_url; returns the
    # agent-facing get_url for the load.
    conn.stage_files.assert_called_once_with(["orders.abc.0.parquet"])
    put.assert_called_once_with(
        "/tmp/orders.abc.0.parquet",
        "http://minio.ext:9000/bucket/sales/_staging/sess/orders.abc.0.parquet?sig=put",
    )
    assert get_url == "http://minio:9000/bucket/sales/_staging/sess/orders.abc.0.parquet?sig=get"


def test_put_file_streams_body_and_checks_status(monkeypatch, tmp_path):
    local = tmp_path / "orders.parquet"
    local.write_bytes(b"parquet-bytes")
    response = MagicMock()
    put = MagicMock(return_value=response)
    monkeypatch.setattr(staging.requests, "put", put)

    staging._put_file(str(local), "http://minio.ext:9000/put?sig=put")

    assert put.call_args.args[0] == "http://minio.ext:9000/put?sig=put"
    # Body is streamed from the open file handle, not read into memory.
    assert hasattr(put.call_args.kwargs["data"], "read")
    response.raise_for_status.assert_called_once()
