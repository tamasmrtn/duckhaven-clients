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
import threading
import uuid

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


def test_schema_evolution_adds_several_columns_at_once(pipeline):
    """Two or more new columns on an existing table must not be batched into one ALTER.

    DuckDB takes a single ALTER action per statement, so the comma-joined form dlt emits by
    default is rejected outright ("Only one ALTER command per statement is supported").
    Only a live agent can prove the split form is what actually reaches DuckDB.
    """

    @dlt.resource(name="laps", write_disposition="append", primary_key="id")
    def laps_v1():
        yield [{"id": 1, "lap_number": 1}]

    pipeline.run(laps_v1())

    @dlt.resource(name="laps", write_disposition="append", primary_key="id")
    def laps_v2():
        yield [
            {
                "id": 2,
                "lap_number": 2,
                "segments_sector_1": "a",
                "segments_sector_2": "b",
                "segments_sector_3": "c",
            }
        ]

    pipeline.run(laps_v2())

    laps = pipeline.sql_client().make_qualified_table_name("laps")
    assert _scalar(pipeline, f"SELECT count(*) FROM {laps}") == 2
    assert _scalar(pipeline, f"SELECT segments_sector_3 FROM {laps} WHERE id = 2") == "c"


def test_concurrent_loads_into_one_table_all_land():
    """Parallel writers race on the same Iceberg table; Polaris rejects the losers with a
    409 and the retry must recover every one of them, exactly once."""
    workers = 4
    rows_each = 2
    dataset = f"dlt_duckhaven_concurrent_{uuid.uuid4().hex[:8]}"
    destination = _destination()
    barrier = threading.Barrier(workers)
    failures: list[BaseException] = []

    def load(worker: int) -> None:
        pipe = dlt.pipeline(
            pipeline_name=f"dlt_duckhaven_concurrent_{worker}",
            destination=destination,
            dataset_name=dataset,
        )

        @dlt.resource(name="events", write_disposition="append")
        def events():
            yield [{"worker": worker, "n": n} for n in range(rows_each)]

        try:
            barrier.wait()  # line the commits up so they actually collide
            pipe.run(events())
        except BaseException as exc:  # noqa: BLE001 - reported after the join
            failures.append(exc)

    threads = [threading.Thread(target=load, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"concurrent loads failed: {failures}"

    reader = dlt.pipeline(
        pipeline_name="dlt_duckhaven_concurrent_0", destination=destination, dataset_name=dataset
    )
    events = reader.sql_client().make_qualified_table_name("events")
    # Every row present once: a rejected commit publishes nothing, so the retry cannot
    # duplicate what it re-runs.
    assert _scalar(reader, f"SELECT count(*) FROM {events}") == workers * rows_each
    assert _scalar(reader, f"SELECT count(DISTINCT worker) FROM {events}") == workers
