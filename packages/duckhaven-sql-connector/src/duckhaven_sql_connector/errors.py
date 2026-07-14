"""Map DuckHaven HTTP failures onto the PEP 249 exception hierarchy.

DuckHaven's FastAPI errors arrive as ``{"detail": ...}`` where ``detail`` is either a
plain string or a structured ``{"error": <slug>, "detail": <message>}`` object. Both
shapes are normalized to a ``(code, message)`` pair and mapped to a DB-API exception,
preserving the slug and status on the raised error.
"""

from __future__ import annotations

import httpx

from .dbapi import (
    DatabaseError,
    Error,
    InterfaceError,
    InternalError,
    OperationalError,
    ProgrammingError,
)

# Slugs the connector maps explicitly regardless of the (usually matching) status code.
_PROGRAMMING_CODES = frozenset(
    {"statement_not_allowed", "sql_not_allowed", "grant_denied", "agent_incompatible"}
)
_OPERATIONAL_CODES = frozenset({"session_not_open", "catalog_read_only", "session_open_failed"})


def _parse_body(response: httpx.Response) -> tuple[str | None, str]:
    """Return ``(error_slug, message)`` from a DuckHaven error response.

    Falls back to the raw response text when the body is not the expected JSON shape,
    so a proxy/HTML error page still yields a usable message.
    """
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return None, response.text.strip() or response.reason_phrase
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        code = detail.get("error")
        message = detail.get("detail") or detail.get("error") or ""
        return (str(code) if code is not None else None), str(message)
    if isinstance(detail, str):
        return None, detail
    return None, str(detail) if detail is not None else response.reason_phrase


def map_http_error(response: httpx.Response) -> Error:
    """Translate a >=400 response into the appropriate DB-API exception instance."""
    status = response.status_code
    code, message = _parse_body(response)

    exc_type: type[Error]
    if code in _PROGRAMMING_CODES:
        exc_type = ProgrammingError
    elif code in _OPERATIONAL_CODES:
        exc_type = OperationalError
    elif status == 400 or status == 422:
        exc_type = ProgrammingError
    elif status == 401:
        # Missing/invalid credentials: an interface-level problem, not a DB error.
        exc_type = InterfaceError
    elif status == 403:
        exc_type = ProgrammingError
    elif status == 404:
        # The session surface being disabled is operational; a missing object is a
        # caller (programming) error. Distinguish on the server's message.
        exc_type = OperationalError if "not enabled" in message.lower() else ProgrammingError
    elif status in (409, 410):
        exc_type = OperationalError
    elif status == 500:
        exc_type = InternalError
    elif status in (429, 502, 503, 504):
        exc_type = OperationalError
    else:
        exc_type = DatabaseError

    rendered = f"[{status}] {message}" if message else f"[{status}]"
    return exc_type(rendered, code=code, status_code=status, detail=message or None)


def map_transport_error(exc: httpx.TransportError) -> OperationalError:
    """Translate an httpx transport failure (connect/read/timeout) to OperationalError."""
    return OperationalError(f"transport error: {exc}")
