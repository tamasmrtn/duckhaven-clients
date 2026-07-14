"""PEP 249 (DB-API 2.0) declarations.

This module currently defines the mandated exception hierarchy. The module globals
(``apilevel``/``threadsafety``/``paramstyle``), the type objects, and the ``connect``
entry point are added in a later change; ``errors.py`` maps transport failures onto the
exceptions defined here.
"""


class Warning(Exception):  # noqa: A001 - PEP 249 mandates this exact name
    """Non-fatal warning, per PEP 249. Standalone (not an ``Error``)."""


class Error(Exception):
    """Base of every DuckHaven connector error (PEP 249 ``Error``).

    Carries the DuckHaven server's structured error fields when the failure came from
    an HTTP response, so callers can branch on them without re-parsing the message.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        # The server's ``error`` slug (e.g. "statement_not_allowed"), when present.
        self.code = code
        # The originating HTTP status, when the error came from a response.
        self.status_code = status_code
        # The server's human-readable ``detail``, when present.
        self.detail = detail


class InterfaceError(Error):
    """Error in the connector/interface rather than the database itself."""


class DatabaseError(Error):
    """Error reported by the database (DuckHaven)."""


class DataError(DatabaseError):
    """Problem with the processed data (bad values, out-of-range, ...)."""


class OperationalError(DatabaseError):
    """Operational failure not necessarily the caller's fault.

    Transport errors, an unavailable/disconnected agent, a reaped or closed session,
    timeouts, and the session surface being disabled all map here.
    """


class IntegrityError(DatabaseError):
    """Relational integrity was violated."""


class InternalError(DatabaseError):
    """The database reported an internal error (e.g. HTTP 500)."""


class ProgrammingError(DatabaseError):
    """Caller error: rejected statement, denied grant, missing object, bad usage."""


class NotSupportedError(DatabaseError):
    """A method or API the backend does not support was used."""
