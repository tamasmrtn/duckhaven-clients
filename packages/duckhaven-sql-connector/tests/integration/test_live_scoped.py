"""Live integration tests against a **scoped** catalog attachment.

Opt-in on top of the ordinary live suite: also needs ``DUCKHAVEN_TEST_SCOPED_WORKSPACE``
and ``DUCKHAVEN_TEST_SCOPED_CATALOG`` (``ci/integration/seed.py`` provisions both). The
scoped fixture lives in its own workspace on purpose — DuckHaven evaluates the enumeration
denial per workspace, so a scoped attachment beside an open catalog would disable
``information_schema`` for that one too.

These assertions cannot be made against a mock: what they pin is that the metadata methods
work where engine-side enumeration is refused, which only reproduces on a real server.
"""

import os

import pytest

from duckhaven_sql_connector import ProgrammingError, connect

pytestmark = pytest.mark.integration

HOST = os.environ.get("DUCKHAVEN_TEST_HOST")
TOKEN = os.environ.get("DUCKHAVEN_TEST_PAT")
AGENT = os.environ.get("DUCKHAVEN_TEST_AGENT") or None
WORKSPACE = os.environ.get("DUCKHAVEN_TEST_SCOPED_WORKSPACE")
CATALOG = os.environ.get("DUCKHAVEN_TEST_SCOPED_CATALOG")

if not (HOST and TOKEN and WORKSPACE and CATALOG):
    pytest.skip(
        "set DUCKHAVEN_TEST_SCOPED_WORKSPACE/SCOPED_CATALOG (plus HOST/PAT) to run the "
        "scoped-catalog live tests",
        allow_module_level=True,
    )

SCHEMA = "conn_scoped"


@pytest.fixture(scope="module")
def conn():
    connection = connect(host=HOST, workspace=WORKSPACE, token=TOKEN, agent=AGENT, catalog=CATALOG)
    try:
        cur = connection.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cur.execute(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.scoped_t")
        cur.execute(
            f"CREATE TABLE {CATALOG}.{SCHEMA}.scoped_t "
            "(id BIGINT, amt DECIMAL(18,4), ts TIMESTAMP WITH TIME ZONE, txt VARCHAR)"
        )
        yield connection
    finally:
        cur = connection.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.scoped_t")
        cur.execute(f"DROP SCHEMA IF EXISTS {CATALOG}.{SCHEMA}")
        connection.close()


def test_engine_side_enumeration_is_refused(conn):
    """The premise of this whole suite. If this ever stops raising, the catalog under test
    is not actually scoped and every assertion below proves nothing."""
    cur = conn.cursor()
    with pytest.raises(ProgrammingError):
        cur.execute("SELECT table_name FROM information_schema.tables")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT table_name FROM information_schema.tables",
        "SELECT schema_name FROM information_schema.schemata",
        "SELECT column_name FROM information_schema.columns",
        "SELECT table_name FROM duckdb_tables()",
        "SHOW TABLES",
        "PRAGMA show_tables",
    ],
)
def test_every_enumeration_spelling_is_refused(conn, sql):
    cur = conn.cursor()
    with pytest.raises(ProgrammingError):
        cur.execute(sql)


def test_catalogs_works(conn):
    cur = conn.cursor().catalogs()
    assert CATALOG in [row[0] for row in cur.fetchall()]


def test_schemas_works(conn):
    cur = conn.cursor().schemas(catalog=CATALOG)
    assert SCHEMA in [row[1] for row in cur.fetchall()]


def test_tables_works(conn):
    cur = conn.cursor().tables(catalog=CATALOG, schema_name=SCHEMA)
    assert "scoped_t" in [row[2] for row in cur.fetchall()]


def test_columns_reports_real_types(conn):
    """DESCRIBE is grant-checked per relation at metadata tier, so it survives here — and
    unlike information_schema.columns it reports the actual columns rather than the
    ('__', 'UNKNOWN') placeholder Iceberg tables otherwise produce."""
    cur = conn.cursor().columns(catalog=CATALOG, schema_name=SCHEMA, table_name="scoped_t")
    rows = cur.fetchall()
    assert [(r[3], r[5]) for r in rows] == [
        ("id", "BIGINT"),
        ("amt", "DECIMAL(18,4)"),
        ("ts", "TIMESTAMP WITH TIME ZONE"),
        ("txt", "VARCHAR"),
    ]
    assert [r[4] for r in rows] == [1, 2, 3, 4]


def test_describe_and_reads_still_work(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM (DESCRIBE {CATALOG}.{SCHEMA}.scoped_t)")
    assert [r[0] for r in cur.fetchall()] == ["id", "amt", "ts", "txt"]
    cur.execute(f"SELECT count(*) FROM {CATALOG}.{SCHEMA}.scoped_t")
    assert cur.fetchall() == [(0,)]
