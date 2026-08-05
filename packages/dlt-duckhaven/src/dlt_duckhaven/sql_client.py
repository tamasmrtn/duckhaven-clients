"""``DuckHavenSqlClient`` — a dlt ``SqlClientBase`` backed by the DuckHaven SQL session.

Wraps ``duckhaven-sql-connector``: opening the client opens a DuckHaven session, and
statements run through the session's DB-API cursor. The session is autocommit (each
statement commits via Polaris), so transaction control is a no-op. Tables are addressed by
fully-qualified ``catalog.schema.table`` names, so the client does not depend on a current
schema being set (the dataset schema may not exist yet on first load).
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, AnyStr, ClassVar

from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.dataset import DBApiCursor
from dlt.destinations.exceptions import (
    DatabaseTerminalException,
    DatabaseTransientException,
    DatabaseUndefinedRelation,
)
from dlt.destinations.sql_client import (
    DBApiCursorImpl,
    SqlClientBase,
    raise_database_error,
    raise_open_connection_error,
)
from dlt.destinations.typing import DBApi, DBTransaction

import duckhaven_sql_connector as dbapi
from dlt_duckhaven.configuration import DuckHavenClientConfiguration

if TYPE_CHECKING:
    from duckhaven_sql_connector import Connection

# DuckHaven returns rows as JSON, so a temporal value arrives as an ISO-8601 string. dlt
# (like any DB-API consumer) expects datetime objects for timestamp columns — e.g. it does
# `pendulum.instance(row)` on `_dlt_version.inserted_at` — so those have to be converted
# back on the way out.
#
# Which columns to convert is decided from the types the server reports (the connector
# surfaces them as PEP 249 `type_code`). Guessing from the value's *shape* was the only
# option before the server reported types, and it is wrong in a way that silently corrupts
# data: a genuine VARCHAR column holding "2024-05-06T07:08:09Z" — a perfectly ordinary
# string to store — would land in the destination as a datetime. The regex below is kept
# only as the fallback for a server that reports no types.
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$"
)

# DuckDB spellings whose values arrive as ISO-8601 text: DATE, TIME, TIME WITH TIME ZONE,
# TIMESTAMP, TIMESTAMP WITH TIME ZONE, TIMESTAMP_MS/_NS/_S. Matching on the prefix covers
# the parameterized and aliased forms without enumerating them.
_TEMPORAL_PREFIXES = ("DATE", "TIME")


# Marker of a Polaris optimistic-concurrency rejection, i.e. two writers committing to the
# same Iceberg table at once and the loser being told to start over from the refreshed
# metadata. Polaris words the detail several ways ("has been concurrently modified",
# "TARGET_ENTITY_CONCURRENTLY_MODIFIED", "branch main was created concurrently"), but every
# shape names this exception type, so it is what to match on rather than any one wording.
#
# Deliberately narrower than "any Polaris 409": a concurrent CREATE TABLE loses with
# AlreadyExistsException, also a 409, and retrying that can never succeed — the table it
# collided with is still there. The 409 is between the agent and Polaris either way, so it
# never reaches us as an HTTP status; it arrives as the failed statement's error text.
_COMMIT_CONFLICT_MARKER = "commitfailedexception"


def is_commit_conflict(exc: BaseException) -> bool:
    """Whether ``exc`` is a losing Iceberg commit that is worth retrying.

    A rejected commit publishes no metadata, so none of its rows become visible and
    re-running the statement cannot duplicate them.
    """
    return _COMMIT_CONFLICT_MARKER in str(exc).lower()


# dlt runs load jobs in parallel (20 workers by default), so one resource whose data spans
# several Parquet files is enough for two statements to commit to the same table at once.
# Retrying the statement rather than letting dlt retry the whole job keeps the staged
# Parquet upload out of the loop (dlt re-runs the job from the top, re-uploading it), and
# reaches the commits dlt would not retry at all: `complete_load`'s writes to _dlt_loads and
# _dlt_version happen outside any job, so a conflict there fails the whole run.
_COMMIT_CONFLICT_ATTEMPTS = 5
_BACKOFF_BASE = 0.2
_BACKOFF_MAX = 5.0


def _backoff_delay(attempt: int) -> float:
    """Capped exponential backoff with full jitter for retry ``attempt`` (0-based).

    Jitter matters more than the curve here: the racing statements were started together,
    so a fixed delay would just line them up to collide again.
    """
    return random.uniform(0, min(_BACKOFF_MAX, _BACKOFF_BASE * (2**attempt)))


def _is_temporal(type_code: Any) -> bool:
    return isinstance(type_code, str) and type_code.upper().startswith(_TEMPORAL_PREFIXES)


def _to_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # A DATE or TIME is not a full datetime; leave anything unparseable alone.
        return value


def _coerce_value(value: Any) -> Any:
    """Shape-based fallback, for a server that reports no column types."""
    if not isinstance(value, str) or not _ISO_DATETIME_RE.match(value):
        return value
    return _to_datetime(value)


class _DuckHavenCursorImpl(DBApiCursorImpl):
    """Cursor that restores ``datetime`` objects for temporal columns on fetch.

    Type-directed when the server reports column types, so a VARCHAR holding an
    ISO-8601 string is left as the string it is; shape-based only as a fallback.
    """

    def _temporal_mask(self) -> list[bool] | None:
        """Per-column "this is a temporal column" flags, or None if types are unknown."""
        description = self.native_cursor.description
        if not description:
            return None
        mask = [_is_temporal(col[1]) for col in description]
        # An older server leaves every type_code None; fall back to sniffing.
        return mask if any(col[1] is not None for col in description) else None

    def _coerce_row(self, row: tuple[Any, ...] | None, mask: list[bool] | None) -> Any:
        if row is None:
            return None
        if mask is None:
            return tuple(_coerce_value(v) for v in row)
        return tuple(_to_datetime(v) if i < len(mask) and mask[i] else v for i, v in enumerate(row))

    def fetchone(self, *args: Any, **kwargs: Any) -> Any:
        return self._coerce_row(self.native_cursor.fetchone(*args, **kwargs), self._temporal_mask())

    def fetchmany(self, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        mask = self._temporal_mask()
        return [self._coerce_row(r, mask) for r in self.native_cursor.fetchmany(*args, **kwargs)]

    def fetchall(self, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        mask = self._temporal_mask()
        return [self._coerce_row(r, mask) for r in self.native_cursor.fetchall(*args, **kwargs)]


class DuckHavenSqlClient(SqlClientBase["Connection"]):
    dbapi: ClassVar[DBApi] = dbapi

    def __init__(
        self,
        dataset_name: str,
        staging_dataset_name: str,
        config: DuckHavenClientConfiguration,
        capabilities: DestinationCapabilitiesContext,
    ) -> None:
        # The DuckHaven catalog is the "database" that qualifies catalog.schema.table.
        super().__init__(config.catalog, dataset_name, staging_dataset_name, capabilities)
        self.config = config
        self._conn: Connection | None = None

    @raise_open_connection_error
    def open_connection(self) -> Connection:
        from dlt_duckhaven import __version__

        self._conn = dbapi.connect(
            host=self.config.host,
            workspace=self.config.workspace,
            token=self.config.credentials.token,
            agent=self.config.agent,
            catalog=self.config.catalog,
            # Rely on fully-qualified names; do not USE a schema that may not exist yet.
            schema=None,
            application=f"dlt-duckhaven/{__version__}",
        )
        return self._conn

    def close_connection(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def native_connection(self) -> Connection:
        return self._conn

    def catalog_name(self, quote: bool = True, casefold: bool = True) -> str | None:
        catalog = self.config.catalog
        if catalog is None:
            return None
        if casefold:
            catalog = self.capabilities.casefold_identifier(catalog)
        if quote:
            catalog = self.capabilities.escape_identifier(catalog)
        return catalog

    @contextmanager
    @raise_database_error
    def begin_transaction(self) -> Iterator[DBTransaction]:
        # The DuckHaven session is autocommit; BEGIN/COMMIT are connector-side no-ops.
        yield self

    @raise_database_error
    def commit_transaction(self) -> None:
        pass

    @raise_database_error
    def rollback_transaction(self) -> None:
        raise NotImplementedError("DuckHaven sessions are autocommit; cannot roll back.")

    @raise_database_error
    def has_dataset(self) -> bool:
        """Check schema existence via the workspace catalog API, not INFORMATION_SCHEMA.

        INFORMATION_SCHEMA.SCHEMATA is engine-side enumeration, and DuckHaven rejects it
        (403) once *any* catalog in the workspace is attached scoped -- the denial applies
        to every session in that workspace, so it could break a load into an unrelated open
        catalog too. The connector's ``schemas()`` cursor method reads the REST browse
        endpoint instead, which filters by grant and works under any attachment mode -- the
        same reasoning as ``get_storage_tables`` using ``DESCRIBE`` for columns.
        """
        schema_name = self.make_qualified_table_name_path(None, quote=False)[-1]
        cursor = self._conn.cursor()
        try:
            cursor.schemas(catalog=self.config.catalog, schema_name=schema_name)
            return len(cursor.fetchall()) > 0
        finally:
            cursor.close()

    def execute_sql(self, sql: AnyStr, *args: Any, **kwargs: Any) -> Sequence[Sequence[Any]] | None:
        """Run ``sql``, retrying a lost Iceberg commit race.

        Only commit conflicts are retried: they are resolved by re-reading the table's
        refreshed metadata, which is exactly what re-running the statement does. Every other
        transient failure (a reaped session, an agent that went away) leaves this connection
        unusable, so it propagates — dlt retries the job against a fresh one.
        """
        for attempt in range(_COMMIT_CONFLICT_ATTEMPTS):
            try:
                with self.execute_query(sql, *args, **kwargs) as cursor:
                    if cursor.description is None:
                        return None
                    return cursor.fetchall()
            except DatabaseTransientException as ex:
                if not is_commit_conflict(ex) or attempt == _COMMIT_CONFLICT_ATTEMPTS - 1:
                    # Still transient, so dlt's own bounded retry stays the outer net.
                    raise
                time.sleep(_backoff_delay(attempt))
        raise AssertionError("unreachable: the loop either returns or raises")

    @contextmanager
    @raise_database_error
    def execute_query(self, query: AnyStr, *args: Any, **kwargs: Any) -> Iterator[DBApiCursor]:
        assert isinstance(query, str)
        cursor = self._conn.cursor()
        try:
            if args:
                # dlt emits pyformat (%s); the connector binds qmark (?) params.
                cursor.execute(query.replace("%s", "?"), list(args))
            else:
                # Multi-statement DDL arrives as one string; the session runs one
                # statement per submit, so split (params never accompany these).
                for part in query.split(";"):
                    if part.strip():
                        cursor.execute(part)
            yield _DuckHavenCursorImpl(cursor)
        finally:
            cursor.close()

    @staticmethod
    def _make_database_exception(ex: Exception) -> Exception:
        if isinstance(ex, dbapi.ProgrammingError):
            # Checked before the undefined-relation match: a conflict message names the
            # table it could not commit to, so a future Polaris wording could plausibly
            # contain "not found" and be misread as a missing table.
            if is_commit_conflict(ex):
                return DatabaseTransientException(ex)
            message = str(ex).lower()
            if any(s in message for s in ("does not exist", "not found", "catalog error")):
                return DatabaseUndefinedRelation(ex)
            return DatabaseTerminalException(ex)
        if isinstance(ex, dbapi.OperationalError):
            # Reaped/closed session, unavailable agent, or timeout — retryable.
            return DatabaseTransientException(ex)
        if isinstance(ex, dbapi.DatabaseError):
            return DatabaseTerminalException(ex)
        return ex
