"""The DuckHaven connection manager.

Identical to dbt-duckdb's manager except that it builds a :class:`DuckHavenEnvironment`
directly. dbt-duckdb's ``environments.create()`` factory dispatches only on
``remote``/``is_motherduck``, with no config hook — so overriding ``open`` is the one
place to inject a custom environment.
"""

from __future__ import annotations

import threading

from dbt.adapters.contracts.connection import Connection, ConnectionState
from dbt.adapters.duckdb.connections import DuckDBConnectionManager
from dbt.adapters.events.logging import AdapterLogger
from dbt.adapters.exceptions import FailedToConnectError

from .environments import DuckHavenEnvironment

logger = AdapterLogger("DuckHaven")


class DuckHavenConnectionManager(DuckDBConnectionManager):
    TYPE = "duckhaven"
    # Own the shared-environment cache so we never clobber dbt-duckdb's.
    _LOCK = threading.RLock()
    _ENV = None
    _LOGGED_MESSAGES: set[str] = set()

    @classmethod
    def open(cls, connection: Connection) -> Connection:
        if connection.state == ConnectionState.OPEN:
            logger.debug("Connection is already open, skipping open.")
            return connection

        credentials = cls.get_credentials(connection.credentials)
        with cls._LOCK:
            try:
                if not cls._ENV or cls._ENV.creds != credentials:
                    cls._ENV = DuckHavenEnvironment(credentials)
                connection.handle = cls._ENV.handle()
                connection.state = ConnectionState.OPEN
            except Exception as e:  # noqa: BLE001 - surfaced as FailedToConnectError below
                logger.debug(f"Error opening a DuckHaven session: {e}")
                connection.handle = None
                connection.state = ConnectionState.FAIL
                raise FailedToConnectError(str(e))
            return connection
