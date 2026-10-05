"""Prometheus metrics for the audit service.

OPTIONAL — requires the ``observability`` extra. When the package is
missing, every helper degrades to a no-op so the rest of the system stays
runnable.

Exposed metrics
---------------
``audit_total{strategy,verdict}``
    Counter of completed audits, labelled by pipeline strategy and final
    verdict. Slice this to compute ``reject_rate`` per verdict.

``audit_latency_seconds{strategy}``
    Histogram of end-to-end audit latency in seconds. Buckets cover 50ms
    to 30s — both mock fast-path and full Qwen path fit in the same view.

``audit_in_flight``
    Gauge of concurrent audits being processed right now.

``gate_failure_total{gate,failure_kind}``
    Counter of approval-gate failures. ``gate`` is the gate name
    (``forbidden_words``, ``cross_product_leak``, ``prompt_injection``,
    ``version_binding``, ``schema_violation``); ``failure_kind`` is the
    specific reason inside that gate.

Endpoint
--------
``GET /v1/metrics`` returns the Prometheus text exposition format.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator


# ---------- Lazy import ----------
try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )

    _HAS_PROM = True
except ImportError:  # pragma: no cover - optional dependency
    _HAS_PROM = False


# ---------- Latency buckets ----------
# Mock fast path ~50 ms; real Qwen ~1.8 s. Bucket boundaries cover both.
_LATENCY_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0,
)


# ---------- Registry ----------
if _HAS_PROM:
    REGISTRY = CollectorRegistry(auto_describe=True)

    AUDIT_TOTAL = Counter(
        "audit_total",
        "Number of completed audits, labelled by pipeline strategy and verdict.",
        labelnames=("strategy", "verdict"),
        registry=REGISTRY,
    )

    AUDIT_LATENCY = Histogram(
        "audit_latency_seconds",
        "End-to-end audit latency in seconds.",
        labelnames=("strategy",),
        buckets=_LATENCY_BUCKETS,
        registry=REGISTRY,
    )

    AUDIT_IN_FLIGHT = Gauge(
        "audit_in_flight",
        "Number of audits currently being processed.",
        registry=REGISTRY,
    )

    GATE_FAILURE = Counter(
        "gate_failure_total",
        "Number of approval-gate failures, labelled by gate and failure kind.",
        labelnames=("gate", "failure_kind"),
        registry=REGISTRY,
    )
else:  # pragma: no cover - no-op fallback
    REGISTRY = None
    AUDIT_TOTAL = None  # type: ignore[assignment]
    AUDIT_LATENCY = None  # type: ignore[assignment]
    AUDIT_IN_FLIGHT = None  # type: ignore[assignment]
    GATE_FAILURE = None  # type: ignore[assignment]


# ---------- Convenience helpers ----------
@contextmanager
def track_audit(strategy: str) -> Iterator[Any]:
    """Time the audit and record latency / in-flight."""
    if not _HAS_PROM:
        yield
        return
    AUDIT_IN_FLIGHT.inc()
    started = time.perf_counter()
    try:
        yield
    finally:
        AUDIT_LATENCY.labels(strategy=strategy).observe(time.perf_counter() - started)
        AUDIT_IN_FLIGHT.dec()


def record_verdict(strategy: str, verdict: str) -> None:
    """Bump the verdict counter once an audit finishes."""
    if not _HAS_PROM:
        return
    AUDIT_TOTAL.labels(strategy=strategy, verdict=verdict).inc()


def record_gate_failure(gate: str, failure_kind: str) -> None:
    """Bump the gate-failure counter."""
    if not _HAS_PROM:
        return
    GATE_FAILURE.labels(gate=gate, failure_kind=failure_kind).inc()


def render() -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for the ``/v1/metrics`` endpoint."""
    if not _HAS_PROM:
        return b"", "text/plain; version=0.0.4"
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = [
    "REGISTRY",
    "AUDIT_TOTAL",
    "AUDIT_LATENCY",
    "AUDIT_IN_FLIGHT",
    "GATE_FAILURE",
    "track_audit",
    "record_verdict",
    "record_gate_failure",
    "render",
]