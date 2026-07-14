"""Macro dispatch inheritance: unresolved `duckhaven__x` must fall back to `duckdb__x`.

This is what lets us reuse dbt-duckdb's materializations and macros while overriding
only a couple. It hinges entirely on the plugin's dependencies=["duckdb"].
"""

from dbt.adapters.factory import FACTORY


def test_dispatch_order_prefers_duckhaven_then_duckdb():
    FACTORY.load_plugin("duckhaven")
    assert FACTORY.get_adapter_type_names("duckhaven") == ["duckhaven", "duckdb"]


def test_macro_package_search_includes_duckdb_and_our_overrides():
    FACTORY.load_plugin("duckhaven")
    packages = FACTORY.get_adapter_package_names("duckhaven")
    # Our overrides win, dbt-duckdb's macros back us up, global is the final fallback.
    assert packages[:2] == ["dbt_duckhaven", "dbt_duckdb"]


def test_our_include_path_is_first_on_the_search_path():
    FACTORY.load_plugin("duckhaven")
    paths = [str(p) for p in FACTORY.get_include_paths("duckhaven")]
    assert paths[0].endswith("dbt/include/duckhaven")
