"""``DuckHavenSqlClient`` — a dlt ``SqlClientBase`` backed by the DuckHaven SQL session.

Wraps ``duckhaven-sql-connector``: opening the client opens a DuckHaven session, and
statements run through the session's DB-API cursor. The session is autocommit (each
statement commits via Polaris), so transaction control is a no-op. Tables are addressed by
fully-qualified ``catalog.schema.table`` names, so the client does not depend on a current
schema being set (the dataset schema may not exist yet on first load).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
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

    def execute_sql(self, sql: AnyStr, *args: Any, **kwargs: Any) -> Sequence[Sequence[Any]] | None:
        with self.execute_query(sql, *args, **kwargs) as cursor:
            if cursor.description is None:
                return None
            return cursor.fetchall()

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
            yield DBApiCursorImpl(cursor)
        finally:
            cursor.close()

    @staticmethod
    def _make_database_exception(ex: Exception) -> Exception:
        if isinstance(ex, dbapi.ProgrammingError):
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
