"""The ``duckhaven`` dlt destination factory, its capability profile, and the
DuckHaven→Iceberg type mapper.

DuckHaven stores every table as Iceberg via the attached Polaris catalog, so the type
mapper constrains dlt's types to what Iceberg (through DuckDB) accepts, and the
capabilities advertise a staged-Parquet SQL destination modeled on dlt's Athena (Iceberg)
and DuckDB destinations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dlt.common.arithmetics import DEFAULT_NUMERIC_PRECISION, DEFAULT_NUMERIC_SCALE
from dlt.common.data_writers.escape import escape_duckdb_literal, escape_postgres_identifier
from dlt.common.destination import Destination, DestinationCapabilitiesContext
from dlt.common.destination.typing import PreparedTableSchema
from dlt.common.exceptions import TerminalValueError
from dlt.common.schema.typing import TColumnSchema, TColumnType
from dlt.destinations.type_mapping import TypeMapperImpl

from dlt_duckhaven.configuration import DuckHavenClientConfiguration, DuckHavenCredentials

if TYPE_CHECKING:
    from dlt_duckhaven.client import DuckHavenJobClient


class DuckHavenTypeMapper(TypeMapperImpl):
    """Maps dlt data types to DuckDB SQL types that the agent can materialize as Iceberg.

    Iceberg has no JSON type (stored as VARCHAR), timestamps are microsecond-resolution
    (no nanoseconds), and there is no 128-bit integer (DuckDB HUGEINT is rejected — the
    Iceberg maximum is a 64-bit ``long``/BIGINT).
    """

    sct_to_unbound_dbt = {
        "json": "VARCHAR",
        "text": "VARCHAR",
        "double": "DOUBLE",
        "bool": "BOOLEAN",
        "date": "DATE",
        "timestamp": "TIMESTAMP WITH TIME ZONE",
        "bigint": "BIGINT",
        "binary": "BLOB",
        "time": "TIME",
    }

    sct_to_dbt = {
        "decimal": "DECIMAL(%i,%i)",
        "wei": "DECIMAL(%i,%i)",
    }

    dbt_to_sct = {
        "VARCHAR": "text",
        "DOUBLE": "double",
        "BOOLEAN": "bool",
        "DATE": "date",
        "TIMESTAMP WITH TIME ZONE": "timestamp",
        "TIMESTAMP": "timestamp",
        "BLOB": "binary",
        "DECIMAL": "decimal",
        "TIME": "time",
        "INTEGER": "bigint",
        "BIGINT": "bigint",
    }

    def to_db_integer_type(self, column: TColumnSchema, table: PreparedTableSchema = None) -> str:
        precision = column.get("precision")
        if precision is None:
            return "BIGINT"
        # precision is a bit-width; DuckDB coerces the sized int to the right Iceberg
        # int/long on write. 128-bit HUGEINT has no Iceberg equivalent, so reject it.
        if precision <= 8:
            return "TINYINT"
        elif precision <= 16:
            return "SMALLINT"
        elif precision <= 32:
            return "INTEGER"
        elif precision <= 64:
            return "BIGINT"
        raise TerminalValueError(
            f"bigint with `{precision=:}` exceeds Iceberg's 64-bit integer; HUGEINT is not"
            " an Iceberg type"
        )

    def to_db_datetime_type(self, column: TColumnSchema, table: PreparedTableSchema = None) -> str:
        precision = column.get("precision")
        if precision is not None and precision > 6:
            raise TerminalValueError(
                f"Iceberg timestamps are microsecond-resolution; `{precision=:}` (nanoseconds)"
                f" is unsupported for column `{column.get('name')}`"
            )
        timezone = column.get("timezone", True)
        return "TIMESTAMP WITH TIME ZONE" if timezone else "TIMESTAMP"

    def from_destination_type(
        self, db_type: str, precision: int | None, scale: int | None
    ) -> TColumnType:
        db_type = db_type.split("(")[0].strip().upper()
        if db_type == "DECIMAL" and precision == 38 and scale == 0:
            return dict(data_type="wei", precision=precision, scale=scale)
        return super().from_destination_type(db_type, precision, scale)


def _set_duckhaven_capabilities(
    caps: DestinationCapabilitiesContext,
) -> DestinationCapabilitiesContext:
    # Bulk loads are staged Parquet + COPY; insert_values is the slow, size-limited
    # fallback (as on MotherDuck).
    caps.preferred_loader_file_format = "parquet"
    caps.supported_loader_file_formats = ["parquet", "insert_values", "jsonl"]
    caps.preferred_staging_file_format = "parquet"
    caps.supported_staging_file_formats = ["parquet", "jsonl"]
    # Every DuckHaven table is Iceberg (inherent to the attached Polaris catalog), so we
    # advertise the format but do not force it per table; DDL stays plain CREATE TABLE.
    caps.supported_table_formats = ["iceberg"]
    caps.type_mapper = DuckHavenTypeMapper
    caps.escape_identifier = escape_postgres_identifier
    caps.escape_literal = escape_duckdb_literal
    caps.sqlglot_dialect = "duckdb"
    caps.has_case_sensitive_identifiers = False
    caps.decimal_precision = (DEFAULT_NUMERIC_PRECISION, DEFAULT_NUMERIC_SCALE)
    caps.wei_precision = (DEFAULT_NUMERIC_PRECISION, 0)
    caps.timestamp_precision = 6
    caps.max_identifier_length = 65536
    caps.max_column_identifier_length = 65536
    # Statements are submitted one-per-request as JSON through the session API.
    caps.max_query_length = 512 * 1024
    caps.is_max_query_length_in_bytes = True
    caps.max_text_data_type_length = 1024 * 1024 * 1024
    caps.is_max_text_data_type_length_in_bytes = True
    # The DuckHaven session is autocommit (each statement commits via Polaris); BEGIN/COMMIT
    # are connector-side no-ops, so DDL transactions are unavailable.
    caps.supports_ddl_transactions = False
    caps.supports_multiple_statements = False
    # Iceberg has no cheap TRUNCATE; replace truncation is done via `DELETE FROM` (Athena).
    caps.supports_truncate_command = False
    caps.supported_merge_strategies = ["delete-insert"]
    caps.supported_replace_strategies = ["insert-from-staging", "truncate-and-insert"]
    return caps


class duckhaven(Destination[DuckHavenClientConfiguration, "DuckHavenJobClient"]):
    spec = DuckHavenClientConfiguration

    def _raw_capabilities(self) -> DestinationCapabilitiesContext:
        return _set_duckhaven_capabilities(DestinationCapabilitiesContext())

    @property
    def client_class(self) -> type[DuckHavenJobClient]:
        from dlt_duckhaven.client import DuckHavenJobClient

        return DuckHavenJobClient

    def __init__(
        self,
        credentials: DuckHavenCredentials | dict[str, Any] | str = None,
        *,
        host: str = None,
        workspace: str = None,
        agent: str = None,
        catalog: str = None,
        destination_name: str = None,
        environment: str = None,
        **kwargs: Any,
    ) -> None:
        """Configure the DuckHaven destination for a pipeline.

        Arguments provided here supersede config files and environment variables.

        Args:
            credentials: A ``DuckHavenCredentials``, a ``{"token": …}`` mapping, or the
                ``dh_pat_…`` token string.
            host: The DuckHaven API base URL (``https://…``).
            workspace: The DuckHaven workspace slug.
            agent: Optional explicit compute (an agent UUID); omit to auto-pick.
            catalog: The DuckHaven catalog that qualifies loaded tables.
            destination_name: Name to disambiguate multiple ``duckhaven`` destinations.
            environment: Destination environment.
            **kwargs: Additional destination config.
        """
        super().__init__(
            credentials=credentials,
            host=host,
            workspace=workspace,
            agent=agent,
            catalog=catalog,
            destination_name=destination_name,
            environment=environment,
            **kwargs,
        )


duckhaven.register()
