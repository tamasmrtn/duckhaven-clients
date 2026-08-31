"""Failures, and the exit status each one leaves behind.

Every error the CLI surfaces is a :class:`DhError` carrying the same three fields
DuckHaven's own error envelope uses -- ``error``, ``message``, ``details`` -- so a
caller parses one shape whether the failure happened locally or on the server.

Exit codes are differentiated by default rather than hidden behind a flag. The one
that earns its keep is :attr:`ExitCode.QUERY_FAILED`: it separates "the SQL you sent
was wrong" from "the CLI or the server broke".
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    #: An unhandled exception or a response the CLI could not make sense of.
    FAILURE = 1
    #: Bad flags or arguments. Typer exits with this on its own.
    USAGE = 2
    #: 401/403, a missing or rejected credential, an unreadable config.
    AUTH = 3
    NOT_FOUND = 4
    #: 409/410/422 -- the request was understood and refused.
    CONFLICT = 5
    #: The query ran and reached `failed`. The CLI worked; the SQL did not.
    QUERY_FAILED = 6
    TIMEOUT = 7
    #: 503, connection refused, DNS failure.
    UNAVAILABLE = 8
    #: Conventional for SIGINT. The server-side query is cancelled first.
    INTERRUPTED = 130


class DhError(Exception):
    """A failure with a machine code, a displayable message, and an exit status."""

    exit_code: ExitCode = ExitCode.FAILURE

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def envelope(self) -> dict[str, Any]:
        """The wire form, identical in shape to the server's own error body."""
        return {"error": self.code, "message": self.message, "details": self.details}


class ConfigError(DhError):
    """The profile file is missing, malformed, or too widely readable.

    Grouped under ``AUTH`` because from the caller's side it is the same problem as
    a rejected token: the CLI has no usable credential.
    """

    exit_code = ExitCode.AUTH


class AuthError(DhError):
    exit_code = ExitCode.AUTH


class NotFoundError(DhError):
    exit_code = ExitCode.NOT_FOUND


class ConflictError(DhError):
    """Understood and refused: 409, 410, 422, and other 4xx."""

    exit_code = ExitCode.CONFLICT


class TimeoutError(DhError):  # noqa: A001 - the CLI's timeout, not the builtin
    exit_code = ExitCode.TIMEOUT


class UnavailableError(DhError):
    exit_code = ExitCode.UNAVAILABLE


class QueryFailed(DhError):
    """The query ran and reached ``failed``. The CLI worked; the SQL did not."""

    exit_code = ExitCode.QUERY_FAILED


#: HTTP status to the class that carries the right exit code.
#:
#: 5xx other than the three below stays ``FAILURE``: a 500 is the server breaking
#: unexpectedly, which is not the same as it being unavailable, and telling a
#: pipeline to retry a crash wastes its time.
_BY_STATUS: dict[int, type[DhError]] = {
    401: AuthError,
    403: AuthError,
    404: NotFoundError,
    408: TimeoutError,
    429: UnavailableError,
    502: UnavailableError,
    503: UnavailableError,
    504: TimeoutError,
}


def _class_for(status: int) -> type[DhError]:
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    if 400 <= status < 500:
        return ConflictError
    return DhError


def parse_envelope(body: object, status: int) -> tuple[str, str, dict[str, Any] | None]:
    """Pull ``(error, message, details)` out of a response body.

    DuckHaven answers every 4xx and 5xx with that envelope, including crashes, so
    this normally just reads three keys. It still degrades gracefully: a proxy's
    HTML error page or a truncated body yields the status line rather than a
    parse failure, because the one moment a caller most needs a message is when
    something upstream is misbehaving.
    """
    if isinstance(body, dict) and "error" in body:
        code = str(body.get("error"))
        message = str(body.get("message") or body.get("detail") or code)
        details = body.get("details")
        return code, message, details if isinstance(details, dict) else None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        # api_version 1 shape, still emitted by servers predating the envelope.
        return f"http_{status}", body["detail"], None
    text = body if isinstance(body, str) else ""
    return f"http_{status}", text.strip() or f"HTTP {status}", None


def from_status(status: int, body: object) -> DhError:
    """Build the right error for an HTTP failure."""
    code, message, details = parse_envelope(body, status)
    return _class_for(status)(code, message, details)


def from_connector(exc: Exception) -> DhError:
    """Translate a ``duckhaven-sql-connector`` DB-API exception.

    The connector preserves the server's slug and status on the exception it
    raises, so the CLI re-derives the same exit code it would have chosen from the
    response itself rather than flattening every SQL failure to 1.
    """
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "detail", None) or str(exc)
    if status is None:
        # A transport failure: no response was ever received.
        return UnavailableError(code or "transport_error", message)
    return _class_for(int(status))(code or f"http_{status}", message)
