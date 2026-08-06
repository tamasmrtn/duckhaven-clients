"""open() builds a DuckHaven environment (not dbt-duckdb's factory) and pins one
session per connection, and commit_if_has_connection() skips commit() when no
transaction is open."""

from types import SimpleNamespace

import pytest
from dbt.adapters.contracts.connection import ConnectionState
from dbt.adapters.duckdb.connections import DuckDBConnectionManager
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
    # The env is stored on the base class (dbt-duckdb reads it there).
    DuckDBConnectionManager._ENV = None
    yield
    DuckDBConnectionManager._ENV = None


def test_open_builds_duckhaven_environment(monkeypatch):
    sessions = []

    def fake_connect(**kwargs):
        session = SimpleNamespace(id=len(sessions))
        sessions.append(session)
        return session

    monkeypatch.setattr(environments, "connect", fake_connect)
    conn = SimpleNamespace(state=ConnectionState.INIT, credentials=make_creds(), handle=None)

    DuckHavenConnectionManager.open(conn)

    assert isinstance(DuckDBConnectionManager._ENV, DuckHavenEnvironment)
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


def _manager_with(monkeypatch, connection):
    manager = DuckHavenConnectionManager.__new__(DuckHavenConnectionManager)
    calls = []
    monkeypatch.setattr(manager, "get_if_exists", lambda: connection)
    monkeypatch.setattr(manager, "commit", lambda: calls.append("commit"))
    return manager, calls


def test_commit_if_has_connection_skips_commit_without_open_transaction(monkeypatch):
    # The benign case this fix targets: DuckDB already closed the transaction by the
    # time this cleanup pass runs. Must not raise and must not call commit().
    connection = SimpleNamespace(name="test", transaction_open=False)
    manager, calls = _manager_with(monkeypatch, connection)

    manager.commit_if_has_connection()

    assert calls == []


def test_commit_if_has_connection_commits_when_transaction_open(monkeypatch):
    connection = SimpleNamespace(name="test", transaction_open=True)
    manager, calls = _manager_with(monkeypatch, connection)

    manager.commit_if_has_connection()

    assert calls == ["commit"]


def test_commit_if_has_connection_noop_without_a_connection(monkeypatch):
    # Regression coverage for the existing base-class behaviour: no connection at all
    # (get_if_exists() returns None) must also skip commit(), unchanged by this fix.
    manager, calls = _manager_with(monkeypatch, None)

    manager.commit_if_has_connection()

    assert calls == []
