"""open() builds a DuckHaven environment (not dbt-duckdb's factory) and pins one
session per connection."""

from types import SimpleNamespace

import pytest
from dbt.adapters.contracts.connection import ConnectionState
from dbt.adapters.duckhaven import environments
from dbt.adapters.duckhaven.connections import DuckHavenConnectionManager
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt.adapters.duckhaven.environments import DuckHavenEnvironment
from dbt.adapters.exceptions import FailedToConnectError


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


@pytest.fixture(autouse=True)
def _reset_env():
    DuckHavenConnectionManager._ENV = None
    yield
    DuckHavenConnectionManager._ENV = None


def test_open_builds_duckhaven_environment(monkeypatch):
    sessions = []

    def fake_connect(**kwargs):
        session = SimpleNamespace(id=len(sessions))
        sessions.append(session)
        return session

    monkeypatch.setattr(environments, "connect", fake_connect)
    conn = SimpleNamespace(state=ConnectionState.INIT, credentials=make_creds(), handle=None)

    DuckHavenConnectionManager.open(conn)

    assert isinstance(DuckHavenConnectionManager._ENV, DuckHavenEnvironment)
    assert conn.state == ConnectionState.OPEN
    assert conn.handle is sessions[0]


def test_each_open_handle_is_its_own_session(monkeypatch):
    # dbt opens one connection per thread; each must get its own DuckHaven session.
    sessions = []
    monkeypatch.setattr(
        environments, "connect", lambda **kw: sessions.append(object()) or sessions[-1]
    )
    env = DuckHavenEnvironment(make_creds())
    assert env.handle() is not env.handle()


def test_open_failure_becomes_failed_to_connect(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no agent")

    monkeypatch.setattr(environments, "connect", boom)
    conn = SimpleNamespace(state=ConnectionState.INIT, credentials=make_creds(), handle=None)

    with pytest.raises(FailedToConnectError):
        DuckHavenConnectionManager.open(conn)
    assert conn.state == ConnectionState.FAIL
