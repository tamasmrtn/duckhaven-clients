"""``Connection.stage_files`` — presign PUT/GET URLs for a session's stage."""

import json

import httpx
import pytest
import respx

from duckhaven_sql_connector import StagingFiles
from duckhaven_sql_connector.dbapi import OperationalError, ProgrammingError

from .dh_support import SESSION_URL, open_conn

STAGING_URL = f"{SESSION_URL}/staging-files"


def _staged_response(*names: str) -> dict:
    return {
        "files": [
            {
                "name": name,
                "key": f"s3://bucket/sales/_staging/sess/{name}",
                "put_url": f"http://minio.ext:9000/bucket/sales/_staging/sess/{name}?sig=put",
                "get_url": f"http://minio:9000/bucket/sales/_staging/sess/{name}?sig=get",
            }
            for name in names
        ],
        "expires_at": "2026-07-18T00:15:00Z",
    }


@respx.mock
def test_stage_files_returns_presigned_urls():
    route = respx.post(STAGING_URL).mock(
        return_value=httpx.Response(200, json=_staged_response("orders.abc.0.parquet"))
    )
    conn = open_conn()
    staged = conn.stage_files(["orders.abc.0.parquet"])

    assert route.called
    # The request body carries the file names.
    assert json.loads(route.calls.last.request.read()) == {"files": ["orders.abc.0.parquet"]}
    assert isinstance(staged, StagingFiles)
    assert staged.expires_at == "2026-07-18T00:15:00Z"
    (file,) = staged.files
    assert file.name == "orders.abc.0.parquet"
    assert file.key == "s3://bucket/sales/_staging/sess/orders.abc.0.parquet"
    assert "sig=put" in file.put_url  # client-facing (external) endpoint
    assert "sig=get" in file.get_url  # agent-facing (internal) endpoint


@respx.mock
def test_stage_files_multiple():
    respx.post(STAGING_URL).mock(
        return_value=httpx.Response(200, json=_staged_response("a.parquet", "b.parquet"))
    )
    staged = open_conn().stage_files(["a.parquet", "b.parquet"])
    assert [f.name for f in staged.files] == ["a.parquet", "b.parquet"]


@respx.mock
def test_reaped_session_marks_connection_dead():
    respx.post(STAGING_URL).mock(
        return_value=httpx.Response(
            409, json={"detail": {"error": "session_not_open", "detail": "reaped"}}
        )
    )
    conn = open_conn()
    with pytest.raises(OperationalError):
        conn.stage_files(["orders.parquet"])
    with pytest.raises(OperationalError):
        conn.stage_files(["orders.parquet"])


@respx.mock
def test_stage_files_on_closed_connection_raises():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    with pytest.raises(ProgrammingError):
        conn.stage_files(["orders.parquet"])
