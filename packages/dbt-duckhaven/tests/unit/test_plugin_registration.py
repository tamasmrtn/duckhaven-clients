"""The adapter is discoverable and wired for macro-dispatch inheritance from duckdb."""

import importlib

from dbt.adapters.duckhaven import Plugin
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt.adapters.duckhaven.impl import DuckHavenAdapter


def test_namespace_module_exposes_plugin():
    mod = importlib.import_module("dbt.adapters.duckhaven")
    assert mod.Plugin is Plugin


def test_plugin_wires_adapter_and_credentials():
    assert Plugin.adapter is DuckHavenAdapter
    assert Plugin.credentials is DuckHavenCredentials
    assert Plugin.adapter.type() == "duckhaven"


def test_plugin_depends_on_duckdb_for_macro_dispatch():
    # dependencies=["duckdb"] is what makes `duckhaven__x` fall back to `duckdb__x`.
    assert "duckdb" in Plugin.dependencies


def test_include_path_points_at_our_macros():
    assert Plugin.include_path.endswith("dbt/include/duckhaven")
