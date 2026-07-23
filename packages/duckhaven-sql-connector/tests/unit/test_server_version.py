"""``Connection.server_version`` — read ``GET /api/version``."""

import httpx
import pytest
import respx

from duckhaven_sql_connector import ServerVersion
from duckhaven_sql_connector.dbapi import ProgrammingError

from .dh_support import BASE, open_conn

VERSION_URL = f"{BASE}/version"


@respx.mock
def test_reports_the_server_version():
    respx.get(VERSION_URL).mock(
        return_value=httpx.Response(200, json={"version": "1.4.0", "api_version": 1})
    )
    conn = open_conn()
    assert conn.server_version() == ServerVersion(version="1.4.0", api_version=1)


@respx.mock
def test_older_server_without_the_endpoint_returns_none():
    """A server predating GET /api/version answers 404; by its contract that means
    "assume the oldest supported behaviour", which the helper signals as None."""
    respx.get(VERSION_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    conn = open_conn()
    assert conn.server_version() is None


@respx.mock
def test_answers_even_after_the_session_is_dead():
    """The version endpoint is session-independent, so it stays useful for diagnosing a
    connection whose session has been reaped."""
    respx.get(VERSION_URL).mock(
        return_value=httpx.Response(200, json={"version": "2.0.0", "api_version": 2})
    )
    conn = open_conn()
    conn._mark_dead()
    assert conn.server_version().api_version == 2


@respx.mock
def test_raises_once_the_connection_is_closed():
    conn = open_conn()
    # The DELETE the session close issues.
    respx.delete(f"{BASE}/sql/sessions/11111111-1111-1111-1111-111111111111").mock(
        return_value=httpx.Response(204)
    )
    conn.close()
    with pytest.raises(ProgrammingError, match="connection is closed"):
        conn.server_version()


@respx.mock
def test_a_non_404_error_still_raises():
    respx.get(VERSION_URL).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    conn = open_conn()
    with pytest.raises(Exception):  # noqa: B017 - 500 maps to InternalError, not None
        conn.server_version()
