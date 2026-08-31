"""The REST client, against a mocked transport.

The pagination cases are the point: a single fetch of a paged endpoint returns the
first hundred rows and looks complete, which is the failure mode this layer exists
to prevent.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from dh.errors import AuthError, NotFoundError, TimeoutError, UnavailableError
from dh.rest import RestClient

HOST = "https://duckhaven.test"
API = f"{HOST}/api"


@pytest.fixture
def client():
    with RestClient(HOST, "dh_pat_secret") as rest:
        yield rest


# --- URL and headers -------------------------------------------------------


@pytest.mark.parametrize("host", [HOST, f"{HOST}/", f"{HOST}///"])
def test_the_api_mount_is_joined_exactly_once(host):
    assert RestClient(host, "t").base_url == API


@respx.mock
def test_a_leading_slash_on_the_path_does_not_double_up(client):
    route = respx.get(f"{API}/workspaces").mock(return_value=httpx.Response(200, json=[]))
    client.get("/workspaces")
    assert route.called


@respx.mock
def test_the_bearer_token_and_user_agent_are_sent(client):
    route = respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json={"id": "1"}))
    client.get("me")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer dh_pat_secret"
    assert request.headers["user-agent"].startswith("duckhaven-cli/")


@respx.mock
def test_unset_parameters_are_omitted_rather_than_sent_empty(client):
    """`status` has no server-side default; sending it empty would narrow to nothing."""
    route = respx.get(f"{API}/workspaces/w/queries").mock(return_value=httpx.Response(200, json=[]))
    client.get("workspaces/w/queries", params={"status": None, "limit": 10, "origin": []})
    assert "status" not in route.calls[0].request.url.params
    assert "origin" not in route.calls[0].request.url.params
    assert route.calls[0].request.url.params["limit"] == "10"


@respx.mock
def test_raw_content_is_sent_verbatim(client):
    """The semantic import route reads its body raw; re-encoding would defeat that."""
    payload = b"semantic_models:\n  - name: orders\n"
    route = respx.post(f"{API}/workspaces/w/semantic/imports/dbt").mock(
        return_value=httpx.Response(200, json={"created": 1})
    )
    client.post("workspaces/w/semantic/imports/dbt", content=payload)
    assert route.calls[0].request.content == payload


# --- Responses -------------------------------------------------------------


@respx.mock
def test_a_204_decodes_to_none(client):
    respx.delete(f"{API}/queries/abc").mock(return_value=httpx.Response(204))
    assert client.delete("queries/abc") is None


@respx.mock
def test_an_error_response_becomes_the_matching_dh_error(client):
    respx.get(f"{API}/workspaces/nope").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "No workspace"})
    )
    with pytest.raises(NotFoundError) as exc:
        client.get("workspaces/nope")
    assert exc.value.code == "not_found"
    assert exc.value.message == "No workspace"


@respx.mock
def test_a_401_is_an_auth_error(client):
    respx.get(f"{API}/me").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized", "message": "nope"})
    )
    with pytest.raises(AuthError):
        client.get("me")


@respx.mock
def test_a_timeout_is_reported_as_one_not_as_a_crash(client):
    respx.get(f"{API}/me").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(TimeoutError) as exc:
        client.get("me")
    assert exc.value.code == "client_timeout"


@respx.mock
def test_a_refused_connection_names_the_host(client):
    respx.get(f"{API}/me").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UnavailableError) as exc:
        client.get("me")
    assert API in exc.value.message


# --- Collections -----------------------------------------------------------


@respx.mock
def test_a_bare_array_endpoint_reports_no_cursor(client):
    """Bounded collections are exempt from the page envelope by design."""
    respx.get(f"{API}/workspaces").mock(return_value=httpx.Response(200, json=[{"slug": "a"}]))
    rows, cursor, has_more = client.collect("workspaces")
    assert rows == [{"slug": "a"}]
    assert cursor is None
    assert has_more is False


@respx.mock
def test_a_paged_endpoint_reports_its_cursor(client):
    respx.get(f"{API}/workspaces/w/queries").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "1"}], "cursor": "c1", "has_more": True}
        )
    )
    rows, cursor, has_more = client.collect("workspaces/w/queries")
    assert rows == [{"id": "1"}]
    assert cursor == "c1"
    assert has_more is True


@respx.mock
def test_walk_follows_the_cursor_to_the_end(client):
    """The truncation trap: one fetch returns page one and looks complete."""
    pages = [
        httpx.Response(
            200, json={"items": [{"id": 1}, {"id": 2}], "cursor": "c1", "has_more": True}
        ),
        httpx.Response(
            200, json={"items": [{"id": 3}, {"id": 4}], "cursor": "c2", "has_more": True}
        ),
        httpx.Response(200, json={"items": [{"id": 5}], "cursor": None, "has_more": False}),
    ]
    route = respx.get(f"{API}/workspaces/w/queries").mock(side_effect=pages)
    assert [row["id"] for row in client.walk("workspaces/w/queries")] == [1, 2, 3, 4, 5]
    assert route.call_count == 3
    # Each request after the first carries the previous page's cursor.
    assert route.calls[1].request.url.params["cursor"] == "c1"
    assert route.calls[2].request.url.params["cursor"] == "c2"


@respx.mock
def test_walk_over_a_bare_array_yields_once(client):
    route = respx.get(f"{API}/agents").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    assert list(client.walk("agents")) == [{"id": 1}]
    assert route.call_count == 1


@respx.mock
def test_walk_stops_at_max_rows_without_fetching_more(client):
    pages = [
        httpx.Response(
            200, json={"items": [{"id": 1}, {"id": 2}], "cursor": "c1", "has_more": True}
        ),
        httpx.Response(200, json={"items": [{"id": 3}], "cursor": None, "has_more": False}),
    ]
    route = respx.get(f"{API}/workspaces/w/queries").mock(side_effect=pages)
    rows = list(client.walk("workspaces/w/queries", max_rows=2))
    assert len(rows) == 2
    assert route.call_count == 1


@respx.mock
def test_walk_stops_when_has_more_is_false_even_with_a_cursor(client):
    """A server that leaves a stale cursor on the last page must not loop us forever."""
    route = respx.get(f"{API}/workspaces/w/queries").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}], "cursor": "c", "has_more": False}
        )
    )
    assert len(list(client.walk("workspaces/w/queries"))) == 1
    assert route.call_count == 1
