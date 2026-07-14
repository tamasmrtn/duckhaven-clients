import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import duckhaven_sql_connector._telemetry as tel
from duckhaven_sql_connector.client import Transport

from .dh_support import make_config

BASE = "https://dh.test/api"


@pytest.fixture(scope="module")
def exporter():
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    # Re-fetch so the module-level tracer resolves to the provider we just set.
    tel._tracer = trace.get_tracer("duckhaven-sql-connector")
    return exp


@respx.mock
def test_request_emits_client_span_and_injects_traceparent(exporter):
    exporter.clear()
    route = respx.get(f"{BASE}/probe").mock(return_value=httpx.Response(200, json={}))
    Transport(make_config(), sleep=lambda _: None).get("/probe")

    spans = exporter.get_finished_spans()
    assert any(s.name == "duckhaven.http" for s in spans)
    assert "traceparent" in route.calls.last.request.headers


def test_client_span_records_exception_status(exporter):
    exporter.clear()
    with pytest.raises(ValueError):
        with tel.client_span("boom"):
            raise ValueError("kaboom")
    span = exporter.get_finished_spans()[-1]
    assert span.status.status_code is trace.StatusCode.ERROR


def test_noop_when_otel_absent(monkeypatch):
    monkeypatch.setattr(tel, "_tracer", None)
    monkeypatch.setattr(tel, "_propagator", None)
    headers: dict[str, str] = {}
    tel.inject_traceparent(headers)
    assert headers == {}
    with tel.client_span("x") as span:
        assert span is None
    assert tel.otel_available() is False
