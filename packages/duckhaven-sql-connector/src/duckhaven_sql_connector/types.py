"""DuckDB type strings, parsed.

DuckHaven reports each result column's type in DuckDB's own spelling, the string
``DESCRIBE`` prints: ``cursor.description[i][1]``, ``cursor.column_types`` and the
``type`` of a column in ``Connection.metadata()``. :func:`parse_type` turns one into a
:class:`DuckType`, keeping what a caller mapping it to another type system needs: a
decimal's precision and scale, an enum's values, a fixed array's size, and the members
of lists, structs, maps and unions.

>>> parse_type("DECIMAL(18,4)")
DuckType(name='DECIMAL', fields=[], params=(18, 4), size=None)
"""

from ._types import DuckType, parse_type

__all__ = ["DuckType", "parse_type"]
