"""Session reuse across load jobs: a finished load job hands its DuckHaven session to the
package's next job instead of deleting it, nothing else ever pools one, completing the
package closes what is left, and a session the server dropped is reopened once."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import dlt_duckhaven.load_jobs as load_jobs
import dlt_duckhaven.sql_client as sql_client_mod
import pytest
from dlt.common.schema import Schema
from dlt.destinations.exceptions import DatabaseTransientException
from dlt_duckhaven.client import DuckHavenJobClient
from dlt_duckhaven.factory import duckhaven
from dlt_duckhaven.load_jobs import DuckHavenCopyJob

import duckhaven_sql_connector as dbapi


def _session_lost():
    return dbapi.OperationalError(
        "[409] session is closed", code="session_not_open", status_code=409
    )


@pytest.fixture(autouse=True)
def _empty_pool():
    sql_client_mod._IDLE_SESSIONS.clear()
    yield
    sql_client_mod._IDLE_SESSIONS.clear()


@pytest.fixture
def sessions(monkeypatch):
    """Every connect() returns a new MagicMock session; the list records them in order."""
    opened = []

    def connect(**kwargs):
        opened.append(MagicMock(name=f"session{len(opened)}"))
        return opened[-1]

    monkeypatch.setattr(sql_client_mod.dbapi, "connect", connect)
    return opened


def _job_client(monkeypatch, catalog="raw"):
    factory = duckhaven(host="https://h", workspace="ws", catalog=catalog, credentials="dh_pat_x")
    config = factory.configuration(factory.spec(), accept_partial=True)
    config.dataset_name = "analytics"
    client = DuckHavenJobClient(Schema("events"), config, factory.capabilities())
    # Query tags read the current pipeline, which a unit test does not have.
    monkeypatch.setattr(client, "_set_query_tags", lambda *a, **kw: None)
    return client


def _run_load_job(client, fail=False):
    """What dlt's run_managed does: open the client, prepare it for the job, run, close."""
    with client:
        client.prepare_load_job_execution(SimpleNamespace(_load_id="l1", _load_table={}))
        if fail:
            raise RuntimeError("job failed")


# -- The pool -------------------------------------------------------------------------


def test_load_jobs_reuse_one_session(monkeypatch, sessions):
    for _ in range(3):
        _run_load_job(_job_client(monkeypatch))

    assert len(sessions) == 1
    sessions[0].close.assert_not_called()


def test_concurrent_jobs_each_get_their_own_session(monkeypatch, sessions):
    first, second = _job_client(monkeypatch), _job_client(monkeypatch)
    with first, second:
        assert first.sql_client.native_connection is not second.sql_client.native_connection

    assert len(sessions) == 2


def test_a_session_is_only_reused_with_the_same_settings(monkeypatch, sessions):
    _run_load_job(_job_client(monkeypatch, catalog="raw"))
    _run_load_job(_job_client(monkeypatch, catalog="other"))

    assert len(sessions) == 2


def test_a_client_that_ran_no_load_job_closes_its_session(monkeypatch, sessions):
    # Schema sync, state restore, pipeline.sql_client() reads: none should leave a session
    # sitting in the pool with nothing to collect it.
    with _job_client(monkeypatch):
        pass

    sessions[0].close.assert_called_once()
    assert sql_client_mod._IDLE_SESSIONS == {}


def test_a_failed_job_closes_its_session(monkeypatch, sessions):
    with pytest.raises(RuntimeError):
        _run_load_job(_job_client(monkeypatch), fail=True)

    sessions[0].close.assert_called_once()
    _run_load_job(_job_client(monkeypatch))
    assert len(sessions) == 2


def test_a_non_load_client_takes_a_pooled_session(monkeypatch, sessions):
    _run_load_job(_job_client(monkeypatch))
    with _job_client(monkeypatch) as client:
        assert client.sql_client.native_connection is sessions[0]

    assert len(sessions) == 1
    sessions[0].close.assert_called_once()


def test_complete_load_closes_every_pooled_session(monkeypatch, sessions):
    first, second = _job_client(monkeypatch), _job_client(monkeypatch)
    with first, second:
        first.prepare_load_job_execution(SimpleNamespace(_load_id="l1", _load_table={}))
        second.prepare_load_job_execution(SimpleNamespace(_load_id="l1", _load_table={}))
    assert len(sessions) == 2

    # The completing client takes one pooled session; complete_load closes the other, and
    # leaving the client closes its own, since it ran no load job.
    with _job_client(monkeypatch) as completing:
        monkeypatch.setattr(completing.sql_client, "execute_sql", MagicMock())
        completing.complete_load("l1")

    assert len(sessions) == 2
    assert sql_client_mod._IDLE_SESSIONS == {}
    for session in sessions:
        session.close.assert_called_once()


def test_close_idle_sessions_ignores_close_errors():
    gone, fine = MagicMock(), MagicMock()
    gone.close.side_effect = RuntimeError("already gone")
    sql_client_mod._IDLE_SESSIONS["key"] = [gone, fine]

    sql_client_mod.close_idle_sessions()

    fine.close.assert_called_once()
    assert sql_client_mod._IDLE_SESSIONS == {}


# -- Recovering a dropped session -----------------------------------------------------


def _open_client(monkeypatch, sessions, *cursor_effects):
    """A connected sql client whose n-th session's cursor.execute has the n-th effect."""
    effects = list(cursor_effects)

    def connect(**kwargs):
        session = MagicMock(name=f"session{len(sessions)}")
        session.cursor.return_value.description = None
        session.cursor.return_value.execute.side_effect = effects.pop(0) if effects else None
        sessions.append(session)
        return session

    monkeypatch.setattr(sql_client_mod.dbapi, "connect", connect)
    client = _job_client(monkeypatch).sql_client
    client.open_connection()
    return client


def test_a_statement_on_a_dropped_session_is_resent_on_a_new_one(monkeypatch, sessions):
    client = _open_client(monkeypatch, sessions, [_session_lost()])

    client.execute_sql("INSERT INTO t SELECT 1")

    assert len(sessions) == 2
    sessions[0].close.assert_called_once()
    sessions[1].cursor.return_value.execute.assert_called_once_with("INSERT INTO t SELECT 1")
    assert client.native_connection is sessions[1]


def test_only_the_refused_part_of_a_split_statement_is_resent(monkeypatch, sessions):
    # Parts before the refused one already ran (and committed) on the old session.
    client = _open_client(monkeypatch, sessions, [None, _session_lost()])

    client.execute_sql("ALTER TABLE t ADD COLUMN a INT; ALTER TABLE t ADD COLUMN b INT")

    resent = [c.args[0] for c in sessions[1].cursor.return_value.execute.call_args_list]
    assert resent == [" ALTER TABLE t ADD COLUMN b INT"]


def test_a_second_dropped_session_is_not_retried_again(monkeypatch, sessions):
    client = _open_client(monkeypatch, sessions, [_session_lost()], [_session_lost()])

    with pytest.raises(DatabaseTransientException):
        client.execute_sql("INSERT INTO t SELECT 1")

    assert len(sessions) == 2


def test_staging_on_a_dropped_session_is_retried_on_a_new_one(monkeypatch, sessions):
    client = _open_client(monkeypatch, sessions)
    monkeypatch.setattr(client, "execute_sql", MagicMock())
    stage = MagicMock(side_effect=[_session_lost(), "http://store/orders.parquet?sig=get"])
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)
    job = DuckHavenCopyJob("/tmp/orders.abc.0.parquet")
    job._job_client = SimpleNamespace(sql_client=client)
    job._load_table = {"name": "orders"}
    job._load_id = "l1"

    job.run()

    assert [c.args[0] for c in stage.call_args_list] == sessions
    assert "orders.parquet?sig=get" in client.execute_sql.call_args.args[0]


def test_other_staging_errors_are_not_retried(monkeypatch, sessions):
    client = _open_client(monkeypatch, sessions)
    stage = MagicMock(side_effect=dbapi.OperationalError("[503] no agent", status_code=503))
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)
    job = DuckHavenCopyJob("/tmp/orders.abc.0.parquet")
    job._job_client = SimpleNamespace(sql_client=client)
    job._load_table = {"name": "orders"}
    job._load_id = "l1"

    with pytest.raises(dbapi.OperationalError):
        job.run()

    assert len(sessions) == 1


def test_a_staged_insert_is_never_resent_on_another_session(monkeypatch, sessions):
    # The server lets a session read only its own staging prefix, so the INSERT that reads
    # a staged file cannot move to a new session; it fails transiently and dlt re-runs the
    # whole job, staging again.
    client = _open_client(monkeypatch, sessions, [_session_lost()])
    stage = MagicMock(return_value="http://store/orders.parquet?sig=get")
    monkeypatch.setattr(load_jobs._staging, "stage_file", stage)
    job = DuckHavenCopyJob("/tmp/orders.abc.0.parquet")
    job._job_client = SimpleNamespace(sql_client=client)
    job._load_table = {"name": "orders"}
    job._load_id = "l1"

    with pytest.raises(DatabaseTransientException):
        job.run()

    assert len(sessions) == 1


def test_a_reference_insert_may_move_to_a_new_session(monkeypatch, sessions):
    # An external staging destination's file is not tied to any session.
    client = _open_client(monkeypatch, sessions, [_session_lost()])
    monkeypatch.setattr(
        load_jobs.ReferenceFollowupJobRequest, "is_reference_job", staticmethod(lambda p: True)
    )
    monkeypatch.setattr(
        load_jobs.ReferenceFollowupJobRequest,
        "resolve_reference",
        staticmethod(lambda p: "s3://external/orders.parquet"),
    )
    job = DuckHavenCopyJob("/tmp/orders.abc.0.reference")
    job._job_client = SimpleNamespace(sql_client=client)
    job._load_table = {"name": "orders"}
    job._load_id = "l1"

    job.run()

    assert len(sessions) == 2
