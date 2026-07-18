"""``Connection.vend_staging_credentials`` — the per-load staging-credential vend."""

import httpx
import pytest
import respx

from duckhaven_sql_connector import StagingCredentials
from duckhaven_sql_connector.dbapi import OperationalError, ProgrammingError

from .dh_support import SESSION_URL, open_conn

STAGING_URL = f"{SESSION_URL}/staging-credentials"


@respx.mock
def test_vend_returns_uri_and_credentials():
    route = respx.post(STAGING_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "uri": "s3://bucket/sales/_staging/abc/load-1",
                "credentials": {
                    "provider": "s3",
                    "access_key_id": "AK",
                    "secret_access_key": "SK",
                    "endpoint_url": "http://minio:9000",
                },
                "expires_at": "2026-07-18T00:15:00Z",
            },
        )
    )
    conn = open_conn()
    creds = conn.vend_staging_credentials()

    assert route.called
    assert isinstance(creds, StagingCredentials)
    assert creds.uri == "s3://bucket/sales/_staging/abc/load-1"
    assert creds.credentials["provider"] == "s3"
    assert creds.credentials["endpoint_url"] == "http://minio:9000"
    assert creds.expires_at == "2026-07-18T00:15:00Z"


@respx.mock
def test_vend_defaults_missing_optional_fields():
    respx.post(STAGING_URL).mock(
        return_value=httpx.Response(200, json={"uri": "s3://bucket/prefix"})
    )
    creds = open_conn().vend_staging_credentials()
    assert creds.credentials == {}
    assert creds.expires_at is None


@respx.mock
def test_reaped_session_marks_connection_dead():
    respx.post(STAGING_URL).mock(
        return_value=httpx.Response(
            409, json={"detail": {"error": "session_not_open", "detail": "reaped"}}
        )
    )
    conn = open_conn()
    with pytest.raises(OperationalError):
        conn.vend_staging_credentials()
    # Session is dead: subsequent use fails fast without another request.
    with pytest.raises(OperationalError):
        conn.vend_staging_credentials()


@respx.mock
def test_vend_on_closed_connection_raises():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    with pytest.raises(ProgrammingError):
        conn.vend_staging_credentials()
