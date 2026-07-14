"""dbt-duckhaven — a dbt adapter routing DuckDB SQL through the DuckHaven API.

Discovered by dbt-core via the ``dbt.adapters.duckhaven`` namespace package: dbt imports
this module for ``type: duckhaven`` and reads the module-level ``Plugin``.
"""

from dbt.adapters.base import AdapterPlugin
from dbt.adapters.duckhaven.connections import DuckHavenConnectionManager  # noqa: F401
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt.adapters.duckhaven.impl import DuckHavenAdapter
from dbt.include import duckhaven

from .__version__ import version as __version__  # noqa: F401 - public package version

# dependencies=["duckdb"] loads dbt-duckdb's macros and puts "duckdb" in the dispatch
# search order, so `duckhaven__<macro>` falls back to `duckdb__<macro>`.
Plugin = AdapterPlugin(
    adapter=DuckHavenAdapter,  # type: ignore[arg-type]
    credentials=DuckHavenCredentials,
    include_path=duckhaven.PACKAGE_PATH,
    dependencies=["duckdb"],
)
