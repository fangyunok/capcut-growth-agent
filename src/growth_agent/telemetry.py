"""OpenTelemetry trace integration for the audit service.

This module is OPTIONAL — it requires the ``observability`` extra so the core
install keeps its six-dependency footprint.

Install with::

    pip install -e ".[observability]"

What it does
------------
* Wraps every ``/v1/audits`` request in a root span (``audit_request``).
* Emits three named child spans that map to the pipeline's biggest stages:
  - ``retrieval`` — knowledge-base lookup (caller wraps this around the
    ``Retriever.search_many`` call).
  - ``decision`` — model call (caller wraps around ``model.invoke``).
  - ``approval_gate`` — version-binding / cross-product-leak / forbidden-word
    guards (caller wraps around the gates' final check).
* Each span carries structured attributes (``product_id``, ``strategy``,
  ``verdict``, ``n_candidates`` etc.) so production traces can be filtered
  by them in Jaeger / Tempo / Honeycomb.
* If the OpenTelemetry packages aren't present, all helpers degrade to
  no-ops via :class:`NoopSpan`, so the rest of the system stays runnable.

What it does NOT do
-------------------
* It does not configure an exporter. Operators pick an exporter (OTLP,
  Jaeger, console) in their deployment — see ``docs/OBSERVABILITY.md``.
  Here we only set the resource attributes and provide a tracer handle.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


# ---------- Lazy OTEL import ----------
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _HAS_OTEL = True
except ImportError:  # pragma: no cover - optional dependency
    _HAS_OTEL = False


# ---------- Configuration ----------
SERVICE_NAME = "claim-studio"
SERVICE_VERSION = "0.4.0"


# ---------- Provider setup ----------
def configure_tracer(
    *,
    service_name: str | None = None,
    service_version: str | None = None,
    exporter: Any | None = None,
) -> bool:
    """Initialise a global tracer provider.

    Returns ``True`` when OTEL is available, ``False`` otherwise.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    if not _HAS_OTEL:
        return False

    if trace.get_tracer_provider().__class__.__name__ != "ProxyTracerProvider":
        return True

    resource = Resource.create(
        {
            "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME),
            "service.version": service_version or os.getenv("OTEL_SERVICE_VERSION", SERVICE_VERSION),
        }
    )
    provider = TracerProvider(resource=resource)

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    elif os.getenv("OTEL_CONSOLE_EXPORTER") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    return True


def get_tracer(name: str = "growth_agent.telemetry"):
    """Return the current tracer. Falls back to a NoopTracer when OTEL is absent."""
    if not _HAS_OTEL:
        return _NoopTracer()
    return trace.get_tracer(name)


# ---------- No-op fallbacks ----------
class _NoopSpan:
    """Drop-in stand-in when opentelemetry isn't installed."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D401
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        pass


class _NoopTracer:
    def start_as_current_span(self, name: str, **_kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


# ---------- Convenience helpers ----------
@contextmanager
def audit_span(trace_id: str | None = None) -> Iterator[Any]:
    """Open a top-level ``audit_request`` span and bind a structured trace id."""
    tracer = get_tracer()
    with tracer.start_as_current_span("audit_request") as span:
        if trace_id is not None:
            span.set_attribute("trace_id", trace_id)
        yield span


@contextmanager
def retrieval_span(query_count: int) -> Iterator[Any]:
    """Span around the knowledge-base lookup phase."""
    tracer = get_tracer()
    with tracer.start_as_current_span("retrieval") as span:
        span.set_attribute("query_count", query_count)
        yield span


@contextmanager
def decision_span(model: str, strategy: str) -> Iterator[Any]:
    """Span around the model invocation that produces the proposal."""
    tracer = get_tracer()
    with tracer.start_as_current_span("decision") as span:
        span.set_attribute("model", model)
        span.set_attribute("strategy", strategy)
        yield span


@contextmanager
def approval_gate_span(gate_name: str) -> Iterator[Any]:
    """Span around a single approval-gate check (one of: version_binding,
    cross_product_leak, forbidden_words, prompt_injection, structural)."""
    tracer = get_tracer()
    with tracer.start_as_current_span("approval_gate") as span:
        span.set_attribute("gate", gate_name)
        yield span


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "configure_tracer",
    "get_tracer",
    "audit_span",
    "retrieval_span",
    "decision_span",
    "approval_gate_span",
]