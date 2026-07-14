"""The DB-API ``Cursor``: submit a statement, poll it to completion, page its rows.

A statement is submitted to the session (``POST …/statements`` → 202), then polled via
``GET /queries/{id}`` until it finishes, then its rows are paged lazily through
:class:`~duckhaven_sql_connector.result.ResultSet`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

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

# A 7-tuple with only the column name known; DuckHaven's rows page carries no types.
_DESCRIPTION_FILLER = (None, None, None, None, None, None)


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

    # -- Execution ----------------------------------------------------------

    def execute(self, operation: str, parameters: Sequence[Any] | None = None) -> Cursor:
        self._ensure_open()
        sql = render_qmark(operation, parameters) if parameters else operation

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
        query = self._poll_to_completion(query["id"], query.get("status", "queued"))

        self._query_id = query["id"]
        row_count = query.get("row_count")
        self._rowcount = row_count if isinstance(row_count, int) else -1

        self._result = ResultSet(transport, self._query_id, config.fetch_size)
        self._result.ensure_started()
        cols = self._result.columns
        self._description = [(name, *_DESCRIPTION_FILLER) for name in cols] if cols else None
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
