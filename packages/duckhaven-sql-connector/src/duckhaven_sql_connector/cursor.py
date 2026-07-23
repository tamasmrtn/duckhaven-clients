"""The DB-API ``Cursor``: submit a statement, poll it to completion, page its rows.

A statement is submitted to the session (``POST …/statements`` → 202), then polled via
``GET /queries/{id}`` until it finishes, then its rows are paged lazily through
:class:`~duckhaven_sql_connector.result.ResultSet`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from . import _metadata
from ._params import render_qmark
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


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self.arraysize = 1
        self._closed = False
        self._query_id: str | None = None
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

    def execute(self, operation: str, parameters: Sequence[Any] | None = None) -> Cursor:
        self._ensure_open()
        sql = render_qmark(operation, parameters) if parameters else operation

        if _is_txn_control_noop(sql):
            # Autocommit session: nothing to submit. Reset to a clean, result-less state.
            self._query_id = None
            self._result = None
            self._description = None
            self._rowcount = -1
            return self

        transport = self._connection._transport
        config = self._connection._config
        session_id = self._connection._session_id

        try:
            response = transport.post(
                f"/sql/sessions/{session_id}/statements",
                json={"sql": sql, "timeout_s": config.timeout},
            )
        except OperationalError as exc:
            # A reaped/closed/agent-lost session answers 409; the connection is dead.
            if exc.status_code == 409:
                self._connection._mark_dead()
            raise

        query = response.json()
        # Record the id before polling so a concurrent cancel() (e.g. dbt aborting from
        # another thread) can reach the statement while it is still running, not only
        # after it finishes.
        self._query_id = query["id"]
        query = self._poll_to_completion(query["id"], query.get("status", "queued"))

        row_count = query.get("row_count")
        self._rowcount = row_count if isinstance(row_count, int) else -1

        self._result = ResultSet(
            transport, self._query_id, config.fetch_size, hooks=transport._hooks
        )
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

    def _poll_to_completion(self, query_id: str, status: str) -> dict[str, Any]:
        transport = self._connection._transport
        deadline = transport._monotonic() + self._connection._config.timeout + _POLL_GRACE
        interval = _POLL_START
        query: dict[str, Any] = {"id": query_id, "status": status}
        while status in _PENDING:
            if transport._monotonic() > deadline:
                self._try_cancel(query_id)
                raise OperationalError(f"statement {query_id} timed out while polling")
            transport._sleep(interval)
            interval = min(_POLL_MAX, interval * 1.5)
            query = transport.get(f"/queries/{query_id}").json()
            status = query.get("status", "running")
        if status != "done":
            raise ProgrammingError(
                query.get("error") or f"statement failed ({status})",
                status_code=None,
                detail=query.get("error"),
            )
        return query

    def _try_cancel(self, query_id: str) -> None:
        try:
            self._connection._transport.delete(f"/queries/{query_id}")
        except Exception:  # noqa: BLE001 - best-effort cancel; never mask the real error
            pass

    def cancel(self) -> None:
        """Cancel the in-flight (or most recent) statement via the query API."""
        if self._query_id is not None:
            self._try_cancel(self._query_id)

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
        self._query_id = None
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
