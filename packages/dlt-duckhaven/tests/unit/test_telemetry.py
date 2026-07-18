"""Optional OTel spans: the load job and staging upload emit spans when a tracer is
configured, and everything is a no-op when OpenTelemetry is unavailable."""

from unittest.mock import MagicMock

import dlt_duckhaven._telemetry as telemetry
import dlt_duckhaven.load_jobs as load_jobs
import pytest
from dlt_duckhaven.load_jobs import DuckHavenCopyJob
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    return exporter


def test_load_span_records_attributes(spans):
    with telemetry.load_span("dlt_duckhaven.load_job", {"dlt.table": "orders"}):
        pass
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["dlt_duckhaven.load_job"]
    assert finished[0].attributes["dlt.table"] == "orders"


def test_load_span_records_exception(spans):
    with pytest.raises(ValueError):  # noqa: PT011
        with telemetry.load_span("dlt_duckhaven.load_job"):
            raise ValueError("boom")
    span = spans.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"


def test_copy_job_emits_span(spans, monkeypatch):
    sql_client = MagicMock()
    sql_client.make_qualified_table_name.return_value = '"raw"."analytics"."orders"'
    sql_client.native_connection = MagicMock()
    monkeypatch.setattr(
        load_jobs._staging,
        "stage_file",
        MagicMock(return_value="s3://bucket/_staging/load1/orders.abc.0.parquet"),
    )
    job = DuckHavenCopyJob("/tmp/orders.abc.0.parquet")
    job._job_client = MagicMock(sql_client=sql_client)
    job._load_table = {"name": "orders"}
    job._load_id = "load1"

    job.run()

    names = [s.name for s in spans.get_finished_spans()]
    assert "dlt_duckhaven.load_job" in names


def test_load_span_is_noop_without_otel(monkeypatch):
    # No tracer configured (extra absent): the span is a harmless no-op yielding None.
    monkeypatch.setattr(telemetry, "_tracer", None)
    with telemetry.load_span("dlt_duckhaven.load_job", {"dlt.table": "orders"}) as span:
        assert span is None
    assert telemetry.otel_available() is False
