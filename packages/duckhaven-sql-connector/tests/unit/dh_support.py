"""Shared constants and builders for the respx-driven Connection/Cursor tests."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import httpx
import respx

from duckhaven_sql_connector.client import Transport
from duckhaven_sql_connector.config import ClientConfig, RetryPolicy
from duckhaven_sql_connector.connection import Connection

BASE = "https://dh.test/api"
WS = "analytics"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
QUERY_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"

SESSIONS_URL = f"{BASE}/workspaces/{WS}/sql/sessions"
SESSION_URL = f"{BASE}/sql/sessions/{SESSION_ID}"
STATEMENTS_URL = f"{BASE}/sql/sessions/{SESSION_ID}/statements"
QUERY_URL = f"{BASE}/queries/{QUERY_ID}"
ROWS_URL = f"{BASE}/queries/{QUERY_ID}/rows"


def make_config(**over: Any) -> ClientConfig:
    base: dict[str, Any] = {
        "host": "https://dh.test",
        "workspace": WS,
        "token": "dh_pat_x",
        # No transport-level retries so call counts in tests are exact.
        "retry": RetryPolicy(max_retries=0, backoff_base=0.0, backoff_max=0.0),
    }
    base.update(over)
    return ClientConfig(**base)


def make_transport(
    config: ClientConfig, *, monotonic: Callable[[], float] | None = None
) -> Transport:
    kwargs: dict[str, Any] = {"sleep": lambda _: None}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return Transport(config, **kwargs)


def session_json(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": SESSION_ID,
        "status": "open",
        "agent_id": AGENT_ID,
        "active_catalog": "sales",
        "staging_uri": "s3://bucket/sales/_staging/abc",
        "error": None,
        "created_at": "2026-01-01T00:00:00Z",
        "last_active_at": "2026-01-01T00:00:00Z",
    }
    data.update(over)
    return data


def mock_open_session(**over: Any) -> None:
    respx.post(SESSIONS_URL).mock(return_value=httpx.Response(201, json=session_json(**over)))


def open_conn(
    config: ClientConfig | None = None, *, monotonic: Callable[[], float] | None = None
) -> Connection:
    """Register the session-open mock and open a Connection over a no-sleep transport."""
    config = config or make_config()
    mock_open_session()
    return Connection.open(config, transport=make_transport(config, monotonic=monotonic))


def steady_clock(step: float = 10_000.0):
    """A monotonic() that jumps far each call, to trip the poll deadline deterministically."""
    counter = itertools.count(0.0, step)
    return lambda: next(counter)
