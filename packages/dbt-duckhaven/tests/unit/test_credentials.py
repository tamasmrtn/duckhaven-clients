"""Credential mapping: profiles.yml fields → dbt-duckdb credential shape."""

import pytest
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt_common.exceptions import DbtRuntimeError


def make(**overrides):
    data = {
        "host": "https://dh.internal",
        "workspace": "analytics",
        "token": "dh_pat_x",
        "catalog": "sales",
        "schema": "analytics",
    }
    data.update(overrides)
    return DuckHavenCredentials.from_dict(data)


def test_type_is_duckhaven():
    assert make().type == "duckhaven"


def test_catalog_maps_to_database():
    creds = make(catalog="sales")
    assert creds.database == "sales"
    assert creds.schema == "analytics"


def test_transactions_disabled_by_default():
    # The DuckHaven session is autocommit; models must not be wrapped in BEGIN/COMMIT.
    assert make().disable_transactions is True


def test_unique_field_distinguishes_connection():
    a = make(agent="00000000-0000-0000-0000-000000000001")
    b = make(agent="00000000-0000-0000-0000-000000000002")
    assert a.unique_field != b.unique_field
    assert "sales" in a.unique_field


def test_connection_keys_hide_token():
    assert "token" not in make()._connection_keys()


def test_connection_keys_include_database():
    # dbt's Jinja `target` is built from _connection_keys; `database` must be present
    # or generate_database_name renders an empty catalog into every relation.
    keys = make()._connection_keys()
    assert "database" in keys
    assert dict(make().connection_info())["database"] == "sales"


def test_missing_required_field_raises():
    with pytest.raises(DbtRuntimeError):
        make(token="")


def test_remote_field_rejected():
    # `remote` would route to dbt-duckdb's Buena Vista environment, not DuckHaven.
    with pytest.raises(DbtRuntimeError):
        make(remote={"host": "h", "port": 5432, "user": "u"})
