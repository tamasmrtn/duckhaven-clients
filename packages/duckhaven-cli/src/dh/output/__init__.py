"""Rendering: one envelope for machines, a plain table for people.

Three rules hold everywhere, and each is a defect observed in a shipping CLI:

* **stdout carries only the payload.** Progress, warnings and errors go to stderr,
  so `dh ... | jq` never chokes on a status line.
* **`--format json` covers the error path too.** `snow --format JSON` renders a
  failure as a Rich-boxed panel on stderr and exits 1, so CI gets nothing
  parseable; here a failure is the same three-key envelope the server uses.
* **No box-drawing characters, ever.** `snow` emits them even when redirected to a
  file, because nothing checks for a TTY. The table here is spaces and newlines.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from enum import Enum
from typing import Any, TextIO

from dh.errors import DhError


class Format(str, Enum):
    JSON = "json"
    TABLE = "table"
    CSV = "csv"


def default_format(stream: TextIO | None = None) -> Format:
    """`table` for a terminal, `json` for anything else.

    Someone who pipes `dh` into `jq` without reading the docs gets JSON, and a
    script never accidentally parses a table laid out for human eyes.
    """
    stream = stream or sys.stdout
    try:
        interactive = stream.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        interactive = False
    return Format.TABLE if interactive else Format.JSON


def color_enabled(stream: TextIO | None = None, *, no_color: bool = False) -> bool:
    """Colour only for a terminal that has not asked us to stop.

    Honours `NO_COLOR` (any non-empty value) per https://no-color.org.
    """
    if no_color or os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        return False


def envelope(data: Any, cursor: str | None = None, has_more: bool = False) -> dict[str, Any]:
    """The one JSON shape every command emits.

    Fixed for the life of the CLI: once CI parses `.data[]` it cannot change. The
    `cursor` and `has_more` keys are present even for endpoints that never
    paginate, so a consumer never has to know which kind an endpoint is -- the
    thing `databricks` gets wrong by naming its envelope key per command.
    """
    return {"data": data, "cursor": cursor, "has_more": has_more}


# --- Tabular shaping -------------------------------------------------------


def columns_of(rows: list[dict[str, Any]]) -> list[str]:
    """Column order: first-seen wins, then any key a later row adds.

    Sorting would scramble the order the server chose, which usually puts the
    identifying fields first.
    """
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def as_grid(data: Any) -> tuple[list[str], list[list[Any]]]:
    """Coerce whatever a command produced into columns and rows.

    Handles the three shapes commands actually return: a list of records, a
    single record, and ``RowsPageOut``'s already-tabular ``{columns, rows}``.
    """
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        cols = [str(c) for c in data.get("columns") or []]
        return cols, [list(r) for r in data["rows"]]
    if isinstance(data, dict):
        return ["field", "value"], [[k, v] for k, v in data.items()]
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        cols = columns_of(data)
        return cols, [[row.get(c) for c in cols] for row in data]
    if isinstance(data, list):
        return ["value"], [[item] for item in data]
    return ["value"], [[data]]


def cell(value: Any) -> str:
    """One cell as text. Empty is shown, not implied."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


_BOLD = "\033[1m"
_RESET = "\033[0m"


def render_table(columns: list[str], rows: list[list[Any]], *, color: bool = False) -> str:
    """Space-aligned columns. No borders, no glyphs, nothing to strip."""
    if not columns:
        return ""
    text = [[cell(v) for v in row] for row in rows]
    widths = [
        max(len(columns[i]), *(len(r[i]) for r in text)) if text else len(columns[i])
        for i in range(len(columns))
    ]
    header = "  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip()
    lines = [f"{_BOLD}{header}{_RESET}" if color else header]
    lines.extend(
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip() for row in text
    )
    return "\n".join(lines)


def render_csv(columns: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([cell(v) for v in row] for row in rows)
    return buffer.getvalue().rstrip("\n")


# --- Writing ---------------------------------------------------------------


def render(
    data: Any,
    fmt: Format,
    *,
    cursor: str | None = None,
    has_more: bool = False,
    color: bool = False,
) -> str:
    if fmt is Format.JSON:
        return json.dumps(envelope(data, cursor, has_more), indent=2, default=str)
    columns, rows = as_grid(data)
    if fmt is Format.CSV:
        return render_csv(columns, rows)
    return render_table(columns, rows, color=color)


def write(
    data: Any,
    fmt: Format,
    *,
    cursor: str | None = None,
    has_more: bool = False,
    stream: TextIO | None = None,
    color: bool | None = None,
) -> None:
    """Write a payload to stdout. Nothing else is ever written there."""
    stream = stream or sys.stdout
    if color is None:
        color = color_enabled(stream)
    body = render(data, fmt, cursor=cursor, has_more=has_more, color=color)
    if body:
        stream.write(body + "\n")


def write_error(error: DhError, fmt: Format, *, stream: TextIO | None = None) -> None:
    """Write a failure to stderr, in JSON when JSON was asked for.

    The `snow` defect this exists to avoid: a `--format json` run that fails must
    still hand CI something it can parse, and must never mix the failure into the
    payload stream.
    """
    stream = stream or sys.stderr
    if fmt is Format.JSON:
        stream.write(json.dumps(error.envelope(), indent=2, default=str) + "\n")
    else:
        stream.write(f"Error: {error.message}\n")
