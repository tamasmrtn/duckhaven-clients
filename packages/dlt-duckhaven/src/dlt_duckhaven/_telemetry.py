"""Optional OpenTelemetry spans for the load path.

The destination never hard-depends on OpenTelemetry. When the ``otel`` extra is installed,
each load job and staging upload emits a span; because the connector already injects a W3C
``traceparent`` on every request, those spans become the parents of the connector's HTTP
spans, so a dlt load traces end-to-end (client → API → agent). Without the extra, every
function here is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _tracer: Any = trace.get_tracer("dlt-duckhaven")
except ImportError:  # pragma: no cover - the otel extra is present in the test env
    Status = StatusCode = None  # type: ignore[assignment]
    _tracer = None


def otel_available() -> bool:
    return _tracer is not None


@contextmanager
def load_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """A span around a load operation; yields ``None`` and does nothing without OTel."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
