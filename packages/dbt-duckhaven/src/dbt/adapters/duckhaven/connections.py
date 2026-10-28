"""The DuckHaven connection manager.

Like dbt-duckdb's manager, except that it builds a :class:`DuckHavenEnvironment`
directly, and keeps each thread's DuckHaven session open from one node to the next.
dbt-duckdb's ``environments.create()`` factory dispatches only on
``remote``/``is_motherduck``, with no config hook — so overriding ``open`` is the one
place to inject a custom environment.
"""

from __future__ import annotations

from typing import Any

from dbt_common.exceptions import DbtRuntimeError

from dbt.adapters.contracts.connection import Connection, ConnectionState
from dbt.adapters.duckdb.connections import DuckDBConnectionManager
from dbt.adapters.events.logging import AdapterLogger
from dbt.adapters.exceptions import FailedToConnectError
from duckhaven_sql_connector import OperationalError

from .environments import DuckHavenEnvironment

logger = AdapterLogger("DuckHaven")


def _session_lost(exc: BaseException | None) -> bool:
    """Whether the server no longer holds the session (reaped, expired, agent gone).

    The connector raises this 409 when a statement is *submitted*, before anything runs,
    so re-submitting the statement on a fresh session cannot run it twice.
    """
    return isinstance(exc, OperationalError) and exc.status_code == 409


class DuckHavenConnectionManager(DuckDBConnectionManager):
    TYPE = "duckhaven"

    @classmethod
    def open(cls, connection: Connection) -> Connection:
        if connection.state == ConnectionState.OPEN:
            logger.debug("Connection is already open, skipping open.")
            return connection

        credentials = cls.get_credentials(connection.credentials)
        # Store the environment on DuckDBConnectionManager (the base), not on this
        # subclass: dbt-duckdb's impl.py reads DuckDBConnectionManager.env() directly in
        # several @available helpers (e.g. get_binding_char, which seeds use), so the env
        # must live there or those calls raise "environment requested before creation".
        base = DuckDBConnectionManager
        with base._LOCK:
            try:
                if not base._ENV or base._ENV.creds != credentials:
                    base._ENV = DuckHavenEnvironment(credentials)
                connection.handle = base._ENV.handle()
                connection.state = ConnectionState.OPEN
            except Exception as e:  # noqa: BLE001 - surfaced as FailedToConnectError below
                logger.debug(f"Error opening a DuckHaven session: {e}")
                connection.handle = None
                connection.state = ConnectionState.FAIL
                raise FailedToConnectError(str(e))
            return connection

    def release(self) -> None:
        """Keep the thread's session open for its next node.

        dbt-core closes the connection after every node. For dbt-duckdb that is a cheap
        cursor, but here it is a whole DuckHaven session, and opening one is the most
        expensive thing a node does. So the thread's connection stays open, dbt reuses
        it for the thread's next node, and ``cleanup_all()`` closes every session when
        the invocation ends.
        """
        with self.lock:
            connection = self.get_if_exists()
        if connection is not None and connection.transaction_open:
            # Not expected — the connector's BEGIN/COMMIT are no-ops and every node
            # commits — but a reused connection must not carry dbt's transaction flag
            # into the next node, so close this one as dbt-core would.
            super().release()

    def add_query(
        self,
        sql: str,
        auto_begin: bool = True,
        bindings: Any | None = None,
        abridge_sql_log: bool = False,
        retryable_exceptions: tuple[type[Exception], ...] = (),
        retry_limit: int = 1,
    ) -> tuple[Connection, Any]:
        """Run ``sql``, reopening the thread's session once if the server dropped it.

        A reused session can outlive the server's idle timeout or max lifetime, or its
        agent can restart, between two nodes. If a second session is lost too, the
        connection is closed so the thread's next node starts from a fresh one.
        """
        args = (sql, auto_begin, bindings, abridge_sql_log, retryable_exceptions, retry_limit)
        try:
            return super().add_query(*args)
        except DbtRuntimeError as exc:
            if not _session_lost(exc.__cause__):
                raise
        connection = self.get_thread_connection()
        logger.debug(f"DuckHaven session for {connection.name} is gone; opening a new one.")
        try:
            # The connector already marked the session dead: close() sends no DELETE.
            connection.handle.close()
        except Exception as e:  # noqa: BLE001 - the old session is gone either way
            logger.debug(f"Ignoring error while closing a lost DuckHaven session: {e}")
        connection.state = ConnectionState.INIT
        self.open(connection)
        try:
            return super().add_query(*args)
        except DbtRuntimeError as exc:
            if _session_lost(exc.__cause__):
                self.close(connection)
            raise

    def commit_if_has_connection(self) -> None:
        """Commit only if a transaction is actually open.

        ``BaseConnectionManager.commit_if_has_connection`` calls ``commit()``
        unconditionally whenever a connection exists, but ``SQLConnectionManager.commit``
        raises ``DbtInternalError`` if ``transaction_open`` is False. Under dbt's threaded
        execution this fires on effectively every model — DuckDB has already closed the
        transaction by the time this cleanup pass runs — and dbt-duckdb's
        ``DuckDBAdapter.commit_if_has_connection`` only downgrades it to a warning log
        rather than avoiding it. Checking the same flag ``commit()`` checks means the
        benign case never raises in the first place, while a commit that fails for a real
        reason (``transaction_open`` still True) is untouched and still surfaces through
        that upstream warning path.
        """
        connection = self.get_if_exists()
        if connection and connection.transaction_open:
            self.commit()
