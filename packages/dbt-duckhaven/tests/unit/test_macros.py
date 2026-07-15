"""Render-level coverage for the DuckHaven macro overrides.

These macros (get_columns_in_relation, drop_schema, drop_relation, load_csv_rows) are the
DuckHaven-specific SQL and otherwise only run in the env-gated integration suites. Here we
render each one through a minimal Jinja environment with stubbed dbt context helpers and
assert on the SQL it emits — no live server, so it runs in normal CI.

The dbt ``{% call statement(...) %}`` block needs a real Jinja macro as its target (so
``caller()`` is defined), so we prepend a tiny ``statement`` macro that forwards the
rendered block body to a Python recorder. Everything else is a plain global stub.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

_MACROS_DIR = Path(__file__).parents[2] / "src/dbt/include/duckhaven/macros"

# A real Jinja macro target for `{% call statement(...) %}` — inside a macro, `caller()`
# yields the block body, which we hand to a Python recorder.
_PREAMBLE = (
    "{% macro statement(name, fetch_result=False, auto_begin=True) %}"
    "{% do _record_statement(name, caller()) %}"
    "{% endmacro %}\n"
)


class _Relation:
    """A stand-in for a dbt relation covering every attribute the macros touch."""

    database = "sales"
    schema = "analytics"
    type = "table"

    def __str__(self) -> str:
        return '"sales"."analytics"."people"'

    def render(self) -> str:
        return str(self)

    def without_identifier(self) -> str:
        return '"sales"."analytics"'


class _Adapter:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    @staticmethod
    def quote(value: str) -> str:
        return f'"{value}"'

    def add_query(self, sql, bindings=None, abridge_sql_log=False):
        self._recorder.queries.append((str(sql), list(bindings or [])))


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[tuple[str, str]] = []
        self.queries: list[tuple[str, list]] = []
        self.returned = None

    @property
    def statement_sql(self) -> str:
        return "\n".join(sql for _, sql in self.statements)


def _render(macro: str, *args, run_query_rows=None) -> _Recorder:
    """Render one macro and return the recorder holding the SQL it emitted."""
    recorder = _Recorder()
    env = jinja2.Environment(extensions=["jinja2.ext.do"])
    env.globals.update(
        {
            "_record_statement": lambda name, sql: recorder.statements.append((name, str(sql))),
            "execute": True,
            "run_query": lambda sql: run_query_rows or [],
            "load_result": lambda name: type("R", (), {"table": "COLUMNS_TABLE"}),
            "sql_convert_columns_in_relation": lambda table: table,
            "return": lambda value: setattr(recorder, "returned", value) or "",
            "adapter": _Adapter(recorder),
            "this": _Relation(),
            "get_batch_size": lambda: 2,
            "get_binding_char": lambda: "?",
            "get_seed_column_quoted_csv": lambda model, names: ", ".join(f'"{n}"' for n in names),
        }
    )
    source = (
        _PREAMBLE
        + (_MACROS_DIR / "adapters.sql").read_text()
        + (_MACROS_DIR / "seed.sql").read_text()
    )
    template = env.from_string(source)
    getattr(template.module, macro)(*args)
    return recorder


def test_get_columns_in_relation_uses_describe_not_information_schema():
    rec = _render("duckhaven__get_columns_in_relation", _Relation())
    sql = rec.statement_sql.lower()
    assert "describe" in sql
    assert "information_schema" not in sql
    assert "column_type as data_type" in sql


def test_drop_schema_drops_relations_then_schema_without_cascade():
    rows = [
        {"table_name": "orders", "table_type": "BASE TABLE"},
        {"table_name": "orders_view", "table_type": "VIEW"},
    ]
    rec = _render("duckhaven__drop_schema", _Relation(), run_query_rows=rows)
    names = [name for name, _ in rec.statements]
    # One drop per relation, then the schema drop.
    assert names == ["drop_1", "drop_2", "drop_schema"]
    per_relation = rec.statement_sql.lower()
    assert "drop table if exists" in per_relation
    assert "drop view if exists" in per_relation
    schema_drop = dict(rec.statements)["drop_schema"].lower()
    assert "drop schema if exists" in schema_drop
    assert "cascade" not in rec.statement_sql.lower()


def test_drop_relation_has_no_cascade():
    rec = _render("duckhaven__drop_relation", _Relation())
    sql = rec.statement_sql.lower()
    assert "drop table if exists" in sql
    assert "cascade" not in sql


def test_load_csv_rows_batches_parameterized_inserts():
    columns = ["id", "name"]
    rows = [(1, "a"), (2, "b"), (3, "c")]
    agate = type("Agate", (), {"rows": rows, "column_names": columns})()
    rec = _render("duckhaven__load_csv_rows", {"name": "seed"}, agate)

    # batch_size 2 over 3 rows → two INSERT statements.
    assert len(rec.queries) == 2
    first_sql, first_bindings = rec.queries[0]
    assert "insert into" in first_sql.lower()
    assert "?" in first_sql
    # First batch binds the first two rows, flattened.
    assert first_bindings == [1, "a", 2, "b"]
    second_sql, second_bindings = rec.queries[1]
    assert second_bindings == [3, "c"]
