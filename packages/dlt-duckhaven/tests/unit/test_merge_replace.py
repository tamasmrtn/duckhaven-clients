"""merge / replace wiring and the DESCRIBE-based schema introspection.

merge and replace are inherited from the staging-dataset-aware SQL job client; these tests
lock the DuckHaven-specific behavior: delete-insert merge against the staging dataset,
insert-from-staging replace, DELETE-based (not TRUNCATE) truncation, and DESCRIBE-based
column introspection for schema evolution on attached Iceberg catalogs.
"""

import pathlib
from unittest.mock import MagicMock

import pytest
from dlt.common.schema import Schema
from dlt.common.schema.utils import new_table
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt_duckhaven.client import DuckHavenJobClient, _parse_duckdb_type
from dlt_duckhaven.factory import duckhaven


def _job_client(table, *, replace_strategy=None):
    factory = duckhaven(host="https://h", workspace="ws", catalog="raw", credentials="dh_pat_x")
    config = factory.configuration(factory.spec(), accept_partial=True)
    config.dataset_name = "analytics"
    if replace_strategy:
        config.replace_strategy = replace_strategy
    schema = Schema("events")
    schema.update_table(table)
    return DuckHavenJobClient(schema, config, factory.capabilities())


def _merge_table():
    return new_table(
        "orders",
        write_disposition="merge",
        columns=[
            {"name": "id", "data_type": "bigint", "primary_key": True, "nullable": False},
            {"name": "amount", "data_type": "decimal", "precision": 10, "scale": 2},
        ],
    )


def _read_job_sql(job):
    return pathlib.Path(job.new_file_path()).read_text()


def test_merge_generates_delete_insert_against_staging_dataset():
    client = _job_client(_merge_table())
    prepared = client.prepare_load_table("orders")
    jobs = client._create_merge_followup_jobs([prepared])
    assert [type(j).__name__ for j in jobs] == ["SqlMergeFollowupJob"]

    sql = _read_job_sql(jobs[0])
    # delete-insert: delete matching keys from the target, then insert deduped staged rows.
    assert 'DELETE FROM "raw"."analytics"."orders"' in sql
    assert 'FROM "raw"."analytics_staging"."orders"' in sql
    assert 'INSERT INTO "raw"."analytics"."orders"' in sql


def test_merge_loads_to_staging_dataset():
    client = _job_client(_merge_table())
    assert client.should_load_data_to_staging_dataset("orders") is True


def test_append_does_not_load_to_staging_dataset():
    client = _job_client(new_table("evts", write_disposition="append", columns=[]))
    assert client.should_load_data_to_staging_dataset("evts") is False


def test_replace_insert_from_staging_uses_staging_replace_job():
    client = _job_client(
        new_table(
            "orders", write_disposition="replace", columns=[{"name": "id", "data_type": "bigint"}]
        ),
        replace_strategy="insert-from-staging",
    )
    prepared = client.prepare_load_table("orders")
    jobs = client._create_replace_followup_jobs([prepared])
    assert [type(j).__name__ for j in jobs] == ["SqlStagingReplaceFollowupJob"]


def test_replace_truncate_and_insert_truncates_before_load():
    client = _job_client(
        new_table(
            "orders", write_disposition="replace", columns=[{"name": "id", "data_type": "bigint"}]
        ),
        replace_strategy="truncate-and-insert",
    )
    assert client.should_truncate_table_before_load("orders") is True


def test_truncate_sql_is_delete_based_not_truncate_command():
    client = _job_client(new_table("orders", write_disposition="replace", columns=[]))
    qualified = client.sql_client.make_qualified_table_name("orders")
    sql = client.sql_client._truncate_table_sql(qualified)
    assert sql == f"DELETE FROM {qualified} WHERE 1=1"


def test_get_storage_tables_parses_describe_output():
    client = _job_client(_merge_table())
    client.sql_client.execute_sql = MagicMock(
        return_value=[
            ("id", "BIGINT", "NO", None, None, None),
            ("amount", "DECIMAL(10,2)", "YES", None, None, None),
            ("name", "VARCHAR", "YES", None, None, None),
        ]
    )
    columns = dict(client.get_storage_tables(["orders"]))["orders"]

    assert columns["id"]["data_type"] == "bigint"
    assert columns["id"]["nullable"] is False
    assert columns["amount"]["data_type"] == "decimal"
    assert columns["amount"]["precision"] == 10
    assert columns["amount"]["scale"] == 2
    assert columns["name"]["data_type"] == "text"
    # DESCRIBE targets the fully-qualified table, not INFORMATION_SCHEMA.
    assert client.sql_client.execute_sql.call_args.args[0].startswith("DESCRIBE ")


def test_get_storage_tables_missing_table_yields_no_columns():
    client = _job_client(_merge_table())
    client.sql_client.execute_sql = MagicMock(
        side_effect=DatabaseUndefinedRelation(Exception("Table orders does not exist"))
    )
    assert dict(client.get_storage_tables(["ghost"]))["ghost"] == {}


@pytest.mark.parametrize(
    "type_text,expected",
    [
        ("VARCHAR", ("VARCHAR", None, None)),
        ("DECIMAL(10,2)", ("DECIMAL", 10, 2)),
        ("TIMESTAMP WITH TIME ZONE", ("TIMESTAMP WITH TIME ZONE", None, None)),
        ("BIGINT", ("BIGINT", None, None)),
    ],
)
def test_parse_duckdb_type(type_text, expected):
    assert _parse_duckdb_type(type_text) == expected
