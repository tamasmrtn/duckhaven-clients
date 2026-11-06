"""The DB-API ``Connection`` and the ``connect`` entry.

In the default ``session`` mode a Connection is one DuckHaven SQL session: ``connect``
opens it (``POST …/sql/sessions``, which blocks server-side until the agent has attached a
DuckDB connection) and pins the Connection to it and its agent. ``close`` deletes the
session. If the session is reaped, hits its max-lifetime, or its agent disconnects, the
next statement gets a 409 → OperationalError and the connection is marked dead; the caller
opens a new one.

In ``auto`` mode ``connect`` opens nothing. Each statement runs as a one-shot query
(``POST …/queries``), which needs no session and so works where SQL sessions are disabled.
The first statement that needs session state opens a session, and the connection stays on
it from then on — see ``_routes_to_session``.

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
from .cursor import Cursor, _needs_session
from .dbapi import MaxRetryDurationError, NotSupportedError, OperationalError, ProgrammingError

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

# The `features` entry that says one-shot queries honour `schema`. A server without it
# ignores the field and resolves names in its default schema instead.
_QUERY_SCHEMA = "query_schema"


@dataclass(frozen=True)
class ServerVersion:
    """What a DuckHaven server reports about itself (``GET /api/version``).

    ``version`` is the release/build version (the git tag the image was built from);
    ``api_version`` is the API contract version, an integer bumped only on a breaking
    change. Additive changes (a new field, a newly admitted statement) move neither.
    ``features`` names the few additions an older server would ignore silently rather
    than refuse, so they cannot be relied on without asking; it is empty against a
    server that predates the list.
    """

    version: str
    api_version: int
    features: frozenset[str] = frozenset()


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
        session_id: str | None,
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
        # What GET /version said, learned on first need; see `_server`.
        self._server_info: ServerVersion | None | bool = False

    @classmethod
    def open(
        cls,
        config: ClientConfig,
        *,
        transport: Transport | None = None,
        hooks: Hooks | None = None,
    ) -> Connection:
        transport = transport or Transport(config, hooks=hooks)
        if config.mode == "auto":
            # Nothing to open: the first statement is the first request.
            return cls(transport, config, session_id=None)
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
        if config.use_cache is not None:
            body["use_cache"] = config.use_cache
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

    def cancel_query(self, query_id: str) -> None:
        """Cancel a run by its id (``DELETE /queries/{id}``), from any thread.

        Needs no session, and works for any run in the workspace, so a tool can stop a
        statement it started on another connection, or one whose ``execute`` is still
        blocked, by the :attr:`Cursor.query_id` it recorded. A run that already finished
        is left as it is. An unknown id raises ``ProgrammingError``.
        """
        if self._closed:
            raise ProgrammingError("connection is closed")
        self._transport.delete(f"/queries/{query_id}")

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

    def ping(self) -> None:
        """Check the connection can still run statements, without running one.

        Asks the server who the token belongs to, which fails on a wrong, expired or
        revoked token, and on a connection with a session, that the session is still
        open. Raises ``OperationalError`` (and marks the connection dead) when it is not,
        ``InterfaceError`` for a rejected token, and whatever reaching the server raised.
        """
        self._ensure_usable()
        if self._session_id is None:
            self._transport.get("/me")
            return
        try:
            session = self._transport.get(f"/sql/sessions/{self._session_id}").json()
        except ProgrammingError as exc:
            if exc.status_code != 404:
                raise
            session = {"status": "gone"}
        if session.get("status") != "open":
            self._mark_dead()
            raise OperationalError(
                f"session is {session.get('status')}; open a new connection",
                code=session.get("error"),
            )

    def metadata(
        self,
        *,
        catalog: str | None = None,
        schema: str | None = None,
        table: str | None = None,
        columns: bool = False,
    ) -> list[dict[str, Any]]:
        """The workspace's catalogs, schemas and tables that this token may see
        (``GET /workspaces/{workspace}/metadata``), filtered by the given names.

        Returns the server's ``catalogs`` list: each catalog has ``name``, ``kind``,
        ``access_mode`` and ``schemas``; each schema ``name`` and ``tables``; each table
        ``name``, ``table_type`` and, with ``columns=True``, ``columns`` of ``name``,
        ``type`` and ``nullable``. ``type`` is DuckDB's spelling; parse it with
        :func:`duckhaven_sql_connector.types.parse_type`. Listing reads no data and
        runs nothing on an agent. A server caps how many tables it reports columns for
        in one call, so narrow ``columns=True`` with ``catalog``, ``schema`` or ``table``.
        """
        self._ensure_usable()
        params: dict[str, Any] = {"columns": "true" if columns else "false"}
        for key, value in (("catalog", catalog), ("schema", schema), ("table", table)):
            if value is not None:
                params[key] = value
        response = self._transport.get(
            f"/workspaces/{self._config.workspace}/metadata", params=params
        )
        return response.json()["catalogs"]

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
        return ServerVersion(
            version=data["version"],
            api_version=data["api_version"],
            features=frozenset(data.get("features") or ()),
        )

    def _server(self) -> ServerVersion | None:
        """What the server reports about itself, asked once per connection. ``None``
        for a server too old to say, which is the oldest supported contract."""
        if self._server_info is False:
            self._server_info = self.server_version()
        return self._server_info  # type: ignore[return-value]

    def _api_version(self) -> int | None:
        """The server's API contract version; ``None`` for a server too old to say."""
        server = self._server()
        return None if server is None else server.api_version

    # -- Sessions -----------------------------------------------------------

    def _routes_to_session(self, sql: str) -> bool:
        """Whether ``sql`` runs on the session rather than as a one-shot query.

        Always in ``session`` mode, and in ``auto`` mode once a session is open, so
        state a statement set (a ``USE``, a temp table) is there for the next one.
        Otherwise only a statement that needs session state, and every statement of a
        connection with a ``schema`` the server cannot apply to a one-shot query: sent
        anyway, it would be ignored and names would resolve in the default schema.
        """
        if self._config.mode == "session" or self._session_id is not None:
            return True
        if _needs_session(sql):
            return True
        if self._config.schema:
            server = self._server()
            return server is None or _QUERY_SCHEMA not in server.features
        return False

    def _session(self) -> str:
        """The session's id, opening it first if this connection has none yet."""
        if self._session_id is None:
            try:
                data = self._open_session(self._transport, self._config)
            except OperationalError as exc:
                if exc.status_code == 404:
                    raise NotSupportedError(
                        "this statement needs a SQL session, and SQL sessions are not "
                        "enabled on this server; run it without session state, or ask "
                        "an administrator to set SQL_SESSIONS_ENABLED=true",
                        code=exc.code,
                        status_code=exc.status_code,
                        detail=exc.detail,
                    ) from exc
                raise
            self._session_id = data["id"]
            self.agent_id = data.get("agent_id")
            self.staging_uri = data.get("staging_uri")
            self.active_catalog = data.get("active_catalog")
            self._apply_defaults()
        return self._session_id

    def _arrow_results(self) -> bool:
        """Whether results are read as Arrow: pyarrow is installed and the server
        (``api_version`` 3 or later) serves ``GET /queries/{id}/result``."""
        from ._arrow import pyarrow_available

        return pyarrow_available() and (self._api_version() or 0) >= 3

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
        session_id = self._session()
        try:
            response = self._transport.post(
                f"/sql/sessions/{session_id}/staging-files",
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
            if self._session_id is not None and not self._dead:
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
    statement_wait: float | None = None,
    first_page_limit: int = 200,
    use_cache: bool | None = None,
    application: str | None = None,
    mode: str = "session",
    tags: dict[str, str] | None = None,
    hooks: Hooks | None = None,
) -> Connection:
    """Open a DB-API 2.0 Connection to DuckHaven.

    ``mode`` is ``"session"`` (the default), which opens a SQL session now and runs
    every statement on it, or ``"auto"``, which opens none: each statement runs as a
    one-shot query, and a session opens only for the first statement that needs one
    (``SET``, ``USE``, a temp table, staging files). ``auto`` works where SQL sessions
    are disabled, as long as no statement needs one.

    ``compute_wait`` is how long to wait if the server has to start elastic compute
    before it can open the session; 0 fails immediately instead.

    ``statement_wait`` is how long the server may hold a statement call waiting for
    it to finish, so a statement is answered when it completes rather than on the
    next poll. ``None`` takes the server's own budget; 0 asks it never to wait.

    ``first_page_limit`` is how many result rows to ask for on the statement response
    itself, saving the round trip the cursor would otherwise always make to learn the
    result's columns. 0 disables it.

    ``use_cache`` says whether the server may answer a repeated read from its result
    cache. ``None`` takes the server's default (on); ``False`` always executes, which is
    what a benchmark needs.

    ``tags`` are labels recorded on every statement this connection runs, shown in
    DuckHaven's query History; ``Cursor.execute(tags=…)`` adds to them.

    ``statement_wait`` and ``first_page_limit`` apply to session statements. A one-shot
    query in ``auto`` mode waits ``statement_wait`` too, or 30 seconds when it is ``None``.
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
        statement_wait=statement_wait,
        first_page_limit=first_page_limit,
        use_cache=use_cache,
        application=application,
        mode=mode,
        tags=tags,
    )
    return Connection.open(config, hooks=hooks)
