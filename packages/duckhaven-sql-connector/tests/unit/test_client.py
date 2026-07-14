import httpx
import pytest
import respx

from duckhaven_sql_connector import dbapi
from duckhaven_sql_connector.client import Transport
from duckhaven_sql_connector.config import ClientConfig, RetryPolicy

BASE = "https://dh.test/api"


def _transport(**over) -> Transport:
    cfg = ClientConfig(
        host="https://dh.test",
        workspace="analytics",
        token="dh_pat_secret",
        # Zero backoff keeps retry tests instant; sleep is stubbed out regardless.
        retry=RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0),
        **over,
    )
    return Transport(cfg, sleep=lambda _: None)


@respx.mock
def test_sends_bearer_and_user_agent():
    route = respx.get(f"{BASE}/probe").mock(return_value=httpx.Response(200, json={}))
    _transport().get("/probe")
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer dh_pat_secret"
    assert req.headers["user-agent"].startswith("duckhaven-sql-connector/")


@respx.mock
def test_get_retries_on_503_then_succeeds():
    route = respx.get(f"{BASE}/probe").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    resp = _transport().get("/probe")
    assert resp.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_get_retries_on_network_error():
    route = respx.get(f"{BASE}/probe").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200)]
    )
    assert _transport().get("/probe").status_code == 200
    assert route.call_count == 2


@respx.mock
def test_get_retry_exhausted_raises_operational():
    route = respx.get(f"{BASE}/probe").mock(return_value=httpx.Response(503, json={"detail": "x"}))
    with pytest.raises(dbapi.OperationalError):
        _transport().get("/probe")
    # initial attempt + max_retries
    assert route.call_count == 3


@respx.mock
def test_post_is_not_retried():
    route = respx.post(f"{BASE}/x").mock(return_value=httpx.Response(503, json={"detail": "x"}))
    with pytest.raises(dbapi.OperationalError):
        _transport().post("/x", json={})
    assert route.call_count == 1


@respx.mock
def test_delete_is_retried():
    route = respx.delete(f"{BASE}/x").mock(side_effect=[httpx.Response(502), httpx.Response(204)])
    assert _transport().delete("/x").status_code == 204
    assert route.call_count == 2


@respx.mock
def test_maps_422_to_programming_error_with_code():
    respx.post(f"{BASE}/stmt").mock(
        return_value=httpx.Response(
            422, json={"detail": {"error": "statement_not_allowed", "detail": "nope"}}
        )
    )
    with pytest.raises(dbapi.ProgrammingError) as ei:
        _transport().post("/stmt", json={"sql": "INSTALL foo"})
    assert ei.value.code == "statement_not_allowed"
    assert ei.value.status_code == 422


@respx.mock
def test_post_network_failure_raises_operational_without_retry():
    route = respx.post(f"{BASE}/x").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(dbapi.OperationalError):
        _transport().post("/x", json={})
    assert route.call_count == 1


@respx.mock
def test_success_returns_response_and_204_passes_through():
    respx.get(f"{BASE}/ok").mock(return_value=httpx.Response(204))
    assert _transport().get("/ok").status_code == 204


def test_context_manager_closes_client():
    with _transport() as t:
        assert t._client.is_closed is False
    assert t._client.is_closed is True


def _recording_transport(policy: RetryPolicy):
    cfg = ClientConfig(
        host="https://dh.test", workspace="analytics", token="dh_pat_x", retry=policy
    )
    slept: list[float] = []
    return Transport(cfg, sleep=slept.append), slept


@respx.mock
def test_retry_after_header_is_honored_over_backoff():
    transport, slept = _recording_transport(
        RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0)
    )
    respx.get(f"{BASE}/probe").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )
    transport.get("/probe")
    assert slept == [7.0]  # used the header, not the (zero) computed backoff


@respx.mock
def test_retry_budget_exceeded_raises_max_retry_duration():
    transport, _ = _recording_transport(
        RetryPolicy(max_retries=5, backoff_base=0.0, backoff_max=0.0, max_elapsed=1.0)
    )
    respx.get(f"{BASE}/probe").mock(
        return_value=httpx.Response(503, headers={"Retry-After": "100"})
    )
    with pytest.raises(dbapi.MaxRetryDurationError):
        transport.get("/probe")


def test_retry_after_parses_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from duckhaven_sql_connector.client import _retry_after_seconds

    future = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    resp = httpx.Response(503, headers={"Retry-After": format_datetime(future)})
    assert 20 < _retry_after_seconds(resp) <= 30
    assert _retry_after_seconds(httpx.Response(503)) is None
    assert _retry_after_seconds(httpx.Response(503, headers={"Retry-After": "garbage"})) is None
