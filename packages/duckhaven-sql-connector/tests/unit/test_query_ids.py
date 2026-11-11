"""Client-chosen query ids, cancelling a run while its submit is held, and tags."""

import json
import threading
import uuid

import httpx
import pytest
import respx

from duckhaven_sql_connector import connect
from duckhaven_sql_connector.connection import Connection
from duckhaven_sql_connector.dbapi import IntegrityError, OperationalError, ProgrammingError

from .dh_support import (
    BASE,
    QUERY_ID,
    ROWS_URL,
    STATEMENTS_URL,
    make_config,
    make_transport,
    open_conn,
    steady_clock,
)


def _done(request: httpx.Request, **over) -> httpx.Response:
    """A finished, result-less run under the id the request chose."""
    body = {"id": json.loads(request.content)["id"], "status": "done", "has_result": False}
    body.update(over)
    return httpx.Response(200, json=body)


@respx.mock
def test_the_client_chooses_the_id_and_the_cursor_knows_it():
    conn = open_conn()
    submit = respx.post(STATEMENTS_URL).mock(side_effect=_done)
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (n INT)")
    sent = json.loads(submit.calls[0].request.content)["id"]
    assert uuid.UUID(sent)
    assert cur.query_id == sent
    cur.execute("CREATE TABLE u (n INT)")
    assert json.loads(submit.calls[1].request.content)["id"] != sent


@respx.mock
def test_a_caller_can_name_the_run():
    conn = open_conn()
    chosen = str(uuid.uuid4())
    submit = respx.post(STATEMENTS_URL).mock(side_effect=_done)
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (n INT)", query_id=chosen)
    assert json.loads(submit.calls[0].request.content)["id"] == chosen
    assert cur.query_id == chosen


@respx.mock
def test_an_older_server_picks_its_own_id_and_the_cursor_takes_it():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            200, json={"id": QUERY_ID, "status": "done", "has_result": False}
        )
    )
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (n INT)")
    assert cur.query_id == QUERY_ID


@respx.mock
def test_an_id_already_used_is_an_integrity_error():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(
            409, json={"error": "query_id_conflict", "message": "A query with id x exists."}
        )
    )
    with pytest.raises(IntegrityError) as info:
        conn.cursor().execute("SELECT 1", query_id=str(uuid.uuid4()))
    assert info.value.code == "query_id_conflict"


@respx.mock
def test_a_cancel_from_another_thread_reaches_a_run_whose_submit_is_held():
    """The point of choosing the id: the run can be stopped before the submit returns.
    The first DELETE arrives before the server has recorded the run and is retried."""
    conn = open_conn()
    cur = conn.cursor()
    deletes = []

    def delete(request):
        deletes.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(404 if len(deletes) == 1 else 204)

    respx.delete(url__regex=rf"{BASE}/queries/.+").mock(side_effect=delete)

    def held_submit(request):
        canceller = threading.Thread(target=cur.cancel)
        canceller.start()
        canceller.join(timeout=5)
        return _done(request, status="cancelled")

    respx.post(STATEMENTS_URL).mock(side_effect=held_submit)
    with pytest.raises(OperationalError, match="cancelled") as info:
        cur.execute("SELECT * FROM big")
    assert info.value.code == "cancelled"
    assert deletes == [cur.query_id, cur.query_id]


@respx.mock
def test_a_cancel_sent_to_an_id_an_older_server_ignored_follows_the_real_one():
    config = make_config()
    respx.post(f"{BASE}/workspaces/analytics/sql/sessions").mock(
        return_value=httpx.Response(201, json={"id": "s", "status": "open"})
    )
    respx.get(f"{BASE}/version").mock(return_value=httpx.Response(404))
    conn = Connection.open(config, transport=make_transport(config, monotonic=steady_clock(1.0)))
    cur = conn.cursor()
    deleted = []

    def delete(request):
        deleted.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(404 if deleted[-1] != QUERY_ID else 204)

    respx.delete(url__regex=rf"{BASE}/queries/.+").mock(side_effect=delete)

    def held_submit(request):
        cur.cancel()  # still submitting: the chosen id is unknown to this server
        return httpx.Response(200, json={"id": QUERY_ID, "status": "running"})

    respx.post(f"{BASE}/sql/sessions/s/statements").mock(side_effect=held_submit)
    respx.get(f"{BASE}/queries/{QUERY_ID}").mock(
        return_value=httpx.Response(200, json={"id": QUERY_ID, "status": "cancelled"})
    )
    with pytest.raises(OperationalError, match="cancelled"):
        cur.execute("SELECT * FROM big")
    assert deleted[-1] == QUERY_ID
    assert len(deleted) >= 2


@respx.mock
def test_cancelling_after_the_run_finished_sends_one_request():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(side_effect=_done)
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (n INT)")
    gone = respx.delete(url__regex=rf"{BASE}/queries/.+").mock(return_value=httpx.Response(404))
    cur.cancel()
    assert gone.call_count == 1


def test_cancel_before_any_statement_does_nothing():
    with respx.mock:
        conn = open_conn()
        conn.cursor().cancel()


@respx.mock
def test_a_run_whose_agent_went_away_is_operational():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        side_effect=lambda r: _done(r, status="failed", error="agent disconnected")
    )
    with pytest.raises(OperationalError, match="agent disconnected"):
        conn.cursor().execute("SELECT 1")


@respx.mock
def test_a_failing_statement_is_still_a_programming_error():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        side_effect=lambda r: _done(r, status="failed", error="Catalog Error: no table t")
    )
    with pytest.raises(ProgrammingError, match="no table t"):
        conn.cursor().execute("SELECT * FROM t")


@respx.mock
def test_tags_from_the_connection_and_the_statement_are_merged():
    conn = open_conn(make_config(tags={"app": "notebook", "team": "a"}))
    submit = respx.post(STATEMENTS_URL).mock(side_effect=_done)
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (n INT)", tags={"team": "b", "cell": "3"})
    cur.execute("CREATE TABLE u (n INT)")
    assert json.loads(submit.calls[0].request.content)["tags"] == {
        "app": "notebook",
        "team": "b",
        "cell": "3",
    }
    assert json.loads(submit.calls[1].request.content)["tags"] == {"app": "notebook", "team": "a"}


@respx.mock
def test_no_tags_sends_no_field():
    conn = open_conn()
    submit = respx.post(STATEMENTS_URL).mock(side_effect=_done)
    conn.cursor().execute("CREATE TABLE t (n INT)")
    assert "tags" not in json.loads(submit.calls[0].request.content)


@respx.mock
def test_connect_takes_tags():
    respx.get(f"{BASE}/version").mock(return_value=httpx.Response(404))
    conn = connect("https://dh.test", "analytics", "dh_pat_x", mode="auto", tags={"app": "x"})
    submit = respx.post(f"{BASE}/workspaces/analytics/queries").mock(side_effect=_done)
    respx.get(ROWS_URL)
    conn.cursor().execute("CREATE TABLE t (n INT)")
    assert json.loads(submit.calls[0].request.content)["tags"] == {"app": "x"}
