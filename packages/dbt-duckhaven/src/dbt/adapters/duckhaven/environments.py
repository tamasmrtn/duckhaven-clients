"""The DuckHaven dbt-duckdb *environment*: run SQL through the DuckHaven session API.

dbt-duckdb delegates "where code runs" to an ``Environment``. This one routes execution
through the ``duckhaven-sql-connector`` session client instead of an in-process DuckDB.
Each :meth:`handle` opens a fresh connector ``Connection`` (one DuckHaven session), so
every dbt thread gets its own session — mirroring dbt-databricks' session-per-connection.
"""

from __future__ import annotations

from dbt_common.exceptions import DbtRuntimeError

from dbt.adapters.contracts.connection import AdapterResponse, Connection
from dbt.adapters.duckdb.environments import Environment
from duckhaven_sql_connector import connect

from .credentials import DuckHavenCredentials


class DuckHavenEnvironment(Environment):
    def __init__(self, creds: DuckHavenCredentials) -> None:
        super().__init__(creds)

    def handle(self):
        creds: DuckHavenCredentials = self.creds  # type: ignore[assignment]
        # The connector's Connection is a DB-API handle: dbt drives it via
        # ``handle.cursor()`` → ``cursor.execute(sql, bindings)``. It also issues
        # ``USE catalog.schema`` on open, so dbt need not.
        return connect(
            host=creds.host,
            workspace=creds.workspace,
            token=creds.token,
            agent=creds.agent,
            catalog=creds.catalog or creds.database,
            schema=creds.schema,
        )

    def get_binding_char(self) -> str:
        # DuckDB paramstyle; the connector renders ``?`` placeholders client-side.
        return "?"

    @classmethod
    def is_cancelable(cls) -> bool:
        # The connector cancels per-statement (DELETE), but dbt's cancel() is
        # connection-scoped and we don't track the active cursor here. Revisit later.
        return False

    @classmethod
    def cancel(cls, connection: Connection) -> None:
        pass

    def submit_python_job(self, handle, parsed_model: dict, compiled_code: str) -> AdapterResponse:
        raise DbtRuntimeError(
            "dbt Python models are not supported by dbt-duckhaven; "
            "DuckHaven agents execute SQL only."
        )

    def load_source(self, plugin_name: str, source_config) -> str:
        raise DbtRuntimeError(
            "dbt-duckdb source plugins (load_source) are not supported by dbt-duckhaven."
        )

    def store_relation(self, plugin_name: str, target_config) -> None:
        raise DbtRuntimeError(
            "dbt-duckdb store plugins (store_relation) are not supported by dbt-duckhaven."
        )
