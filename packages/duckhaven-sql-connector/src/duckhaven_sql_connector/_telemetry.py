"""Optional OpenTelemetry integration and host instrumentation hooks.

The connector never hard-depends on OpenTelemetry. When the ``otel`` extra is installed
it emits a CLIENT span per request and injects a W3C ``traceparent`` header so the client
span joins the DuckHaven server trace (mirroring ``duckhaven_shared.telemetry``). Without
it, every function here is a no-op.

Separately, :class:`Hooks` lets a host observe request timings, retries, and rows fetched
without any OpenTelemetry dependency — a client library must not run its own metrics
server, so it exposes callbacks the host can wire into its own instrumentation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _propagator: Any = TraceContextTextMapPropagator()
    _tracer: Any = trace.get_tracer("duckhaven-sql-connector")
except ImportError:  # pragma: no cover - the otel extra is present in the test env
    SpanKind = Status = StatusCode = None  # type: ignore[assignment]
    _propagator = None
    _tracer = None


def otel_available() -> bool:
    return _tracer is not None


def inject_traceparent(headers: dict[str, str]) -> None:
    """Inject the active span's W3C ``traceparent`` into ``headers`` (no-op if OTel absent)."""
    if _propagator is not None:
        _propagator.inject(headers)


@contextmanager
def client_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """A CLIENT span around a request; yields ``None`` and does nothing without OTel."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@dataclass
class Hooks:
    """Optional instrumentation callbacks a host can supply (independent of OTel).

    - ``on_request(method, path, status_code, duration_s)`` after each HTTP response.
    - ``on_retry(method, path, attempt)`` before each retry of an idempotent request.
    - ``on_rows_fetched(query_id, n)`` after each page of result rows is loaded.
    """

    on_request: Callable[[str, str, int, float], None] | None = None
    on_retry: Callable[[str, str, int], None] | None = None
    on_rows_fetched: Callable[[str, int], None] | None = None
