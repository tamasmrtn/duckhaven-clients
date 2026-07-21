"""Render-level coverage for the DuckHaven macro overrides.

These macros are the DuckHaven-specific SQL and otherwise only run in the env-gated
integration suites. Here we render each one through a minimal Jinja environment with
stubbed dbt context helpers and assert on what it emits — no live server, so it runs in
normal CI.

Scope note: for the merge and snapshot overrides these tests prove *dispatch wiring* — that
we delegate to the dbt-core macro rather than dbt-duckdb's, with the arguments in the right
order — not that the resulting SQL is valid against Iceberg. Only the live suites can prove
the latter.

The dbt ``{% call statement(...) %}`` block needs a real Jinja macro as its target (so
``caller()`` is defined), so we prepend a tiny ``statement`` macro that forwards the
rendered block body to a Python recorder. Everything else is a plain global stub.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

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
    def __init__(self, recorder: _Recorder, relations: list[dict] | None = None) -> None:
        self._recorder = recorder
        self._relations = relations or []

    @staticmethod
    def quote(value: str) -> str:
        return f'"{value}"'

    def add_query(self, sql, bindings=None, abridge_sql_log=False):
        self._recorder.queries.append((str(sql), list(bindings or [])))

    def list_relation_names(self, database, schema):
        self._recorder.listed.append((database, schema))
        return self._relations


class _CompilerError(Exception):
    """Stands in for dbt's compiler error so tests can assert it was raised."""


class _Config:
    """A stand-in for dbt's model ``config`` context object."""

    def __init__(self, values: dict | None = None) -> None:
        self._values = values or {}

    def get(self, name, default=None):
        return self._values.get(name, default)


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[tuple[str, str]] = []
        self.queries: list[tuple[str, list]] = []
        self.calls: list[tuple] = []
        # (database, schema) pairs adapter.list_relation_names was asked for.
        self.listed: list[tuple[str, str]] = []
        self.returned = None

    @property
    def statement_sql(self) -> str:
        return "\n".join(sql for _, sql in self.statements)

    def record(self, name, *args):
        """Record a delegated macro call and return a recognisable marker."""
        self.calls.append((name, *args))
        return f"<{name}>"


def _render(macro: str, *args, run_query_rows=None, config=None, relations=None) -> _Recorder:
    """Render one macro and return the recorder holding the SQL it emitted."""
    recorder = _Recorder()
    env = jinja2.Environment(extensions=["jinja2.ext.do"])

    def _raise_compiler_error(msg):
        raise _CompilerError(str(msg))

    env.globals.update(
        {
            "_record_statement": lambda name, sql: recorder.statements.append((name, str(sql))),
            "execute": True,
            "run_query": lambda sql: run_query_rows or [],
            "load_result": lambda name: type("R", (), {"table": "COLUMNS_TABLE"}),
            "sql_convert_columns_in_relation": lambda table: table,
            "return": lambda value: setattr(recorder, "returned", value) or "",
            "adapter": _Adapter(recorder, relations),
            "this": _Relation(),
            "config": _Config(config),
            "exceptions": type(
                "E", (), {"raise_compiler_error": staticmethod(_raise_compiler_error)}
            ),
            "get_batch_size": lambda: 2,
            "get_binding_char": lambda: "?",
            "get_seed_column_quoted_csv": lambda model, names: ", ".join(f'"{n}"' for n in names),
            # Delegation targets — recorded so tests can assert which macro we routed to.
            "default__get_merge_sql": lambda *a: recorder.record("default__get_merge_sql", *a),
            "default__snapshot_merge_sql": lambda *a: recorder.record(
                "default__snapshot_merge_sql", *a
            ),
            # Mirrors dbt-duckdb's normalize_incremental_predicates: none -> [], a bare
            # string -> [string], a sequence -> list.
            "normalize_incremental_predicates": lambda p: (
                [] if p is None else ([p] if isinstance(p, str) else list(p))
            ),
            "make_temp_relation": lambda rel: "TEMP_RELATION",
            "snapshot_staging_table": lambda *a: "STAGING_SELECT",
            "create_table_as": lambda *a: recorder.record("create_table_as", *a),
        }
    )
    source = (
        _PREAMBLE
        + (_MACROS_DIR / "adapters.sql").read_text()
        + (_MACROS_DIR / "seed.sql").read_text()
        + (_MACROS_DIR / "materializations/incremental_strategy/merge.sql").read_text()
        + (_MACROS_DIR / "materializations/snapshot.sql").read_text()
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
    relations = [
        {"table_name": "orders", "table_type": "MANAGED"},
        {"table_name": "orders_view", "table_type": "VIEW"},
    ]
    rec = _render("duckhaven__drop_schema", _Relation(), relations=relations)
    names = [name for name, _ in rec.statements]
    # One drop per relation, then the schema drop.
    assert names == ["drop_1", "drop_2", "drop_schema"]
    per_relation = rec.statement_sql.lower()
    assert "drop table if exists" in per_relation
    assert "drop view if exists" in per_relation
    schema_drop = dict(rec.statements)["drop_schema"].lower()
    assert "drop schema if exists" in schema_drop
    assert "cascade" not in rec.statement_sql.lower()


def test_drop_schema_lists_relations_via_the_adapter_not_information_schema():
    """information_schema.tables is rejected outright on a workspace with any scoped
    catalog attached, which made this macro fail on every drop_schema there."""
    rec = _render(
        "duckhaven__drop_schema",
        _Relation(),
        relations=[{"table_name": "orders", "table_type": "MANAGED"}],
    )
    assert rec.listed == [("sales", "analytics")]
    assert "information_schema" not in rec.statement_sql.lower()


def test_drop_schema_of_an_empty_schema_only_drops_the_schema():
    rec = _render("duckhaven__drop_schema", _Relation(), relations=[])
    assert [name for name, _ in rec.statements] == ["drop_schema"]


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


def _merge_args(**overrides):
    args = {
        "target_relation": "TARGET",
        "temp_relation": "TEMP",
        "unique_key": "id",
        "dest_columns": ["id", "name"],
        "incremental_predicates": None,
    }
    args.update(overrides)
    return args


def test_merge_delegates_to_dbt_core_default_not_duckdb_by_name():
    # dbt-duckdb's duckdb__get_merge_sql emits UPDATE BY NAME / INSERT BY NAME, which
    # duckdb-iceberg does not document. We must route to dbt-core's explicit-column form.
    rec = _render("duckhaven__get_incremental_merge_sql", _merge_args())
    assert rec.calls == [("default__get_merge_sql", "TARGET", "TEMP", "id", ["id", "name"], [])]
    assert rec.returned == "<default__get_merge_sql>"


def test_merge_override_targets_the_dispatched_macro():
    # duckdb__get_incremental_merge_sql calls duckdb__get_merge_sql directly rather than
    # through adapter.dispatch, so only get_INCREMENTAL_merge_sql is a live override point.
    # Renaming ours to duckhaven__get_merge_sql would make it dead code and silently
    # restore the BY NAME form.
    source = (_MACROS_DIR / "materializations/incremental_strategy/merge.sql").read_text()
    assert "macro duckhaven__get_incremental_merge_sql" in source


def test_merge_normalizes_incremental_predicates_before_delegating():
    # dbt-core's default__get_merge_sql does `[] + incremental_predicates`, which raises on a
    # bare string — so the string must be normalized to a list first, as dbt-duckdb does.
    rec = _render("duckhaven__get_incremental_merge_sql", _merge_args(incremental_predicates="a=1"))
    assert rec.calls[0][-1] == ["a=1"]


@pytest.mark.parametrize(
    "unsupported",
    [
        "merge_clauses",
        "merge_returning_columns",
        "merge_on_using_columns",
        "merge_update_condition",
        "merge_insert_condition",
        "merge_update_set_expressions",
    ],
)
def test_merge_rejects_duckdb_only_configs(unsupported):
    # Routing through dbt-core's default drops these. Silently ignoring them would write
    # wrong data (merge_update_condition would widen the merge to every matched row).
    with pytest.raises(_CompilerError) as excinfo:
        _render(
            "duckhaven__get_incremental_merge_sql",
            _merge_args(),
            config={unsupported: ["x"]},
        )
    assert unsupported in str(excinfo.value)


@pytest.mark.parametrize("supported", ["merge_update_columns", "merge_exclude_columns"])
def test_merge_allows_configs_dbt_core_default_honours(supported):
    rec = _render("duckhaven__get_incremental_merge_sql", _merge_args(), config={supported: ["a"]})
    assert rec.calls[0][0] == "default__get_merge_sql"


def test_snapshot_merge_delegates_to_dbt_core_merge_not_duckdb_update_from():
    # dbt-duckdb's duckdb__snapshot_merge_sql uses a joined UPDATE ... FROM, undocumented on
    # Iceberg. dbt-core's default uses MERGE INTO, which duckdb-iceberg documents since 1.5.3.
    rec = _render("duckhaven__snapshot_merge_sql", "TARGET", "SOURCE", ["id", "name"])
    assert rec.calls == [("default__snapshot_merge_sql", "TARGET", "SOURCE", ["id", "name"])]


def test_snapshot_staging_table_is_a_local_temp_table():
    # dbt-duckdb builds a REAL table here for MotherDuck; on DuckHaven the staging relation
    # renders unqualified, so that would land Iceberg churn in the session's default
    # namespace. The first create_table_as arg is `temporary` and must stay True.
    rec = _render("build_snapshot_staging_table", "STRATEGY", "SQL", "TARGET")
    create_calls = [c for c in rec.calls if c[0] == "create_table_as"]
    assert len(create_calls) == 1
    assert create_calls[0][1] is True
    assert rec.returned == "TEMP_RELATION"
