"""Client-side JSON→Arrow conversion (the ``arrow`` optional extra).

v1 has no server-side Arrow result path, so ``Cursor.fetch_arrow_table`` builds a
``pyarrow.Table`` from the JSON rows the pager already returns. ``pyarrow`` is an optional
dependency; calling the Arrow path without it raises ``NotSupportedError``.
"""

from __future__ import annotations

import importlib
from typing import Any

from .dbapi import NotSupportedError


def _load_pyarrow() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:  # pragma: no cover - pyarrow is present in the test env
        raise NotSupportedError(
            "pyarrow is required for Arrow results; install duckhaven-sql-connector[arrow]"
        ) from exc


def to_arrow_table(columns: list[str], rows: list[tuple[Any, ...]]) -> Any:
    """Build a ``pyarrow.Table`` from column names and row tuples (column order preserved)."""
    pa = _load_pyarrow()
    if not columns:
        return pa.table({})
    data = {col: [row[i] for row in rows] for i, col in enumerate(columns)}
    return pa.table(data)
