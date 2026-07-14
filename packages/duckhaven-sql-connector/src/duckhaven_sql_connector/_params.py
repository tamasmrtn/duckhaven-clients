"""Client-side ``qmark`` parameter binding.

DuckHaven's statement API takes no server-side parameters (only ``{sql, timeout_s}``),
so the connector renders parameters into the SQL text itself. This is done safely:
values are turned into typed SQL literals with proper quoting/escaping — never string
interpolation — and ``?`` placeholders are only substituted when they appear at the top
level, not inside string literals, quoted identifiers, or comments.

Named/`$name` binding is intentionally out of scope for v1; ``paramstyle`` is ``qmark``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from .dbapi import ProgrammingError


def quote_identifier(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def render_literal(value: Any) -> str:
    """Render a Python value as a safe DuckDB SQL literal."""
    if value is None:
        return "NULL"
    # bool before int (bool is an int subclass); datetime before date likewise.
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ProgrammingError("cannot bind non-finite float (nan/inf)")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ProgrammingError("cannot bind non-finite Decimal")
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (bytes, bytearray)):
        # unhex() yields a BLOB and avoids any ambiguity over backslash escapes.
        return f"unhex('{bytes(value).hex()}')"
    if isinstance(value, datetime):
        return "TIMESTAMP '" + value.isoformat(sep=" ") + "'"
    if isinstance(value, date):
        return "DATE '" + value.isoformat() + "'"
    if isinstance(value, time):
        return "TIME '" + value.isoformat() + "'"
    raise ProgrammingError(f"unsupported parameter type: {type(value).__name__}")


def render_qmark(sql: str, parameters: Sequence[Any]) -> str:
    """Substitute ``?`` placeholders with rendered literals, respecting SQL syntax.

    ``?`` inside single-quoted strings, double-quoted identifiers, and ``--`` / ``/* */``
    comments is left untouched. The number of placeholders must match ``parameters``.
    """
    params = list(parameters)
    out: list[str] = []
    i = 0
    n = len(sql)
    pidx = 0

    while i < n:
        ch = sql[i]

        # Single-quoted string literal (with '' escaping).
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier (with "" escaping).
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Line comment.
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                out.append(sql[i])
                i += 1
            continue

        # Block comment.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            out.append(sql[i])
            out.append(sql[i + 1])
            i += 2
            while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                out.append(sql[i])
                i += 1
            if i < n:
                out.append("*/")
                i += 2
            continue

        # A real placeholder.
        if ch == "?":
            if pidx >= len(params):
                raise ProgrammingError("more placeholders than parameters")
            out.append(render_literal(params[pidx]))
            pidx += 1
            i += 1
            continue

        out.append(ch)
        i += 1

    if pidx != len(params):
        raise ProgrammingError(
            f"parameter count mismatch: {len(params)} given, {pidx} placeholder(s) in SQL"
        )
    return "".join(out)
