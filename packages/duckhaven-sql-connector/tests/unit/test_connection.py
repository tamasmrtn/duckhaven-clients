import json

import httpx
import pytest
import respx

from duckhaven_sql_connector import connect
from duckhaven_sql_connector.client import Transport
from duckhaven_sql_connector.connection import Connection
from duckhaven_sql_connector.dbapi import (
    InterfaceError,
    MaxRetryDurationError,
    OperationalError,
    ProgrammingError,
)

from .dh_support import (
    AGENT_ID,
    QUERY_ID,
    QUERY_URL,
    ROWS_URL,
    SESSION_URL,
    SESSIONS_URL,
    STATEMENTS_URL,
    make_config,
    make_transport,
    mock_cold_open,
    mock_open_session,
    open_conn,
    pending_json,
    session_json,
    steady_clock,
)


@respx.mock
def test_open_populates_session_fields():
    conn = open_conn()
    assert conn.agent_id == AGENT_ID
    assert conn.active_catalog == "sales"
    assert conn.staging_uri.endswith("/_staging/abc")


@respx.mock
def test_close_deletes_session_and_is_idempotent():
    delete = respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    conn.close()  # second close is a no-op, no second DELETE
    assert delete.call_count == 1


@respx.mock
def test_context_manager_closes():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    with open_conn() as conn:
        assert conn._closed is False
    assert conn._closed is True


@respx.mock
def test_cursor_on_closed_connection_raises():
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    conn = open_conn()
    conn.close()
    with pytest.raises(ProgrammingError):
        conn.cursor()


@respx.mock
def test_session_gone_marks_connection_dead():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            409, json={"detail": {"error": "session_not_open", "detail": "reaped"}}
        )
    )
    cur = conn.cursor()
    with pytest.raises(OperationalError):
        cur.execute("SELECT 1")
    # The connection is now dead: opening another cursor fails fast.
    with pytest.raises(OperationalError):
        conn.cursor()


@respx.mock
def test_open_failure_closes_transport():
    respx.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
    config = make_config()
    transport = make_transport(config)
    with pytest.raises(OperationalError):
        Connection.open(config, transport=transport)
    assert transport._client.is_closed


@respx.mock
def test_open_on_forbidden_agent_raises_programming_error():
    """403 agent_forbidden: the agent is visible but the caller's tier is too low."""
    respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(
            403,
            json={
                "detail": {
                    "error": "agent_forbidden",
                    "detail": "This action on agent 'warehouse-a' requires the 'use' tier.",
                }
            },
        )
    )
    config = make_config(agent=AGENT_ID)
    transport = make_transport(config)
    with pytest.raises(ProgrammingError) as exc:
        Connection.open(config, transport=transport)
    assert exc.value.code == "agent_forbidden"
    assert exc.value.status_code == 403
    assert transport._client.is_closed


@respx.mock
def test_open_on_restricted_agent_raises_programming_error():
    """404 on a restricted agent the caller holds no grant on.

    The server hides such an agent rather than forbidding it, so the denial is
    indistinguishable from a deleted agent — no error code, and the same
    ProgrammingError a genuinely missing id would raise. Pinned because the natural
    reading of "not found" is a client-side typo, and it is not retryable either way.
    """
    respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(404, json={"detail": "Agent not found"})
    )
    config = make_config(agent=AGENT_ID)
    transport = make_transport(config)
    with pytest.raises(ProgrammingError) as exc:
        Connection.open(config, transport=transport)
    assert exc.value.code is None
    assert exc.value.status_code == 404
    assert transport._client.is_closed


# -- Elastic cold start ------------------------------------------------------


@respx.mock
def test_warm_open_does_not_poll():
    """The common case must stay one request. A 201 is already open."""
    poll = respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=session_json()))
    open_conn()
    assert poll.call_count == 0


@respx.mock
def test_cold_open_polls_until_the_session_opens():
    poll = mock_cold_open(pending_json(), pending_json(status="opening"), session_json())
    config = make_config()
    conn = Connection.open(config, transport=make_transport(config))
    assert poll.call_count == 3
    # Read off the *polled* payload, not the 202 body: a pending session names no agent
    # until one claims it, so trusting the first response would strand agent_id at None.
    assert conn.agent_id == AGENT_ID
    assert conn.staging_uri.endswith("/_staging/abc")


@respx.mock
def test_cold_open_raises_with_the_servers_reason_when_compute_never_arrives():
    """A terminal status stops the wait early — the reason beats a timeout."""
    mock_cold_open(pending_json(status="failed", error="provisioning_timeout"))
    config = make_config()
    with pytest.raises(OperationalError) as exc:
        Connection.open(config, transport=make_transport(config))
    assert exc.value.code == "provisioning_timeout"
    assert "provisioning_timeout" in str(exc.value)


@respx.mock
def test_cold_open_gives_up_at_the_compute_wait_budget():
    mock_cold_open(*[pending_json()] * 5)
    config = make_config(compute_wait=30.0)
    # A clock that jumps 10s per call blows the 30s budget within a few polls.
    transport = make_transport(config, monotonic=steady_clock(10.0))
    with pytest.raises(MaxRetryDurationError) as exc:
        Connection.open(config, transport=transport)
    assert "30.0s" in str(exc.value)
    assert transport._client.is_closed


@respx.mock
def test_open_retries_a_compute_starting_503_and_honours_retry_after():
    """The fallback for a server that answered 503 despite being asked to continue.

    Reachable only if the wait fields did not take effect (an API gateway dropping an
    unknown field, say) — a current server hands back a 202 instead. Retrying is safe
    because the server abandons the session row but *not* the compute it started, so
    the retry lands on the agent already coming up.
    """
    slept: list[float] = []
    open_route = respx.post(SESSIONS_URL).mock(
        side_effect=[
            httpx.Response(
                503,
                json={"detail": {"error": "compute_starting", "detail": "starting"}},
                headers={"Retry-After": "7"},
            ),
            httpx.Response(201, json=session_json()),
        ]
    )
    config = make_config()
    transport = Transport(config, sleep=slept.append)
    conn = Connection.open(config, transport=transport)
    assert open_route.call_count == 2
    assert slept == [7.0]
    assert conn.agent_id == AGENT_ID


@respx.mock
def test_open_does_not_retry_a_503_without_the_compute_starting_code():
    """A server with no elastic compute answers a plain 503 for a cold pool.

    Retrying that would never produce an agent, so it must fail on the first response —
    the distinction is the error code, not the status.
    """
    open_route = respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(503, json={"detail": "No connected agent available"})
    )
    config = make_config()
    with pytest.raises(OperationalError) as exc:
        Connection.open(config, transport=make_transport(config))
    assert open_route.call_count == 1
    assert exc.value.code is None


@respx.mock
def test_disabled_wait_fails_fast_on_compute_starting():
    open_route = respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(
            503,
            json={"detail": {"error": "compute_starting", "detail": "starting"}},
            headers={"Retry-After": "5"},
        )
    )
    config = make_config(compute_wait=0)
    with pytest.raises(OperationalError):
        Connection.open(config, transport=make_transport(config))
    assert open_route.call_count == 1


@respx.mock
def test_a_success_that_is_not_open_is_never_treated_as_usable():
    """Guards the trap that made this work necessary.

    open() used to read the body without checking the session status, so a 202 yielded a
    Connection over an unusable session whose first statement 409'd. Anything not `open`
    is now either waited out or raised.
    """
    mock_cold_open(pending_json(status="closed"))
    config = make_config()
    with pytest.raises(OperationalError) as exc:
        Connection.open(config, transport=make_transport(config))
    assert "closed" in str(exc.value)


@respx.mock
def test_schema_default_issues_quoted_use():
    mock_open_session(active_catalog="sales")
    statements = respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            202, json={"id": "22222222-2222-2222-2222-222222222222", "status": "done"}
        )
    )
    respx.get("https://dh.test/api/queries/22222222-2222-2222-2222-222222222222/rows").mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )

    config = make_config(schema="analytics")
    Connection.open(config, transport=make_transport(config))

    sent_sql = json.loads(statements.calls.last.request.content)["sql"]
    assert sent_sql == 'USE "sales"."analytics"'


def test_agent_must_be_uuid():
    with pytest.raises(InterfaceError):
        make_config(agent="warehouse-a")


@respx.mock
def test_open_sends_agent_and_catalog_in_body():
    open_route = respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(201, json=session_json())
    )
    config = make_config(agent=AGENT_ID, catalog="raw")
    Connection.open(config, transport=make_transport(config))
    body = json.loads(open_route.calls.last.request.content)
    assert body == {
        "agent_id": AGENT_ID,
        "catalog": "raw",
        # Asks the server to hand back a session it could not open in time rather than
        # a 503, so a cold elastic pool can be waited out instead of failing.
        "wait_timeout_s": 10.0,
        "on_wait_timeout": "continue",
    }


@respx.mock
def test_open_omits_the_wait_fields_when_the_wait_is_disabled():
    """compute_wait=0 restores the pre-cold-start request exactly.

    Not merely a shorter budget: without the fields the server keeps its own default of
    `cancel`, so it answers 503 rather than handing back a pending session nobody is
    going to poll.
    """
    open_route = respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(201, json=session_json())
    )
    config = make_config(catalog="raw", compute_wait=0)
    Connection.open(config, transport=make_transport(config))
    assert json.loads(open_route.calls.last.request.content) == {"catalog": "raw"}


@respx.mock
def test_top_level_connect_opens_over_a_real_transport():
    mock_open_session()
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(204))
    with connect(host="https://dh.test", workspace="analytics", token="dh_pat_x") as conn:
        assert conn.active_catalog == "sales"


@respx.mock
def test_cancel_cancels_the_in_flight_statement():
    # dbt aborts a run by cancelling the connection from another thread; that must reach
    # the cursor's active statement and DELETE it on the query API.
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done"})
    )
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [], "columns": [], "cursor": None, "total": 0}
        )
    )
    cancel = respx.delete(QUERY_URL).mock(return_value=httpx.Response(204))
    cur = conn.cursor()
    cur.execute("SELECT 1")
    conn.cancel()
    assert cancel.called


@respx.mock
def test_cancel_is_a_noop_without_any_statement():
    # No statement was ever submitted, so there is nothing to DELETE; no query route is
    # registered, so respx would raise if cancel tried to call one.
    conn = open_conn()
    conn.cursor()
    conn.cancel()  # must not raise
