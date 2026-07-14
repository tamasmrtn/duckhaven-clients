"""PEP 249 (DB-API 2.0) declarations.

The mandated exception hierarchy, module globals, type objects, and constructors.
``connect`` lives in ``connection.py`` (and is re-exported from the package root) to
avoid an import cycle; ``errors.py`` maps transport failures onto the exceptions here.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any

# --- Module globals (PEP 249) ------------------------------------------------

apilevel = "2.0"
# 1 = threads may share the module but not connections. dbt opens one session
# (Connection) per thread, so this is the correct, honest value.
threadsafety = 1
# The statement API takes no server-side parameters; the connector renders ``?``
# placeholders into safe SQL literals client-side (see _params.py).
paramstyle = "qmark"


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


# --- Type objects and constructors (PEP 249) --------------------------------


class _DBAPITypeObject:
    """A type-comparison singleton that equals any of the given type-code names."""

    def __init__(self, *values: str) -> None:
        self.values = frozenset(values)

    def __eq__(self, other: object) -> bool:
        return other in self.values

    def __ne__(self, other: object) -> bool:
        return other not in self.values

    def __hash__(self) -> int:
        return hash(self.values)


STRING = _DBAPITypeObject("VARCHAR", "TEXT", "CHAR", "BPCHAR", "STRING")
BINARY = _DBAPITypeObject("BLOB", "BYTEA", "VARBINARY")
NUMBER = _DBAPITypeObject(
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
)
DATETIME = _DBAPITypeObject("TIMESTAMP", "TIMESTAMPTZ", "DATE", "TIME", "TIMETZ")
ROWID = _DBAPITypeObject("ROWID")

Date = _dt.date
Time = _dt.time
Timestamp = _dt.datetime


def DateFromTicks(ticks: float) -> _dt.date:
    return Date(*_time.localtime(ticks)[:3])


def TimeFromTicks(ticks: float) -> _dt.time:
    return Time(*_time.localtime(ticks)[3:6])


def TimestampFromTicks(ticks: float) -> _dt.datetime:
    return Timestamp(*_time.localtime(ticks)[:6])


def Binary(value: Any) -> bytes:
    return bytes(value)
