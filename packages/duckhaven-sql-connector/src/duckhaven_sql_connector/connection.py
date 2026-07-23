"""The DB-API ``Connection`` — one DuckHaven SQL session — and the ``connect`` entry.

``connect`` opens a session (``POST …/sql/sessions``, which blocks server-side until the
agent has attached a DuckDB connection) and returns a Connection pinned to that session
and its agent. ``close`` deletes the session. If the session is reaped, hits its
max-lifetime, or its agent disconnects, the next statement gets a 409 → OperationalError
and the connection is marked dead; the caller opens a new one.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._params import quote_identifier
from ._telemetry import Hooks
from .client import Transport
from .config import ClientConfig, RetryPolicy
from .cursor import Cursor
from .dbapi import OperationalError, ProgrammingError


@dataclass(frozen=True)
class ServerVersion:
    """What a DuckHaven server reports about itself (``GET /api/version``).

    ``version`` is the release/build version (the git tag the image was built from);
    ``api_version`` is the API contract version, an integer bumped only on a breaking
    change. Additive changes (a new field, a newly admitted statement) move neither, so
    this is provenance and a coarse compatibility signal — not a feature list.
    """

    version: str
    api_version: int


@dataclass(frozen=True)
class StagedFile:
    """A presigned staging file: upload to ``put_url`` (HTTP PUT), read from ``get_url``.

    ``key`` is the assigned object-storage location (``s3://…`` / ``abfss://…``) under the
    session's staging prefix. The URLs are opaque, short-lived, and backend-agnostic."""

    name: str
    key: str
    put_url: str
    get_url: str


@dataclass(frozen=True)
class StagingFiles:
    """The presigned files for one staging request, and when the URLs expire."""

    files: list[StagedFile]
    expires_at: str | None = None


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
        # Live cursors, so a connection-scoped cancel() can reach the in-flight
        # statement. Weak so a finished cursor is not kept alive by this set.
        self._cursors: weakref.WeakSet[Cursor] = weakref.WeakSet()

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
        cursor = Cursor(self)
        self._cursors.add(cursor)
        return cursor

    def cancel(self) -> None:
        """Best-effort cancel of the in-flight statement on this session.

        DB-API has no cancel, but dbt drives one connection per thread and aborts a
        run by cancelling the *other* threads' connections. Statements run serially
        per session, so at most one cursor has a live statement; cancelling each known
        cursor covers it, and a cursor whose statement already finished is a no-op.
        """
        for cursor in list(self._cursors):
            cursor.cancel()

    # -- Server introspection -----------------------------------------------

    def server_version(self) -> ServerVersion | None:
        """The server's release and API-contract version (``GET /api/version``).

        Returns ``None`` against a server predating the endpoint, which answers 404 — by
        the server's own contract that means "assume the oldest supported behaviour". The
        call is independent of the session, so it still answers after the session has gone
        dead (useful for diagnostics); it raises only once the connection itself is closed.
        """
        if self._closed:
            raise ProgrammingError("connection is closed")
        try:
            response = self._transport.get("/version")
        except ProgrammingError as exc:
            if exc.status_code == 404:
                return None
            raise
        data = response.json()
        return ServerVersion(version=data["version"], api_version=data["api_version"])

    # -- Staging ------------------------------------------------------------

    def stage_files(self, names: Sequence[str]) -> StagingFiles:
        """Presign a PUT (upload) and GET (read) URL per file under this session's stage
        (``POST …/sql/sessions/{id}/staging-files``).

        A client (e.g. the dlt ``duckhaven`` destination) uploads bulk data to each
        ``put_url`` with a plain HTTP PUT, then issues a load command that reads the
        ``get_url`` through the session — the agent runs it over httpfs, no storage secret.
        ``names`` are bare file names (no path separators). A reaped/closed session answers
        409 → the connection is marked dead (open a new one), mirroring statement execution.
        """
        self._ensure_usable()
        try:
            response = self._transport.post(
                f"/sql/sessions/{self._session_id}/staging-files",
                json={"files": list(names)},
            )
        except OperationalError as exc:
            if exc.status_code == 409:
                self._mark_dead()
            raise
        data = response.json()
        return StagingFiles(
            files=[
                StagedFile(name=f["name"], key=f["key"], put_url=f["put_url"], get_url=f["get_url"])
                for f in data["files"]
            ],
            expires_at=data.get("expires_at"),
        )

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
    application: str | None = None,
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
        application=application,
    )
    return Connection.open(config, hooks=hooks)
