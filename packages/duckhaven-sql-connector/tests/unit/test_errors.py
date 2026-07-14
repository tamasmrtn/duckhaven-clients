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
        (422, "agent_incompatible", dbapi.ProgrammingError),
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


def test_404_disabled_is_operational_but_missing_is_programming():
    disabled = map_http_error(_resp(404, json={"detail": "SQL sessions are not enabled"}))
    missing = map_http_error(_resp(404, json={"detail": "Session not found"}))
    assert isinstance(disabled, dbapi.OperationalError)
    assert isinstance(missing, dbapi.ProgrammingError)


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
