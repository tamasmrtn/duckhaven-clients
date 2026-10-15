"""Turn JSON result values back into Python values, by their DuckDB column type.

The rows page is JSON, so every value arrives as a string, number, list or object.
``column_schema`` says what each column really is (DuckDB's own type spelling), and
this module uses it to hand PEP 249 callers the Python type they expect: ``Decimal``
for ``DECIMAL``, ``int`` for every integer type, ``datetime``/``date``/``time`` for
the temporal types, ``UUID`` and ``bytes``. Nested lists, structs and maps are walked
by their declared member types.

Conversion goes by type, not by the value's JSON shape, so one mapping serves every
server: ``api_version`` 2 sends ``DECIMAL`` and wide integers as JSON numbers,
``api_version`` 3 as strings, and both convert to the same Python value. Without a
``column_schema`` (an older server or agent) values are passed through untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from .dbapi import InterfaceError

Converter = Callable[[Any], Any]

_INTEGERS = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)
_TIMESTAMPS = frozenset(
    {
        "TIMESTAMP",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
        "TIMESTAMP WITH TIME ZONE",
    }
)


@dataclass
class DuckType:
    """A parsed DuckDB logical type: its base name, and for containers its members.

    ``fields`` holds a struct's or union's ``(name, type)`` members, a map's
    ``("key", …), ("value", …)``, and a list's or array's single ``("", element)``.
    """

    name: str
    fields: list[tuple[str, DuckType]] = field(default_factory=list)


def parse_type(text: str) -> DuckType:
    """Parse DuckDB's spelling of a logical type (``DESCRIBE``'s ``column_type``)."""
    parser = _Parser(text)
    parsed = parser.type()
    parser.skip_space()
    if parser.pos != len(text):
        raise ValueError(f"unexpected text after type at {parser.pos}: {text!r}")
    return parsed


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def peek(self) -> str:
        self.skip_space()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise ValueError(f"expected {char!r} at {self.pos}: {self.text!r}")
        self.pos += 1

    def word(self) -> str:
        self.skip_space()
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] == "_"
        ):
            self.pos += 1
        if start == self.pos:
            raise ValueError(f"expected a name at {self.pos}: {self.text!r}")
        return self.text[start : self.pos]

    def name(self) -> str:
        """A struct or union member name: a bare word or a double-quoted identifier."""
        if self.peek() != '"':
            return self.word()
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == '"':
                if self.text[self.pos : self.pos + 1] == '"':
                    out.append('"')
                    self.pos += 1
                    continue
                return "".join(out)
            out.append(char)
        raise ValueError(f"unterminated quoted name: {self.text!r}")

    def skip_parameters(self) -> None:
        """Skip ``(…)`` after a scalar type: ``DECIMAL(18,4)``, ``ENUM('a', 'b')``."""
        if self.peek() != "(":
            return
        depth, quoted = 0, False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if quoted:
                if char == "'":
                    quoted = self.text[self.pos : self.pos + 1] == "'"
                    if quoted:
                        self.pos += 1
                continue
            if char == "'":
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return
        raise ValueError(f"unbalanced parentheses: {self.text!r}")

    def type(self) -> DuckType:
        base = self.word().upper()
        if base in ("STRUCT", "UNION"):
            self.expect("(")
            members = [self.member()]
            while self.peek() == ",":
                self.pos += 1
                members.append(self.member())
            self.expect(")")
            parsed = DuckType(base, members)
        elif base == "MAP":
            self.expect("(")
            key = self.type()
            self.expect(",")
            value = self.type()
            self.expect(")")
            parsed = DuckType(base, [("key", key), ("value", value)])
        else:
            if base in ("TIMESTAMP", "TIME") and self.text[self.pos :].upper().startswith(
                " WITH TIME ZONE"
            ):
                self.pos += len(" WITH TIME ZONE")
                base += " WITH TIME ZONE"
            self.skip_parameters()
            parsed = DuckType(base)
        while self.peek() == "[":
            self.pos += 1
            while self.peek().isdigit():
                self.pos += 1
            self.expect("]")
            parsed = DuckType("LIST", [("", parsed)])
        return parsed

    def member(self) -> tuple[str, DuckType]:
        name = self.name()
        return name, self.type()


def converter_for(type_text: str) -> Converter | None:
    """A function turning one JSON value of ``type_text`` into its Python value, or
    ``None`` when the JSON value already is the right one (strings, booleans,
    floats). An unparseable type is passed through: the value still arrives, only
    unconverted."""
    try:
        parsed = parse_type(type_text)
    except ValueError:
        return None
    return _converter(parsed, nested=False)


def _converter(t: DuckType, *, nested: bool) -> Converter | None:
    if t.name in _INTEGERS:
        return _int
    if t.name == "DECIMAL":
        return _decimal
    if t.name in _TIMESTAMPS:
        return datetime.fromisoformat
    if t.name == "DATE":
        return date.fromisoformat
    if t.name in ("TIME", "TIME WITH TIME ZONE"):
        return time.fromisoformat
    if t.name == "UUID":
        return uuid.UUID
    if t.name == "BLOB" and not nested:
        # Hex at the top level. Nested bytes are encoded by the server's JSON
        # serializer, not as hex, and are passed through.
        return bytes.fromhex
    if t.name == "LIST":
        element = _converter(t.fields[0][1], nested=True)
        return None if element is None else (lambda v: [_apply(element, x) for x in v])
    if t.name == "STRUCT":
        members = {n: c for n, m in t.fields if (c := _converter(m, nested=True)) is not None}
        if not members:
            return None
        return lambda v: {k: _apply(members[k], x) if k in members else x for k, x in v.items()}
    if t.name == "MAP":
        key = _converter(t.fields[0][1], nested=True)
        value = _converter(t.fields[1][1], nested=True)
        if key is None and value is None:
            return None
        return lambda v: {_apply(key, k): _apply(value, x) for k, x in v.items()}
    return None


def _apply(convert: Converter | None, value: Any) -> Any:
    if value is None or convert is None:
        return value
    return convert(value)


def _int(value: Any) -> int:
    # An older agent wrote HUGEINT as DOUBLE, so a float can arrive here; it was
    # rounded before it left the agent and converting it cannot make it worse.
    return int(value)


def _decimal(value: Any) -> Decimal:
    # str() first: a v2 server's float 0.1 must become Decimal("0.1"), not the
    # binary expansion Decimal(0.1) would give.
    return Decimal(str(value))


def convert_row(converters: list[Converter | None], row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Convert one row; raises :class:`InterfaceError` if a value has the wrong shape."""
    try:
        return tuple(_apply(c, v) for c, v in zip(converters, row, strict=True))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InterfaceError(f"could not decode a result value: {exc}") from exc


def arrow_converter_for(type_text: str) -> Converter | None:
    """For a value read from an Arrow result, the conversion that makes it what the
    JSON path returns for the same column (see :func:`converter_for`).

    Arrow already carries most types as Python wants them. Two differ: a
    ``HUGEINT``/``UHUGEINT`` column travels as ``DECIMAL(38,0)`` (Parquet has no
    128-bit integer) and becomes an ``int`` again, and a ``MAP`` arrives as a list of
    key/value pairs and becomes a ``dict``.
    """
    try:
        parsed = parse_type(type_text)
    except ValueError:
        return None
    return _arrow_converter(parsed)


def _arrow_converter(t: DuckType) -> Converter | None:
    if t.name in ("HUGEINT", "UHUGEINT"):
        return _int
    if t.name == "LIST":
        element = _arrow_converter(t.fields[0][1])
        return None if element is None else (lambda v: [_apply(element, x) for x in v])
    if t.name == "STRUCT":
        members = {n: c for n, m in t.fields if (c := _arrow_converter(m)) is not None}
        if not members:
            return None
        return lambda v: {k: _apply(members[k], x) if k in members else x for k, x in v.items()}
    if t.name == "MAP":
        key = _arrow_converter(t.fields[0][1])
        value = _arrow_converter(t.fields[1][1])
        return lambda v: {_apply(key, k): _apply(value, x) for k, x in v}
    return None
