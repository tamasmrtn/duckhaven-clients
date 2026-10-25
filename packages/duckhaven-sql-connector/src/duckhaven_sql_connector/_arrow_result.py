"""Fetching a whole result as Arrow (``GET /queries/{id}/result``, or carried on the
statement response when it is small), with pyarrow.

The alternative to paging JSON rows: one request, read one record batch at a time,
with every value keeping its type. Rows come out as the same Python values the
JSON path returns (see ``_types``), so a caller cannot tell which path ran except
by speed; ``fetch_arrow_table`` hands the Arrow data over without converting it.
"""

from __future__ import annotations

import contextlib
import io
from collections import deque
from typing import TYPE_CHECKING, Any

from ._telemetry import Hooks
from ._types import Converter, arrow_converter_for

if TYPE_CHECKING:
    from .client import Transport


class _ChunkReader(io.RawIOBase):
    """A readable file over the chunks of a streamed HTTP body, for pyarrow."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        # Fill the whole buffer unless the body ends: pyarrow reads a message body
        # with one read() and takes a short read for a truncated stream.
        n = 0
        while n < len(buffer):
            if not self._pending:
                try:
                    self._pending = next(self._chunks)
                except StopIteration:
                    break
                continue
            k = min(len(buffer) - n, len(self._pending))
            buffer[n : n + k] = self._pending[:k]
            self._pending = self._pending[k:]
            n += k
        return n


class ArrowResultSet:
    """The result of one statement, read from its Arrow stream on first use."""

    def __init__(
        self,
        transport: Transport,
        query_id: str,
        column_schema: list[tuple[str, str]] | None,
        total: int,
        hooks: Hooks | None = None,
        inline: bytes | None = None,
    ) -> None:
        self._transport = transport
        # The whole stream, when the statement response carried it (inline_arrow).
        self._inline = inline
        self._query_id = query_id
        self._hooks = hooks or Hooks()
        self.column_schema = column_schema
        self.columns: list[str] = [name for name, _ in column_schema or []]
        self.total = total
        self._converters: list[Converter | None] = [
            arrow_converter_for(t) for _, t in column_schema or []
        ]
        self._buffer: deque[tuple[Any, ...]] = deque()
        self._stack: contextlib.ExitStack | None = None
        self._reader: Any = None
        self._started = False
        self._exhausted = False

    # -- the stream ---------------------------------------------------------

    def ensure_started(self) -> None:
        """Open the stream; afterwards ``columns`` is known even without a schema."""
        if self._started:
            return
        import pyarrow as pa

        self._started = True
        if self._inline is not None:
            self._reader = pa.ipc.open_stream(pa.py_buffer(self._inline))
            self._inline = None
            if not self.columns:
                self.columns = list(self._reader.schema.names)
            return
        self._stack = contextlib.ExitStack()
        response = self._stack.enter_context(
            self._transport.stream(f"/queries/{self._query_id}/result", params={"format": "arrow"})
        )
        if response.status_code == 204:  # DDL or DML: no result grid
            self._close()
            return
        self._reader = pa.ipc.open_stream(_ChunkReader(response.iter_bytes()))
        if not self.columns:
            self.columns = list(self._reader.schema.names)

    def _next_batch(self) -> Any:
        self.ensure_started()
        if self._reader is None:
            return None
        try:
            return self._reader.read_next_batch()
        except StopIteration:
            self._close()
            return None

    def _close(self) -> None:
        self._exhausted = True
        self._reader = None
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    # -- rows ---------------------------------------------------------------

    def _fill(self) -> bool:
        batch = self._next_batch()
        if batch is None:
            return False
        rows = batch.to_pylist()
        convert = len(self._converters) == batch.num_columns
        for row in rows:
            values = tuple(row.values())
            if convert:
                values = tuple(
                    v if c is None or v is None else c(v)
                    for c, v in zip(self._converters, values, strict=True)
                )
            self._buffer.append(values)
        if self._hooks.on_rows_fetched is not None:
            self._hooks.on_rows_fetched(self._query_id, len(rows))
        return True

    def fetchone(self) -> tuple[Any, ...] | None:
        while not self._buffer and not self._exhausted:
            if not self._fill():
                break
        return self._buffer.popleft() if self._buffer else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        while len(out) < size:
            row = self.fetchone()
            if row is None:
                break
            out.append(row)
        return out

    def fetchall(self) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        while (row := self.fetchone()) is not None:
            out.append(row)
        return out

    def arrow_table(self) -> Any:
        """The rows not yet fetched, as a ``pyarrow.Table``, unconverted."""
        import pyarrow as pa

        from ._arrow import to_arrow_table

        if self._buffer:
            # Rows already pulled into Python by fetchone/fetchmany: rebuild those.
            pending = to_arrow_table(self.columns, list(self._buffer))
            self._buffer.clear()
            batches = [pending]
        else:
            batches = []
        self.ensure_started()
        if self._reader is not None:
            batches.append(self._reader.read_all())
            self._close()
        if not batches:
            return pa.table({c: [] for c in self.columns})
        return (
            batches[0]
            if len(batches) == 1
            else pa.concat_tables(batches, promote_options="permissive")
        )
