"""Credentials for the ``duckhaven`` adapter.

Maps ``profiles.yml`` (``host`` / ``workspace`` / ``token`` / ``agent`` / ``catalog`` /
``schema``) onto dbt-duckdb's model: ``catalog`` is the dbt *database* (a Polaris
catalog), and execution is remote, so there is no local DuckDB file/path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbt_common.exceptions import DbtRuntimeError

from dbt.adapters.contracts.connection import Credentials
from dbt.adapters.duckdb.credentials import DuckDBCredentials


@dataclass
class DuckHavenCredentials(DuckDBCredentials):
    # DuckHaven connection — the Databricks host / http_path / token analog.
    host: str = ""
    workspace: str = ""
    token: str = ""
    # Compute selector, an agent UUID; omit to let the API auto-pick.
    agent: str | None = None
    # dbt "database" → Polaris catalog. Kept alongside dbt's inherited ``database``.
    catalog: str | None = None

    # Iceberg tables commit per statement via Polaris and the session is autocommit,
    # so models must never be wrapped in BEGIN/COMMIT.
    disable_transactions: bool = True

    @property
    def type(self) -> str:
        return "duckhaven"

    @property
    def unique_field(self) -> str:
        # Identifies the connection for dbt's connection cache.
        return f"{self.host}|{self.workspace}|{self.agent}|{self.database}"

    def _connection_keys(self):
        # Fields shown by ``dbt debug`` (token is deliberately omitted).
        return ("host", "workspace", "agent", "catalog", "schema")

    @classmethod
    def __pre_deserialize__(cls, data: dict[Any, Any]) -> dict[Any, Any]:
        # Bypass DuckDBCredentials' path/database consistency enforcement — there is no
        # local DuckDB file here. Map ``catalog`` onto dbt's ``database`` and set a
        # sentinel path so inherited code that reads ``path`` stays happy.
        data = Credentials.__pre_deserialize__(data)
        if data.get("catalog") and not data.get("database"):
            data["database"] = data["catalog"]
        data.setdefault("path", ":memory:")
        data.setdefault("database", "memory")
        return data

    def __post_init__(self):
        super().__post_init__()
        if not (self.host and self.workspace and self.token):
            raise DbtRuntimeError(
                "dbt-duckhaven requires 'host', 'workspace', and 'token' in profiles.yml"
            )
        if self.remote is not None:
            raise DbtRuntimeError(
                "dbt-duckhaven does not use 'remote'; it routes through the DuckHaven API"
            )
