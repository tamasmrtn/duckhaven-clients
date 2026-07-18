"""DuckHaven SQL connector — a DB-API 2.0 (PEP 249) client for DuckHaven's session API.

The package root *is* the DB-API module: it exposes ``connect`` plus the mandated
globals, exception hierarchy, and type objects.

    from duckhaven_sql_connector import connect

    with connect(host=..., workspace=..., token="dh_pat_...") as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        print(cur.fetchall())
"""

# Resolved first so submodules importing ``__version__`` during the import chain below
# see it already set (client.py builds its User-Agent from it).
try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # pragma: no cover - source checkout without a build
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("duckhaven-sql-connector")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0+unknown"

from ._telemetry import Hooks
from .config import ClientConfig, RetryPolicy
from .connection import Connection, StagedFile, StagingFiles, connect
from .cursor import Cursor
from .dbapi import (
    BINARY,
    DATETIME,
    NUMBER,
    ROWID,
    STRING,
    Binary,
    DatabaseError,
    DataError,
    Date,
    DateFromTicks,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    MaxRetryDurationError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Time,
    TimeFromTicks,
    Timestamp,
    TimestampFromTicks,
    Warning,
    apilevel,
    paramstyle,
    threadsafety,
)

__all__ = [
    "__version__",
    # entry point + objects
    "connect",
    "Connection",
    "Cursor",
    "ClientConfig",
    "RetryPolicy",
    "StagedFile",
    "StagingFiles",
    "Hooks",
    # globals
    "apilevel",
    "threadsafety",
    "paramstyle",
    # exceptions
    "Warning",
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "MaxRetryDurationError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
    # type objects + constructors
    "STRING",
    "BINARY",
    "NUMBER",
    "DATETIME",
    "ROWID",
    "Date",
    "Time",
    "Timestamp",
    "DateFromTicks",
    "TimeFromTicks",
    "TimestampFromTicks",
    "Binary",
]
