"""open() builds a DuckHaven environment (not dbt-duckdb's factory) and pins one
session per connection; release() keeps that session for the thread's next node and
add_query() reopens it once if the server dropped it; commit_if_has_connection() skips
commit() when no transaction is open."""

import multiprocessing
import threading
from types import SimpleNamespace

import pytest
from dbt.adapters.contracts.connection import ConnectionState
from dbt.adapters.duckdb.connections import DuckDBConnectionManager
from dbt.adapters.duckhaven import environments
from dbt.adapters.duckhaven.connections import DuckHavenConnectionManager
from dbt.adapters.duckhaven.credentials import DuckHavenCredentials
from dbt.adapters.duckhaven.environments import DuckHavenEnvironment
from dbt.adapters.exceptions import FailedToConnectError
from dbt_common.exceptions import DbtRuntimeError

from duckhaven_sql_connector import OperationalError, ProgrammingError


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
    # Every open is its own DuckHaven session; reuse across nodes happens one level up,
    # in the connection manager (see the release() tests below).
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


# -- Session reuse across nodes ---------------------------------------------


def _session_lost():
    return OperationalError("[409] session is not open", code="session_not_open", status_code=409)


class FakeSession:
    """A connector Connection stand-in: records statements, raises queued errors."""

    def __init__(self, errors):
        self.errors = errors
        self.statements = []
        self.closed = 0

    def cursor(self):
        return self

    def execute(self, sql, bindings=None):
        if self.errors:
            raise self.errors.pop(0)
        self.statements.append(sql)

    def rollback(self):
        pass

    def close(self):
        self.closed += 1


class Sessions(list):
    """Every session opened so far; ``errors`` queues the errors for each next one."""

    def __init__(self):
        super().__init__()
        self.errors = []


@pytest.fixture
def sessions(monkeypatch):
    opened = Sessions()

    def fake_connect(**kwargs):
        opened.append(FakeSession(opened.errors.pop(0) if opened.errors else []))
        return opened[-1]

    monkeypatch.setattr(environments, "connect", fake_connect)
    return opened


def make_manager():
    config = SimpleNamespace(credentials=make_creds())
    return DuckHavenConnectionManager(config, multiprocessing.get_context("spawn"))


def run_node(manager, name, sql="select 1"):
    """What dbt does per node: acquire the thread connection, run SQL, release it."""
    manager.set_connection_name(name)
    manager.add_query(sql, auto_begin=False)
    manager.release()


def test_release_keeps_the_session_for_the_threads_next_node(sessions):
    manager = make_manager()

    for name in ("model.a", "model.b", "test.c"):
        run_node(manager, name)

    assert len(sessions) == 1
    assert sessions[0].statements == ["select 1"] * 3
    assert sessions[0].closed == 0
    assert manager.get_if_exists().state == ConnectionState.OPEN


def test_cleanup_all_closes_the_kept_session(sessions):
    manager = make_manager()
    run_node(manager, "model.a")

    manager.cleanup_all()

    assert sessions[0].closed == 1
    assert manager.thread_connections == {}


def test_each_thread_keeps_its_own_session(sessions):
    manager = make_manager()

    def worker(prefix):
        for i in range(3):
            run_node(manager, f"model.{prefix}{i}")

    threads = [threading.Thread(target=worker, args=(p,)) for p in "xy"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sessions) == 2
    assert [len(s.statements) for s in sessions] == [3, 3]


def test_release_closes_a_connection_still_flagged_in_a_transaction(sessions):
    manager = make_manager()
    manager.set_connection_name("model.a")
    manager.add_query("select 1", auto_begin=False)
    manager.get_if_exists().transaction_open = True

    manager.release()

    assert sessions[0].closed == 1
    assert manager.get_if_exists().state == ConnectionState.CLOSED


def test_a_lost_session_is_reopened_and_the_statement_retried(sessions):
    sessions.errors.append([_session_lost()])
    manager = make_manager()

    run_node(manager, "model.a", sql="create table t as select 1")

    assert len(sessions) == 2
    assert sessions[0].statements == []
    assert sessions[0].closed == 1
    assert sessions[1].statements == ["create table t as select 1"]
    assert manager.get_if_exists().handle is sessions[1]


def test_a_second_lost_session_fails_the_node_and_the_next_node_reopens(sessions):
    sessions.errors += [[_session_lost()], [_session_lost()]]
    manager = make_manager()

    with pytest.raises(DbtRuntimeError):
        run_node(manager, "model.a")
    assert manager.get_if_exists().state == ConnectionState.CLOSED

    run_node(manager, "model.b")

    assert len(sessions) == 3
    assert sessions[2].statements == ["select 1"]


def test_other_errors_are_not_retried(sessions):
    sessions.errors.append([ProgrammingError("[400] syntax error", status_code=400)])
    manager = make_manager()

    with pytest.raises(DbtRuntimeError, match="syntax error"):
        run_node(manager, "model.a")

    assert len(sessions) == 1
    assert manager.get_if_exists().state == ConnectionState.OPEN
