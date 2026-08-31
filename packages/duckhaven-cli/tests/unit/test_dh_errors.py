"""Error envelope parsing and the status-to-exit-code contract.

The exit codes are a promise CI branches on, so every row of the documented table
is asserted here rather than assumed.
"""

from __future__ import annotations

import pytest

from dh.errors import (
    AuthError,
    ConflictError,
    DhError,
    ExitCode,
    NotFoundError,
    QueryFailed,
    TimeoutError,
    UnavailableError,
    from_connector,
    from_status,
    parse_envelope,
)


class _ConnectorError(Exception):
    """Stands in for a duckhaven-sql-connector DB-API exception."""

    def __init__(self, message, *, code=None, status_code=None, detail=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.detail = detail


# --- Envelope parsing ------------------------------------------------------


def test_the_server_envelope_is_read_verbatim():
    body = {"error": "sql_not_allowed", "message": "DDL is not permitted.", "details": {"a": 1}}
    assert parse_envelope(body, 422) == ("sql_not_allowed", "DDL is not permitted.", {"a": 1})


def test_a_missing_message_falls_back_to_the_code():
    assert parse_envelope({"error": "forbidden"}, 403)[1] == "forbidden"


def test_non_dict_details_are_dropped_rather_than_passed_through():
    assert parse_envelope({"error": "x", "message": "m", "details": "oops"}, 400)[2] is None


def test_an_api_version_1_detail_body_still_yields_a_message():
    assert parse_envelope({"detail": "Workspace not found"}, 404)[1] == "Workspace not found"


def test_a_proxy_html_page_yields_the_status_rather_than_failing():
    """The moment a caller most needs a message is when something upstream misbehaves."""
    code, message, _ = parse_envelope("<html>502 Bad Gateway</html>", 502)
    assert code == "http_502"
    assert "502" in message


def test_an_empty_body_still_produces_a_message():
    assert parse_envelope("", 500)[1] == "HTTP 500"


# --- Status to exit code ---------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_type", "expected_code"),
    [
        (401, AuthError, ExitCode.AUTH),
        (403, AuthError, ExitCode.AUTH),
        (404, NotFoundError, ExitCode.NOT_FOUND),
        (409, ConflictError, ExitCode.CONFLICT),
        (410, ConflictError, ExitCode.CONFLICT),
        (422, ConflictError, ExitCode.CONFLICT),
        (400, ConflictError, ExitCode.CONFLICT),
        (405, ConflictError, ExitCode.CONFLICT),
        (408, TimeoutError, ExitCode.TIMEOUT),
        (504, TimeoutError, ExitCode.TIMEOUT),
        (429, UnavailableError, ExitCode.UNAVAILABLE),
        (502, UnavailableError, ExitCode.UNAVAILABLE),
        (503, UnavailableError, ExitCode.UNAVAILABLE),
        (500, DhError, ExitCode.FAILURE),
    ],
)
def test_every_documented_status_maps_to_its_exit_code(status, expected_type, expected_code):
    error = from_status(status, {"error": "x", "message": "m"})
    assert isinstance(error, expected_type)
    assert error.exit_code is expected_code


def test_a_500_is_a_crash_not_an_outage():
    """Telling a pipeline to retry a server bug just wastes its time."""
    assert from_status(500, {"error": "internal_error", "message": "boom"}).exit_code is (
        ExitCode.FAILURE
    )


def test_the_envelope_survives_the_round_trip():
    error = from_status(422, {"error": "sql_not_allowed", "message": "no", "details": {"k": "v"}})
    assert error.envelope() == {
        "error": "sql_not_allowed",
        "message": "no",
        "details": {"k": "v"},
    }


def test_query_failed_has_its_own_code():
    """Exit 6 is what separates bad SQL from a broken CLI."""
    assert QueryFailed("query_failed", "boom").exit_code is ExitCode.QUERY_FAILED


# --- Connector exceptions --------------------------------------------------


def test_a_connector_error_reuses_the_status_it_carries():
    exc = _ConnectorError("[403] denied", code="grant_denied", status_code=403, detail="denied")
    mapped = from_connector(exc)
    assert isinstance(mapped, AuthError)
    assert mapped.code == "grant_denied"
    assert mapped.message == "denied"


def test_a_connector_transport_failure_is_unavailable():
    """No response ever arrived, so there is no status to map."""
    mapped = from_connector(_ConnectorError("transport error: connection refused"))
    assert isinstance(mapped, UnavailableError)
    assert mapped.exit_code is ExitCode.UNAVAILABLE


def test_a_connector_error_without_a_slug_derives_one_from_the_status():
    mapped = from_connector(_ConnectorError("[409] busy", status_code=409))
    assert mapped.code == "http_409"
    assert mapped.exit_code is ExitCode.CONFLICT
