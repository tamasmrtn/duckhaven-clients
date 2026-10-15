"""``qmark`` parameter binding.

A DuckHaven with ``api_version`` 3 or later takes the values as typed JSON next to the
SQL (:func:`server_parameter`) and renders them itself. Against an older server, whose
statement API takes no parameters, the connector renders them into the SQL text. This is
done safely:
values are turned into typed SQL literals with proper quoting/escaping — never string
interpolation — and ``?`` placeholders are only substituted when they appear at the top
level, not inside string literals, quoted identifiers, or comments.

Named/`$name` binding is intentionally out of scope for v1; ``paramstyle`` is ``qmark``.
"""

from __future__ import annotations

import base64
import math
import uuid
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


def server_parameter(value: Any) -> Any:
    """``value`` as DuckHaven's typed ``parameters`` take it.

    JSON's own types travel as they are. Anything JSON cannot say goes as
    ``{"type": ..., "value": ...}``: a ``Decimal`` keeps every digit as ``DECIMAL(p,s)``,
    a ``datetime`` is a ``TIMESTAMP`` (``TIMESTAMPTZ`` when aware), ``bytes`` travel
    base64-encoded, and a list of one type is a list of that type.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ProgrammingError("cannot bind non-finite float (nan/inf)")
        return value
    if isinstance(value, Decimal):
        return {"type": _decimal_type(value), "value": str(value)}
    if isinstance(value, datetime):
        kind = "TIMESTAMPTZ" if value.tzinfo is not None else "TIMESTAMP"
        return {"type": kind, "value": value.isoformat(sep=" ")}
    if isinstance(value, date):
        return {"type": "DATE", "value": value.isoformat()}
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ProgrammingError("cannot bind a time with a time zone; use a datetime")
        return {"type": "TIME", "value": value.isoformat()}
    if isinstance(value, bytes | bytearray | memoryview):
        return {"type": "BLOB", "value": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, uuid.UUID):
        return {"type": "UUID", "value": str(value)}
    if isinstance(value, list | tuple):
        return {"type": _list_type(value), "value": [_element(v) for v in value]}
    raise ProgrammingError(f"unsupported parameter type: {type(value).__name__}")


def _decimal_type(value: Decimal) -> str:
    if not value.is_finite():
        raise ProgrammingError("cannot bind non-finite Decimal")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    scale = max(0, -exponent)
    precision = max(len(digits) + max(0, exponent), scale, 1)
    if precision > 38:
        raise ProgrammingError("cannot bind a Decimal with more than 38 digits")
    return f"DECIMAL({precision},{scale})"


# A list's element type, from the Python types of its members.
_ELEMENT_TYPES: tuple[tuple[type, str], ...] = (
    (bool, "BOOLEAN"),
    (int, "BIGINT"),
    (float, "DOUBLE"),
    (str, "VARCHAR"),
    (datetime, "TIMESTAMP"),
    (date, "DATE"),
    (uuid.UUID, "UUID"),
)


def _list_type(values: Sequence[Any]) -> str:
    kinds = set()
    for v in values:
        if v is None:
            continue
        for python_type, duck_type in _ELEMENT_TYPES:
            if isinstance(v, python_type):
                if duck_type == "TIMESTAMP" and v.tzinfo is not None:
                    duck_type = "TIMESTAMPTZ"
                kinds.add(duck_type)
                break
        else:
            raise ProgrammingError(f"unsupported list element type: {type(v).__name__}")
    if len(kinds) != 1:
        raise ProgrammingError("a list parameter needs values of one type (and not only NULLs)")
    return kinds.pop() + "[]"


def _element(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date | uuid.UUID):
        return str(value)
    return value
