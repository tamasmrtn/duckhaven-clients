import httpx
import pytest

from duckhaven_sql_connector import dbapi
from duckhaven_sql_connector.errors import map_http_error, map_transport_error


def _resp(status: int, *, json=None, text=None) -> httpx.Response:
    if json is not None:
        return httpx.Response(status, json=json)
    return httpx.Response(status, text=text or "")


def test_exception_hierarchy_matches_pep249():
    assert issubclass(dbapi.InterfaceError, dbapi.Error)
    assert issubclass(dbapi.DatabaseError, dbapi.Error)
    for sub in (
        dbapi.DataError,
        dbapi.OperationalError,
        dbapi.IntegrityError,
        dbapi.InternalError,
        dbapi.ProgrammingError,
        dbapi.NotSupportedError,
    ):
        assert issubclass(sub, dbapi.DatabaseError)
    # Warning is standalone, not an Error.
    assert not issubclass(dbapi.Warning, dbapi.Error)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (422, "statement_not_allowed", dbapi.ProgrammingError),
        (422, "sql_not_allowed", dbapi.ProgrammingError),
        (403, "grant_denied", dbapi.ProgrammingError),
        (403, "agent_forbidden", dbapi.ProgrammingError),
        (422, "agent_incompatible", dbapi.ProgrammingError),
        (503, "compute_starting", dbapi.OperationalError),
        (503, "compute_unavailable", dbapi.OperationalError),
        (409, "session_not_open", dbapi.OperationalError),
        (409, "catalog_read_only", dbapi.OperationalError),
        (503, "session_open_failed", dbapi.OperationalError),
    ],
)
def test_maps_structured_error_codes(status, code, expected):
    resp = _resp(status, json={"detail": {"error": code, "detail": "boom"}})
    exc = map_http_error(resp)
    assert isinstance(exc, expected)
    assert exc.code == code
    assert exc.detail == "boom"
    assert exc.status_code == status


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (422, "statement_not_allowed", dbapi.ProgrammingError),
        (422, "sql_not_allowed", dbapi.ProgrammingError),
        (403, "grant_denied", dbapi.ProgrammingError),
        (403, "agent_forbidden", dbapi.ProgrammingError),
        (422, "agent_incompatible", dbapi.ProgrammingError),
        (503, "compute_starting", dbapi.OperationalError),
        (503, "compute_unavailable", dbapi.OperationalError),
        (409, "session_not_open", dbapi.OperationalError),
        (409, "catalog_read_only", dbapi.OperationalError),
        (503, "session_open_failed", dbapi.OperationalError),
    ],
)
def test_maps_structured_error_codes_v2_envelope(status, code, expected):
    """The api_version 2 envelope is flat: {"error": ..., "message": ..., "details": ...}."""
    resp = _resp(status, json={"error": code, "message": "boom", "details": None})
    exc = map_http_error(resp)
    assert isinstance(exc, expected)
    assert exc.code == code
    assert exc.detail == "boom"
    assert exc.status_code == status


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, dbapi.InterfaceError),
        (403, dbapi.ProgrammingError),
        (409, dbapi.OperationalError),
        (410, dbapi.OperationalError),
        (500, dbapi.InternalError),
        (503, dbapi.OperationalError),
        (504, dbapi.OperationalError),
        (418, dbapi.DatabaseError),
    ],
)
def test_maps_plain_string_detail_by_status(status, expected):
    resp = _resp(status, json={"detail": "something happened"})
    exc = map_http_error(resp)
    assert isinstance(exc, expected)
    assert exc.code is None
    assert exc.detail == "something happened"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, dbapi.InterfaceError),
        (403, dbapi.ProgrammingError),
        (409, dbapi.OperationalError),
        (410, dbapi.OperationalError),
        (500, dbapi.InternalError),
        (503, dbapi.OperationalError),
        (504, dbapi.OperationalError),
        (418, dbapi.DatabaseError),
    ],
)
def test_maps_v2_envelope_by_status_when_code_is_generic(status, expected):
    """A status-derived v2 code (e.g. "conflict", "unauthorized") isn't in either slug set,
    so classification still falls back to the status, exactly as with a v1 plain string."""
    resp = _resp(status, json={"error": "some_derived_code", "message": "something happened"})
    exc = map_http_error(resp)
    assert isinstance(exc, expected)
    assert exc.code == "some_derived_code"
    assert exc.detail == "something happened"


def test_agent_forbidden_slug_wins_over_the_status_default():
    """The slug classifies, not the status.

    403 already defaults to ProgrammingError, so listing `agent_forbidden` in
    _PROGRAMMING_CODES only bites if the server ever sends it on a status that maps
    elsewhere (409 defaults to OperationalError, i.e. retry-and-reconnect — the wrong
    advice for an access denial).
    """
    exc = map_http_error(_resp(409, json={"detail": {"error": "agent_forbidden", "detail": "no"}}))
    assert isinstance(exc, dbapi.ProgrammingError)


def test_compute_codes_win_over_the_status_default():
    """As with agent_forbidden: the slug classifies, not the status.

    503 already defaults to OperationalError, so listing the compute codes only bites if
    a server sends one on a status that maps elsewhere — 422 would otherwise read as a
    caller error, when in fact the caller need only wait.
    """
    for code in ("compute_starting", "compute_unavailable"):
        exc = map_http_error(_resp(422, json={"detail": {"error": code, "detail": "x"}}))
        assert isinstance(exc, dbapi.OperationalError), code


def test_retry_after_is_carried_onto_the_raised_error():
    """Connection.open reads this to pace its retry, so it must survive the mapping."""
    exc = map_http_error(
        httpx.Response(
            503,
            json={"detail": {"error": "compute_starting", "detail": "starting"}},
            headers={"Retry-After": "5"},
        )
    )
    assert exc.retry_after == 5.0
    assert map_http_error(_resp(503, json={"detail": "x"})).retry_after is None


def test_retry_after_parses_an_http_date_and_rejects_nonsense():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from duckhaven_sql_connector.errors import _retry_after_seconds

    future = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    assert (
        20
        < _retry_after_seconds(
            httpx.Response(503, headers={"Retry-After": format_datetime(future)})
        )
        <= 30
    )
    assert _retry_after_seconds(httpx.Response(503)) is None
    # A malformed value is ignored rather than raised: the caller falls back to its own
    # backoff, which is always safe.
    assert _retry_after_seconds(httpx.Response(503, headers={"Retry-After": "soon"})) is None
    # A date already in the past clamps to zero, never negative.
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
    assert (
        _retry_after_seconds(httpx.Response(503, headers={"Retry-After": format_datetime(past)}))
        == 0.0
    )


def test_404_disabled_is_operational_but_missing_is_programming():
    disabled = map_http_error(_resp(404, json={"detail": "SQL sessions are not enabled"}))
    missing = map_http_error(_resp(404, json={"detail": "Session not found"}))
    assert isinstance(disabled, dbapi.OperationalError)
    assert isinstance(missing, dbapi.ProgrammingError)


def test_404_disabled_vs_missing_still_works_on_v2_envelope():
    """api_version 2 derives the same generic "not_found" slug for both cases (verified
    against the server: both raise a plain-string HTTPException), so there is no machine
    code to distinguish them on — this only keeps working because the server's message
    ("SQL sessions are not enabled" vs "Session not found") makes it through the v2
    envelope unchanged. A regression that drops `message` back to the reason phrase would
    misclassify a disabled-sessions deployment as a caller error again.
    """
    disabled = map_http_error(
        _resp(404, json={"error": "not_found", "message": "SQL sessions are not enabled"})
    )
    missing = map_http_error(
        _resp(404, json={"error": "not_found", "message": "Session not found"})
    )
    assert isinstance(disabled, dbapi.OperationalError)
    assert disabled.code == "not_found"
    assert disabled.detail == "SQL sessions are not enabled"
    assert isinstance(missing, dbapi.ProgrammingError)
    assert missing.detail == "Session not found"


def test_non_json_body_falls_back_to_text():
    resp = _resp(502, text="<html>bad gateway</html>")
    exc = map_http_error(resp)
    assert isinstance(exc, dbapi.OperationalError)
    assert "bad gateway" in exc.detail


def test_empty_non_json_body_falls_back_to_reason_phrase():
    exc = map_http_error(_resp(500, text=""))
    assert isinstance(exc, dbapi.InternalError)
    assert exc.detail == "Internal Server Error"


def test_structured_error_without_detail_uses_slug_as_message():
    exc = map_http_error(_resp(422, json={"detail": {"error": "some_code"}}))
    assert isinstance(exc, dbapi.ProgrammingError)
    assert exc.code == "some_code"
    assert exc.detail == "some_code"


def test_unexpected_detail_shape_is_stringified():
    exc = map_http_error(_resp(400, json={"detail": ["a", "b"]}))
    assert isinstance(exc, dbapi.ProgrammingError)
    assert exc.code is None
    assert "a" in exc.detail


def test_json_without_detail_key_falls_back_to_reason_phrase():
    exc = map_http_error(_resp(500, json={"message": "oops"}))
    assert isinstance(exc, dbapi.InternalError)
    assert exc.detail == "Internal Server Error"


def test_map_transport_error():
    exc = map_transport_error(httpx.ConnectError("refused"))
    assert isinstance(exc, dbapi.OperationalError)
    assert "refused" in str(exc)
