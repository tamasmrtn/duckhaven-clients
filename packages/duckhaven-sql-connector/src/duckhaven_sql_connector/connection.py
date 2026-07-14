"""The DB-API ``Connection`` — one DuckHaven SQL session — and the ``connect`` entry.

``connect`` opens a session (``POST …/sql/sessions``, which blocks server-side until the
agent has attached a DuckDB connection) and returns a Connection pinned to that session
and its agent. ``close`` deletes the session. If the session is reaped, hits its
max-lifetime, or its agent disconnects, the next statement gets a 409 → OperationalError
and the connection is marked dead; the caller opens a new one.
"""

from __future__ import annotations

from typing import Any

from ._params import quote_identifier
from ._telemetry import Hooks
from .client import Transport
from .config import ClientConfig, RetryPolicy
from .cursor import Cursor
from .dbapi import OperationalError, ProgrammingError


class Connection:
    def __init__(
        self,
        transport: Transport,
        config: ClientConfig,
        *,
        session_id: str,
        agent_id: str | None = None,
        staging_uri: str | None = None,
        active_catalog: str | None = None,
    ) -> None:
        self._transport = transport
        self._config = config
        self._session_id = session_id
        self.agent_id = agent_id
        self.staging_uri = staging_uri
        self.active_catalog = active_catalog
        self._closed = False
        self._dead = False

    @classmethod
    def open(
        cls,
        config: ClientConfig,
        *,
        transport: Transport | None = None,
        hooks: Hooks | None = None,
    ) -> Connection:
        transport = transport or Transport(config, hooks=hooks)
        body: dict[str, Any] = {}
        if config.agent is not None:
            body["agent_id"] = config.agent
        if config.catalog is not None:
            body["catalog"] = config.catalog
        try:
            response = transport.post(f"/workspaces/{config.workspace}/sql/sessions", json=body)
            data = response.json()
        except Exception:
            transport.close()
            raise
        conn = cls(
            transport,
            config,
            session_id=data["id"],
            agent_id=data.get("agent_id"),
            staging_uri=data.get("staging_uri"),
            active_catalog=data.get("active_catalog"),
        )
        conn._apply_defaults()
        return conn

    # -- Cursors ------------------------------------------------------------

    def cursor(self) -> Cursor:
        self._ensure_usable()
        return Cursor(self)

    # -- Transactions (autocommit session; documented no-ops) ---------------

    def commit(self) -> None:
        """No-op: the session is autocommit. Use explicit BEGIN/COMMIT via execute()."""

    def rollback(self) -> None:
        """No-op: the session is autocommit. Use explicit ROLLBACK via execute()."""

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if not self._dead:
                self._transport.delete(f"/sql/sessions/{self._session_id}")
        finally:
            self._transport.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Internals ----------------------------------------------------------

    def _apply_defaults(self) -> None:
        if not self._config.schema:
            return
        schema = quote_identifier(self._config.schema)
        target = (
            f"{quote_identifier(self.active_catalog)}.{schema}" if self.active_catalog else schema
        )
        cursor = self.cursor()
        cursor.execute(f"USE {target}")
        cursor.close()

    def _mark_dead(self) -> None:
        self._dead = True

    def _ensure_usable(self) -> None:
        if self._closed:
            raise ProgrammingError("connection is closed")
        if self._dead:
            raise OperationalError("session is no longer open; open a new connection")


def connect(
    host: str,
    workspace: str,
    token: str,
    *,
    agent: str | None = None,
    catalog: str | None = None,
    schema: str | None = None,
    timeout: float = 600.0,
    http_timeout: float = 60.0,
    tls_verify: bool = True,
    retry: RetryPolicy | None = None,
    hooks: Hooks | None = None,
) -> Connection:
    """Open a DuckHaven SQL session and return a DB-API 2.0 Connection."""
    config = ClientConfig(
        host=host,
        workspace=workspace,
        token=token,
        agent=agent,
        catalog=catalog,
        schema=schema,
        timeout=timeout,
        http_timeout=http_timeout,
        tls_verify=tls_verify,
        retry=retry or RetryPolicy(),
    )
    return Connection.open(config, hooks=hooks)
