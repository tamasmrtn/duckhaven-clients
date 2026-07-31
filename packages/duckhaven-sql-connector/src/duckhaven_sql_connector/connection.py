"""The DB-API ``Connection`` — one DuckHaven SQL session — and the ``connect`` entry.

``connect`` opens a session (``POST …/sql/sessions``, which blocks server-side until the
agent has attached a DuckDB connection) and returns a Connection pinned to that session
and its agent. ``close`` deletes the session. If the session is reaped, hits its
max-lifetime, or its agent disconnects, the next statement gets a 409 → OperationalError
and the connection is marked dead; the caller opens a new one.

A deployment running elastic compute can legitimately have nothing running when a client
connects. The server then parks the session and starts an agent, and ``Connection.open``
waits that out (up to ``compute_wait``) instead of failing — see ``_open_session``.
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
from .dbapi import MaxRetryDurationError, OperationalError, ProgrammingError

# The server-side wait we ask for on session open. Deliberately small and chosen by us:
# the server's own budget is operator-tunable (up to 120s), and a block longer than
# ``http_timeout`` would abort the request socket-side while the server went on to open
# the session — orphaning one nobody holds. Bounding it here removes that entirely, and
# ``_await_open`` carries the rest of a cold start by polling.
_OPEN_WAIT_TIMEOUT_S = 10.0

# Poll cadence while compute starts. A cold start runs to tens of seconds, so
# sub-second polling would only burn requests to no purpose.
_SESSION_POLL_START = 1.0
_SESSION_POLL_MAX = 5.0

# Fallback wait when the server says to retry but sends no Retry-After.
_COMPUTE_RETRY_DELAY_S = 5.0

# Statuses a session passes through before it can run statements. Anything else is
# terminal: the session will never open, so waiting on it is pointless.
_SESSION_NOT_READY = ("pending", "opening")


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
        try:
            data = cls._open_session(transport, config)
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

    @classmethod
    def _open_session(cls, transport: Transport, config: ClientConfig) -> dict[str, Any]:
        """Open the session, waiting out an elastic cold start, and return the open row.

        Against warm compute this is the single POST it has always been. When DuckHaven
        has to start an agent first it parks the session and — because we ask for
        ``on_wait_timeout="continue"`` — hands it straight back ``202`` still ``pending``,
        so the waiting happens here rather than failing the connect.

        Returns the session as the server last described it, which is *not* necessarily
        the body of the original response: a pending session carries no ``agent_id``
        until an agent claims it.
        """
        path = f"/workspaces/{config.workspace}/sql/sessions"
        body: dict[str, Any] = {}
        if config.agent is not None:
            body["agent_id"] = config.agent
        if config.catalog is not None:
            body["catalog"] = config.catalog
        if config.compute_wait > 0:
            # Ask to be handed the pending session rather than a 503. Both fields are
            # optional and ignored by a server that predates them, which then behaves
            # exactly as it does today.
            body["wait_timeout_s"] = _OPEN_WAIT_TIMEOUT_S
            body["on_wait_timeout"] = "continue"

        deadline = transport._monotonic() + config.compute_wait
        attempt = 0
        while True:
            try:
                response = transport.post(path, json=body)
            except OperationalError as exc:
                # The server gave up holding the request but kept the compute it started,
                # so re-posting is the sanctioned way to wait: the retry lands on the
                # agent already coming up. Reached only when `continue` did not take
                # effect — a gateway dropping the unknown field, say — since a cold pool
                # normally answers 202. Every other 503, including the plain "no agent
                # available" of a server without elastic compute, raises as before:
                # retrying those would never produce an agent.
                if exc.code != "compute_starting":
                    raise
                delay = exc.retry_after if exc.retry_after is not None else _COMPUTE_RETRY_DELAY_S
                if transport._monotonic() + delay > deadline:
                    raise
                attempt += 1
                transport._on_retry("POST", path, attempt)
                transport._sleep(delay)
                continue

            session = response.json()
            if session.get("status") == "open":
                return session
            return cls._await_open(transport, config, session, deadline)

    @staticmethod
    def _await_open(
        transport: Transport, config: ClientConfig, session: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        """Poll a session the server handed back unopened until it opens, or raise.

        Checked before the first sleep so a session that is already open costs no extra
        request, and stopped the moment the status turns terminal — the server records
        *why* in ``error`` (``compute_unavailable``, ``provisioning_timeout``,
        ``open_timeout``), which is more use to the caller than a timeout would be.
        """
        session_id = session["id"]
        delay = _SESSION_POLL_START
        while True:
            status = session.get("status")
            if status == "open":
                return session
            if status not in _SESSION_NOT_READY:
                reason = session.get("error") or "no reason recorded"
                raise OperationalError(f"session {status}: {reason}", code=session.get("error"))
            if transport._monotonic() + delay > deadline:
                raise MaxRetryDurationError(
                    f"compute did not become available within {config.compute_wait}s "
                    f"(session is {status})"
                )
            transport._sleep(delay)
            delay = min(delay * 2, _SESSION_POLL_MAX)
            session = transport.get(f"/sql/sessions/{session_id}").json()

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
    compute_wait: float = 300.0,
    application: str | None = None,
    hooks: Hooks | None = None,
) -> Connection:
    """Open a DuckHaven SQL session and return a DB-API 2.0 Connection.

    ``compute_wait`` is how long to wait if the server has to start elastic compute
    before it can open the session; 0 fails immediately instead.
    """
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
        compute_wait=compute_wait,
        application=application,
    )
    return Connection.open(config, hooks=hooks)
