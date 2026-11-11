"""The DB-API ``Cursor``: submit a statement, poll it to completion, page its rows.

A statement is submitted to the session (``POST …/statements``) or, in ``auto`` mode, as
a one-shot query (``POST …/queries``). Either way it is then polled via
``GET /queries/{id}`` until it finishes, and its rows are paged lazily through
:class:`~duckhaven_sql_connector.result.ResultSet`.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from . import _metadata
from ._arrow_result import ArrowResultSet
from ._params import render_qmark, server_parameter
from .dbapi import OperationalError, ProgrammingError
from .result import ResultSet

if TYPE_CHECKING:
    from .connection import Connection

# Poll cadence for an in-flight statement: start tight, back off to a ceiling.
_POLL_START = 0.1
_POLL_MAX = 2.0
# Grace added to the server-side statement timeout before the client gives up polling.
_POLL_GRACE = 30.0

_PENDING = ("queued", "running")

# How long a one-shot query is held server-side when the caller set no
# `statement_wait`. Sending none would answer 202 at once and leave the result to the
# poll loop's sleeps; the server caps it at 60.
_ONE_SHOT_WAIT_S = 30.0

# A cancel sent while the submit is still in flight can reach the server before the run
# is recorded, and answer 404. It is retried this often, for this long.
_CANCEL_RETRY_S = 0.1
_CANCEL_RETRY_FOR_S = 2.0

# What a run ends with when its agent went away mid-statement.
_AGENT_LOST = "agent disconnected"


def _describe(columns: list[str], schema: list[tuple[str, str]] | None) -> list[tuple[Any, ...]]:
    """Build PEP 249 ``description`` 7-tuples for a result's columns.

    ``type_code`` is DuckHaven's ``column_schema`` type — DuckDB's own logical-type
    spelling (``"DECIMAL(18,4)"``, ``"STRUCT(a INTEGER, b VARCHAR)"``), the same string
    ``DESCRIBE`` prints. PEP 249 leaves ``type_code`` implementation-defined, so a type
    name is a legal value for it. It is ``None`` against a server that does not report
    the schema, which is what the field was before DuckHaven grew it.

    The remaining five fields (display_size, internal_size, precision, scale, null_ok)
    stay ``None``: the precision/scale are already inside the type string, and DuckDB
    relations carry no reliable nullability.
    """
    types = dict(schema or ())
    return [(name, types.get(name), None, None, None, None, None) for name in columns]


# Standalone transaction-control statements. The DuckHaven session is autocommit (each
# statement commits via Polaris), so these carry no server-side meaning — and submitting a
# real ``BEGIN`` would wrongly open a transaction on the agent's connection. Some clients
# (notably dbt's test harness) still emit a bare ``COMMIT``; treat all of these as
# client-side no-ops rather than statements the session would leave queued forever.
_TXN_CONTROL_NOOPS = frozenset(
    {
        "BEGIN",
        "BEGIN TRANSACTION",
        "START TRANSACTION",
        "COMMIT",
        "COMMIT TRANSACTION",
        "END",
        "END TRANSACTION",
        "ROLLBACK",
        "ROLLBACK TRANSACTION",
        "ABORT",
    }
)


def _is_txn_control_noop(sql: str) -> bool:
    """True if ``sql`` is a standalone BEGIN/COMMIT/ROLLBACK-style statement."""
    normalized = " ".join(sql.strip().rstrip(";").split()).upper()
    return normalized in _TXN_CONTROL_NOOPS


# Statements whose effect outlives them on the connection, so they need a session to
# live in: `auto` mode opens one for the first of these. Anything else, an unknown
# leading keyword included, runs as a one-shot query, where the server refuses what it
# cannot run with a clear error rather than this guess changing where it ran.
_SESSION_KEYWORDS = frozenset({"SET", "RESET", "USE", "PRAGMA", "ATTACH", "DETACH"})
_LEADING_NOISE = re.compile(r"(?:\s+|--[^\n]*|/\*.*?\*/)*", re.DOTALL)


def _needs_session(sql: str) -> bool:
    """True if ``sql`` sets state a later statement may rely on (``SET``, ``USE``,
    ``CREATE TEMP TABLE``, ...), reading past leading comments."""
    words = sql[_LEADING_NOISE.match(sql).end() :].split(None, 4)
    head = [w.upper() for w in words[:4]]
    if not head:
        return False
    if head[0] in _SESSION_KEYWORDS:
        return True
    # CREATE [OR REPLACE] TEMP|TEMPORARY TABLE|VIEW|MACRO|SEQUENCE ...
    if head[0] != "CREATE":
        return False
    rest = head[3:] if head[1:3] == ["OR", "REPLACE"] else head[1:]
    return bool(rest) and rest[0] in ("TEMP", "TEMPORARY")


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self.arraysize = 1
        self._closed = False
        # The id of the statement in flight or last run, chosen here before it is sent,
        # so another thread can cancel it while execute() is still blocked.
        self.query_id: str | None = None
        self._submitting = False
        self._cancel_requested = False
        self._result: ResultSet | None = None
        self._description: list[tuple[Any, ...]] | None = None
        self._rowcount = -1

    # -- PEP 249 read-only attributes ---------------------------------------

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def column_types(self) -> list[str] | None:
        """The result columns' DuckDB types, or ``None`` when the server reported none.

        A convenience over ``[d[1] for d in description]`` for callers that want the
        types on their own — e.g. to decide whether a value needs decoding.
        """
        if self._description is None:
            return None
        types = [d[1] for d in self._description]
        return None if all(t is None for t in types) else types

    # -- Execution ----------------------------------------------------------

    def execute(
        self,
        operation: str,
        parameters: Sequence[Any] | None = None,
        *,
        query_id: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> Cursor:
        """Run one statement.

        ``query_id`` names the run (a UUID string); one is chosen when omitted. It is
        set on :attr:`query_id` before the statement is sent, so another thread can
        cancel the run by it at once. ``tags`` add to the connection's ``tags`` for this
        statement. A server too old to take a chosen id picks its own, and
        :attr:`query_id` becomes that once the submit returns.
        """
        self._ensure_open()
        # A current server renders the values itself, typed; an older one cannot.
        server_side = bool(parameters) and (self._connection._api_version() or 0) >= 3
        if parameters and not server_side:
            sql = render_qmark(operation, parameters)
        else:
            sql = operation

        if _is_txn_control_noop(sql):
            # Autocommit session: nothing to submit. Reset to a clean, result-less state.
            self.query_id = None
            self._result = None
            self._description = None
            self._rowcount = -1
            return self

        transport = self._connection._transport
        config = self._connection._config
        on_session = self._connection._routes_to_session(sql)
        arrow = self._connection._arrow_results()

        chosen = str(query_id) if query_id is not None else str(uuid.uuid4())
        body: dict[str, Any] = {"id": chosen, "sql": sql, "timeout_s": config.timeout}
        merged_tags = {**(config.tags or {}), **(tags or {})}
        if merged_tags:
            body["tags"] = merged_tags
        if server_side:
            body["parameters"] = [server_parameter(v) for v in parameters]
        if config.use_cache is not None:
            body["use_cache"] = config.use_cache
        if on_session:
            path = f"/sql/sessions/{self._connection._session()}/statements"
            wait = config.statement_wait
            # Always sent, 0 included: the server returns a first page by default, so
            # omitting the field asks for one. 0 has to travel to mean "don't".
            # Never more than the caller will buffer anyway. Reading the result as Arrow
            # takes no JSON page at all: the rows come from the Arrow stream instead.
            body["first_page_limit"] = (
                0 if arrow else min(config.first_page_limit, config.fetch_size)
            )
            if arrow:
                # A server that can carries a small result whole on this response, so
                # it costs no second call. Older servers ignore the field.
                body["inline_format"] = "arrow"
        else:
            path = f"/workspaces/{config.workspace}/queries"
            wait = (
                config.statement_wait
                if config.statement_wait is not None
                else min(_ONE_SHOT_WAIT_S, config.http_timeout / 2)
            )
            for key, value in (
                ("catalog", config.catalog),
                ("agent_id", config.agent),
                # Only reached when the server applies it (see _routes_to_session).
                ("schema", config.schema),
            ):
                if value:
                    body[key] = value
        if wait is not None:
            body["wait_timeout_s"] = wait

        # Set before the submit, so a concurrent cancel() (dbt aborting from another
        # thread, a BI tool's stop button) can reach the run while the server holds it.
        self.query_id = chosen
        self._cancel_requested = False
        self._submitting = True
        try:
            response = transport.post(path, json=body)
        except OperationalError as exc:
            # A reaped/closed/agent-lost session answers 409; the connection is dead.
            if on_session and exc.status_code == 409:
                self._connection._mark_dead()
            raise
        finally:
            self._submitting = False

        query = response.json()
        # An older server ignores the chosen id and picks its own.
        self.query_id = query["id"]
        if self._cancel_requested and self.query_id != chosen:
            # The cancel went to an id the server never used.
            self._try_cancel(self.query_id)
        query = self._poll_to_completion(query, wait)

        row_count = query.get("row_count")
        self._rowcount = row_count if isinstance(row_count, int) else -1

        if query.get("has_result") is False:
            # DDL/DML: the server says there is nothing to read, so do not ask it for
            # an empty result just to learn that. An older server sends no field.
            self._result = ResultSet.from_rows([], [])
            self._description = None
            return self

        if arrow:
            schema = query.get("column_schema")
            inline = query.get("inline_arrow")
            self._result = ArrowResultSet(
                transport,
                self.query_id,
                [(c["name"], c["type"]) for c in schema] if schema else None,
                total=row_count if isinstance(row_count, int) else 0,
                hooks=transport._hooks,
                inline=base64.b64decode(inline) if inline else None,
            )
            if not self._result.columns:
                # No schema on the query (DDL/DML, or an older agent): the stream says.
                self._result.ensure_started()
            cols = self._result.columns
            self._description = _describe(cols, self._result.column_schema) if cols else None
            return self

        self._result = ResultSet(
            transport, self.query_id, config.fetch_size, hooks=transport._hooks
        )
        # The server can return the first page on the submit response. When it does,
        # this whole statement cost one HTTP call; `ensure_started` would otherwise
        # make a second one unconditionally -- even for `SELECT 1`, and even for a
        # caller that never reads a row -- purely so `.description` has column names.
        first_page = query.get("first_page")
        if first_page is not None:
            self._result.prime(first_page)
        else:
            self._result.ensure_started()
        cols = self._result.columns
        self._description = _describe(cols, self._result.column_schema) if cols else None
        return self

    def executemany(self, operation: str, seq_of_parameters: Sequence[Sequence[Any]]) -> Cursor:
        total = 0
        known = True
        for parameters in seq_of_parameters:
            self.execute(operation, parameters)
            if self._rowcount < 0:
                known = False
            else:
                total += self._rowcount
        self._rowcount = total if known else -1
        return self

    def _poll_to_completion(self, query: dict[str, Any], wait: float | None) -> dict[str, Any]:
        """Follow a statement to a terminal state, starting from the submit response.

        Normally never loops: the server holds the submit response until the
        statement finishes, so it arrives terminal and this returns straight away.
        What is left is the tail that outran that budget, and there the status route
        takes the same wait, so each GET blocks instead of answering "still running".

        That buys request *volume*, not latency — the sleep before each GET still
        bounds how late completion is noticed, exactly as it did before. It stays
        because a server too old to know the parameter answers immediately, and
        without it this would spin. Against such a server the behaviour below is
        unchanged; the tail is a small share of statements either way.

        Takes the whole submit body rather than its id and status: when that body is
        already terminal it *is* the result, carrying `row_count` and `error`. Keeping
        only the two fields discarded both — `rowcount` came back -1 for every
        statement, and a failure raised "statement failed (failed)" instead of what
        actually went wrong. Neither showed up until the server started answering
        submit terminally.
        """
        transport = self._connection._transport
        config = self._connection._config
        query_id = query["id"]
        status = query.get("status", "queued")
        deadline = transport._monotonic() + config.timeout + _POLL_GRACE
        params = None if wait is None else {"wait_timeout_s": wait}
        interval = _POLL_START
        while status in _PENDING:
            if transport._monotonic() > deadline:
                self._try_cancel(query_id)
                raise OperationalError(f"statement {query_id} timed out while polling")
            transport._sleep(interval)
            interval = min(_POLL_MAX, interval * 1.5)
            query = transport.get(f"/queries/{query_id}", params=params).json()
            status = query.get("status", "running")
        if status == "done":
            return query
        error = query.get("error")
        if status == "cancelled":
            # Stopped from outside the statement: nothing wrong with the SQL.
            raise OperationalError(f"statement {query_id} was cancelled", code="cancelled")
        if error and error.startswith(_AGENT_LOST):
            raise OperationalError(error, detail=error)
        raise ProgrammingError(
            error or f"statement failed ({status})", status_code=None, detail=error
        )

    def _try_cancel(self, query_id: str) -> None:
        try:
            self._connection._transport.delete(f"/queries/{query_id}")
        except Exception:  # noqa: BLE001 - best-effort cancel; never mask the real error
            pass

    def cancel(self) -> None:
        """Cancel the in-flight (or most recent) statement via the query API.

        Safe to call from another thread while :meth:`execute` is blocked: the run's id
        is known before it is sent. The server records the run a moment after the
        request arrives, so a 404 while the submit is still in flight is retried
        briefly. A run that already finished is left as it is.
        """
        self._cancel_requested = True
        query_id = self.query_id
        if query_id is None:
            return
        transport = self._connection._transport
        deadline = transport._monotonic() + _CANCEL_RETRY_FOR_S
        while True:
            try:
                transport.delete(f"/queries/{query_id}")
                return
            except ProgrammingError as exc:
                if exc.status_code != 404 or not self._submitting:
                    return
            except Exception:  # noqa: BLE001 - best-effort cancel; never mask the real error
                return
            if transport._monotonic() > deadline:
                return
            transport._sleep(_CANCEL_RETRY_S)

    # -- Fetching -----------------------------------------------------------

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._require_result().fetchone()

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        return self._require_result().fetchmany(self.arraysize if size is None else size)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._require_result().fetchall()

    # -- Metadata (dbt/BI relation introspection) ---------------------------

    def catalogs(self) -> Cursor:
        """List catalogs (`catalog_name`). Fetch the rows to read them."""
        transport, workspace = self._browse_target()
        return self._local_metadata(_metadata.fetch_catalogs(transport, workspace))

    def schemas(self, *, catalog: str | None = None, schema_name: str | None = None) -> Cursor:
        """List schemas, optionally filtered by catalog / name pattern.

        Costs one request per catalog in scope; pass ``catalog=`` to make it one.
        """
        transport, workspace = self._browse_target()
        return self._local_metadata(
            _metadata.fetch_schemas(transport, workspace, catalog, schema_name)
        )

    def tables(
        self,
        *,
        catalog: str | None = None,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> Cursor:
        """List tables, optionally filtered by catalog/schema/name.

        Costs one request per catalog in scope plus one per schema; pass ``catalog=`` and
        ``schema_name=`` to keep that to two.
        """
        transport, workspace = self._browse_target()
        return self._local_metadata(
            _metadata.fetch_tables(transport, workspace, catalog, schema_name, table_name)
        )

    def _browse_target(self) -> tuple[Any, str]:
        """Check the cursor is usable, then hand back what the browse calls need.

        Called *before* the listing is fetched: a closed cursor must raise rather than
        issue requests.
        """
        self._ensure_open()
        return self._connection._transport, self._connection._config.workspace

    def _local_metadata(self, listing: tuple[list[str], list[tuple[Any, ...]]]) -> Cursor:
        """Present a client-assembled listing as an ordinary finished result set."""
        columns, rows = listing
        self.query_id = None
        self._result = ResultSet.from_rows(columns, rows)
        self._description = _describe(columns, None)
        self._rowcount = len(rows)
        return self

    def columns(
        self,
        *,
        catalog: str | None = None,
        schema_name: str | None = None,
        table_name: str | None = None,
        column_name: str | None = None,
    ) -> Cursor:
        """Execute a query listing one relation's columns; fetch the rows to read them.

        ``table_name`` is **required and exact** — DuckHaven reports columns with
        ``DESCRIBE``, which names a single relation. Enumerate relations with
        :meth:`tables` first. Rows keep the familiar shape ``(table_catalog,
        table_schema, table_name, column_name, ordinal_position, data_type,
        is_nullable)``; ``data_type`` is DuckDB's spelling, the same string a query
        result reports in its column types.
        """
        return self._run_metadata(
            _metadata.columns_query(catalog, schema_name, table_name, column_name)
        )

    def _run_metadata(self, query: tuple[str, list[Any]]) -> Cursor:
        sql, params = query
        return self.execute(sql, params or None)

    def fetch_arrow_table(self) -> Any:
        """Return the remaining rows as a ``pyarrow.Table`` (requires the ``arrow`` extra)."""
        from ._arrow import to_arrow_table

        result = self._require_result()
        if isinstance(result, ArrowResultSet):
            return result.arrow_table()
        columns = result.columns
        rows = result.fetchall()
        return to_arrow_table(columns, rows)

    def __iter__(self) -> Cursor:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        self._result = None

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- No-op setters PEP 249 defines --------------------------------------

    def setinputsizes(self, sizes: Sequence[Any]) -> None:
        pass

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        pass

    # -- Internals ----------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProgrammingError("cursor is closed")
        self._connection._ensure_usable()

    def _require_result(self) -> ResultSet:
        if self._closed:
            raise ProgrammingError("cursor is closed")
        if self._result is None:
            raise ProgrammingError("no result set; call execute() first")
        return self._result
