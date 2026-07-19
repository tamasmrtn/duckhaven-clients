"""End-to-end: a real dlt pipeline against a live DuckHaven.

Proves the whole loop — staged Parquet → COPY through the session API → Iceberg table —
by running a pipeline and reading the rows back: an ``append`` load, then a schema-evolving
append (a new column), then a ``merge`` that must update in place (idempotent, no
duplicates).

Gated on ``DUCKHAVEN_TEST_HOST``/``WORKSPACE``/``PAT``/``CATALOG`` and run via
``make test-dlt-integration``. Also requires the server-side staging-credential vend
endpoint (``POST …/sql/sessions/{id}/staging-credentials``); until it ships, the staged
load cannot complete.
"""

import os

import dlt
import pytest
from dlt_duckhaven import duckhaven

pytestmark = pytest.mark.integration


def _destination():
    host = os.environ.get("DUCKHAVEN_TEST_HOST")
    workspace = os.environ.get("DUCKHAVEN_TEST_WORKSPACE")
    token = os.environ.get("DUCKHAVEN_TEST_PAT")
    catalog = os.environ.get("DUCKHAVEN_TEST_CATALOG")
    if not all((host, workspace, token, catalog)):
        pytest.skip("set DUCKHAVEN_TEST_HOST/WORKSPACE/PAT/CATALOG to run the live e2e")
    return duckhaven(
        host=host,
        workspace=workspace,
        catalog=catalog,
        credentials=token,
        agent=os.environ.get("DUCKHAVEN_TEST_AGENT") or None,
    )


@pytest.fixture
def pipeline():
    return dlt.pipeline(
        pipeline_name="dlt_duckhaven_e2e",
        destination=_destination(),
        dataset_name="dlt_duckhaven_e2e",
        dev_mode=True,
    )


def _scalar(pipeline, sql):
    with pipeline.sql_client() as client:
        return client.execute_sql(sql)[0][0]


def test_append_then_schema_evolution(pipeline):
    @dlt.resource(name="people", write_disposition="append", primary_key="id")
    def people_v1():
        yield [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

    pipeline.run(people_v1())
    people = pipeline.sql_client().make_qualified_table_name("people")
    assert _scalar(pipeline, f"SELECT count(*) FROM {people}") == 2

    # Second run adds a column: the destination must ALTER the Iceberg table and load.
    @dlt.resource(name="people", write_disposition="append", primary_key="id")
    def people_v2():
        yield [{"id": 3, "name": "carol", "age": 30}]

    pipeline.run(people_v2())
    assert _scalar(pipeline, f"SELECT count(*) FROM {people}") == 3
    assert _scalar(pipeline, f"SELECT age FROM {people} WHERE id = 3") == 30


def test_merge_is_idempotent(pipeline):
    @dlt.resource(name="accounts", write_disposition="merge", primary_key="id")
    def accounts(name):
        yield [{"id": 1, "name": name}]

    pipeline.run(accounts("alice"))
    pipeline.run(accounts("alice-updated"))

    table = pipeline.sql_client().make_qualified_table_name("accounts")
    assert _scalar(pipeline, f"SELECT count(*) FROM {table}") == 1
    assert _scalar(pipeline, f"SELECT name FROM {table} WHERE id = 1") == "alice-updated"
