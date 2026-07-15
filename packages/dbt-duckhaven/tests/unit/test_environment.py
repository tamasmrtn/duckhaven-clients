"""The environment routes handle() through the connector and refuses Python jobs."""

import pytest
from dbt.adapters.duckhaven import environments
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt.adapters.duckhaven.environments import DuckHavenEnvironment
from dbt_common.exceptions import DbtRuntimeError


def make_creds(**overrides):
    data = {
        "host": "https://dh.internal",
        "workspace": "analytics",
        "token": "dh_pat_x",
        "catalog": "sales",
        "schema": "analytics",
    }
    data.update(overrides)
    return DuckHavenCredentials.from_dict(data)


def test_handle_opens_a_connector_session_with_mapped_args(monkeypatch):
    calls = {}

    def fake_connect(**kwargs):
        calls.update(kwargs)
        return "SESSION"

    monkeypatch.setattr(environments, "connect", fake_connect)
    env = DuckHavenEnvironment(make_creds(agent="00000000-0000-0000-0000-000000000009"))

    handle = env.handle()

    assert handle == "SESSION"
    # schema is deliberately not forwarded: dbt fully-qualifies relations and creates
    # schemas itself, so a session-level USE would only fail on not-yet-created schemas.
    application = calls.pop("application")
    assert application.startswith("dbt-duckhaven/")
    assert calls == {
        "host": "https://dh.internal",
        "workspace": "analytics",
        "token": "dh_pat_x",
        "agent": "00000000-0000-0000-0000-000000000009",
        "catalog": "sales",
    }


def test_binding_char_is_qmark():
    assert DuckHavenEnvironment(make_creds()).get_binding_char() == "?"


def test_not_cancelable_in_v1():
    assert DuckHavenEnvironment.is_cancelable() is False


def test_python_models_are_refused():
    env = DuckHavenEnvironment(make_creds())
    with pytest.raises(DbtRuntimeError, match="Python models are not supported"):
        env.submit_python_job(handle=None, parsed_model={}, compiled_code="x")


def test_source_and_store_plugins_are_refused():
    env = DuckHavenEnvironment(make_creds())
    with pytest.raises(DbtRuntimeError):
        env.load_source("p", None)
    with pytest.raises(DbtRuntimeError):
        env.store_relation("p", None)
