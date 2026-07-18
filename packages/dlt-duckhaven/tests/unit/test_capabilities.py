"""Lock the DuckHaven destination capability profile so drift is caught."""

from dlt_duckhaven.factory import DuckHavenTypeMapper, duckhaven


def test_capabilities_profile():
    caps = duckhaven().capabilities()

    # Staged Parquet is the default load path; insert_values is the slow fallback.
    assert caps.preferred_loader_file_format == "parquet"
    assert caps.supported_loader_file_formats == ["parquet", "insert_values", "jsonl"]
    assert caps.preferred_staging_file_format == "parquet"
    assert caps.supported_staging_file_formats == ["parquet", "jsonl"]

    # Iceberg is advertised (inherent to the attached Polaris catalog).
    assert caps.supported_table_formats == ["iceberg"]

    # DuckDB dialect + escaping.
    assert caps.sqlglot_dialect == "duckdb"
    assert caps.has_case_sensitive_identifiers is False
    assert caps.escape_identifier is not None
    assert caps.escape_literal is not None

    # Iceberg precision constraints.
    assert caps.timestamp_precision == 6
    assert caps.decimal_precision[0] == 38

    # Autocommit session: no DDL transactions, one statement per submit, no TRUNCATE.
    assert caps.supports_ddl_transactions is False
    assert caps.supports_multiple_statements is False
    assert caps.supports_truncate_command is False

    # Load strategies (merge is delete-insert on Iceberg).
    assert caps.supported_merge_strategies == ["delete-insert"]
    assert caps.supported_replace_strategies == ["insert-from-staging", "truncate-and-insert"]

    assert caps.type_mapper is DuckHavenTypeMapper
