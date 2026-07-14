"""Result fetching — the transport seam behind the cursor.

v1 pages JSON rows off ``GET /queries/{id}/rows`` (the server's only result path today),
following the string-offset ``cursor`` until it is null. It is deliberately isolated so a
future server-side Arrow / EXTERNAL_LINKS disposition can slot in behind the same
``ResultSet`` interface without touching the cursor.

Rows arrive as ``{column: value}`` objects and are yielded as tuples in ``columns`` order,
per PEP 249.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from ._telemetry import Hooks
from .dbapi import InterfaceError

if TYPE_CHECKING:
    from .client import Transport


class ResultSet:
    def __init__(
        self, transport: Transport, query_id: str, fetch_size: int, hooks: Hooks | None = None
    ) -> None:
        self._transport = transport
        self._query_id = query_id
        self._fetch_size = fetch_size
        self._hooks = hooks or Hooks()
        self._buffer: deque[tuple[Any, ...]] = deque()
        self._next_cursor: str | None = None
        self._started = False
        self._exhausted = False
        self.columns: list[str] = []
        self.total = 0

    def _load_page(self) -> None:
        params: dict[str, Any] = {"limit": self._fetch_size}
        if self._next_cursor is not None:
            params["cursor"] = self._next_cursor
        response = self._transport.get(f"/queries/{self._query_id}/rows", params=params)
        try:
            page = response.json()
            columns = list(page["columns"])
            rows = page["rows"]
            self.total = page.get("total", 0) or 0
            self._next_cursor = page.get("cursor")
        except (ValueError, KeyError, TypeError) as exc:
            raise InterfaceError("malformed rows page from server") from exc
        self.columns = columns
        for row in rows:
            self._buffer.append(tuple(row.get(col) for col in columns))
        if self._hooks.on_rows_fetched is not None:
            self._hooks.on_rows_fetched(self._query_id, len(rows))
        if self._next_cursor is None:
            self._exhausted = True
        self._started = True

    def ensure_started(self) -> None:
        """Fetch the first page so column metadata is available for ``.description``."""
        if not self._started:
            self._load_page()

    def fetchone(self) -> tuple[Any, ...] | None:
        while not self._buffer and not self._exhausted:
            self._load_page()
        if self._buffer:
            return self._buffer.popleft()
        return None

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
        while True:
            row = self.fetchone()
            if row is None:
                break
            out.append(row)
        return out
