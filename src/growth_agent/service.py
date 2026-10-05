"""Production-style FastAPI service for evidence-based marketing claim audit.

This module is OPTIONAL — it requires the ``service`` extra so the core
install keeps its 6-dependency footprint.

Run with::

    pip install -e ".[service]"
    uvicorn growth_agent.service:app --host 0.0.0.0 --port 8080

Endpoints (v1):

* ``POST /v1/audits``       — submit a synchronous audit; returns the verdict
* ``GET  /v1/audits/{id}`` — fetch a stored audit bundle by run_id
* ``GET  /v1/healthz``      — liveness probe
* ``GET  /v1/readyz``       — readiness probe
* ``GET  /v1/metrics``      — Prometheus exposition (requires ``observability`` extra)
* ``GET  /v1/openapi.json`` — generated OpenAPI schema

Design choices:

* ``asyncio.Semaphore`` enforces a global concurrency ceiling so a single
  worker cannot run more than ``GROWTH_SERVICE_CONCURRENCY`` audits at once.
* ``X-Trace-Id`` is propagated (or generated) per request and returned in
  the response header so a downstream caller can correlate logs.
* ``/v1/audits`` returns the verdict inline so business callers do not have
  to poll. Asynchronous dispatch with polling is a future addition; the
  current 1.8s-per-request latency makes that premature.
* When the ``observability`` extra is installed, every request is wrapped
  in an OpenTelemetry ``audit_request`` span and latency/verdict are
  exposed at ``/v1/metrics`` for Prometheus scraping. When the extra is
  absent, both observability surfaces degrade to no-ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "FastAPI is required for the service layer. "
        "Install with: pip install -e '.[service]'"
    ) from exc

from pydantic import BaseModel, Field

from . import metrics, telemetry
from .audit import AuditBundle, AuditRequest, run_audit
from .resources import (
    DEFAULT_AUDIT_KNOWLEDGE,
    DEFAULT_AUDIT_RULES,
    DEFAULT_OUTPUT_ROOT,
)


logger = logging.getLogger("growth_agent.service")


# ---------- Configuration (env-tunable) ----------
MAX_CONCURRENT = int(os.getenv("GROWTH_SERVICE_CONCURRENCY", "8"))
MAX_BODY_BYTES = int(os.getenv("GROWTH_SERVICE_MAX_BODY", "32768"))


# ---------- Concurrency limiter ----------
_audit_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# ---------- Pydantic contracts ----------
class AuditSubmitRequest(BaseModel):
    """Inbound payload for ``POST /v1/audits``."""

    request: AuditRequest
    mode: Literal["qwen", "api"] = "qwen"
    strategy: Literal["agent", "rag"] = "agent"


class AuditSubmitResponse(BaseModel):
    """Verdict returned by ``POST /v1/audits``."""

    run_id: str
    status: str
    strategy: str
    duration_ms: int


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "claim-studio"
    version: str = "0.4.0"
    concurrency_limit: int


class ErrorResponse(BaseModel):
    error: str
    message: str
    trace_id: str | None = None


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("service starting: concurrency_limit=%d", MAX_CONCURRENT)
    telemetry.configure_tracer()
    try:
        yield
    finally:
        logger.info("service shutting down")


# ---------- App ----------
app = FastAPI(
    title="claim-studio audit service",
    version="0.4.0",
    lifespan=lifespan,
    description=(
        "Production-style audit API for evidence-based marketing claim review. "
        "Submits an existing marketing copy and returns a verdict, evidence citations, "
        "and a SHA256-bound audit bundle."
    ),
    responses={
        413: {"model": ErrorResponse, "description": "Request body too large"},
        429: {"model": ErrorResponse, "description": "Concurrency limit reached"},
        500: {"model": ErrorResponse, "description": "Audit execution failed"},
    },
)


# ---------- Middleware ----------
class TraceIdMiddleware:
    """Generate or propagate ``X-Trace-Id`` and ``X-Duration-Ms`` per request.

    Implemented as a pure ASGI middleware (not ``@app.middleware``) so the
    headers are guaranteed to land on the outgoing ``http.response.start``
    message — the BaseHTTPMiddleware quirk swallows custom headers on the
    response object when the inner app streams a body.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = {name.lower(): value for name, value in scope.get("headers", [])}
        trace_id = incoming.get(b"x-trace-id", b"").decode() or str(uuid.uuid4())
        start = time.perf_counter()

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                duration_ms = int((time.perf_counter() - start) * 1000)
                headers_list = list(message.get("headers", []))
                headers_list.extend(
                    [
                        (b"x-trace-id", trace_id.encode("ascii")),
                        (b"x-duration-ms", str(duration_ms).encode("ascii")),
                    ]
                )
                message["headers"] = headers_list
            await send(message)

        await self.app(scope, receive, send_with_trace)


class BodySizeLimitMiddleware:
    """Reject payloads over ``MAX_BODY_BYTES`` before they hit the parser."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(value.decode()) > MAX_BODY_BYTES:
                        response = JSONResponse(
                            {
                                "error": "payload_too_large",
                                "message": f"Request body exceeds {MAX_BODY_BYTES} bytes.",
                            },
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    response = JSONResponse(
                        {"error": "invalid_content_length", "message": "Content-Length must be an integer."},
                        status_code=400,
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(TraceIdMiddleware)


# ---------- Endpoints ----------
@app.get(
    "/v1/healthz",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 if the process is up. Does not verify downstream dependencies.",
)
async def healthz() -> HealthResponse:
    return HealthResponse(concurrency_limit=MAX_CONCURRENT)


@app.get(
    "/v1/readyz",
    response_model=HealthResponse,
    summary="Readiness probe",
    description="Returns 200 if the service can accept audits (knowledge base is readable).",
)
async def readyz() -> HealthResponse:
    knowledge = Path(os.getenv("GROWTH_AUDIT_KNOWLEDGE", str(DEFAULT_AUDIT_KNOWLEDGE)))
    if not knowledge.is_file():
        raise HTTPException(
            status_code=503,
            detail={"error": "knowledge_unavailable", "message": f"Missing knowledge file: {knowledge}"},
        )
    return HealthResponse(concurrency_limit=MAX_CONCURRENT)


@app.post(
    "/v1/audits",
    response_model=AuditSubmitResponse,
    status_code=200,
    summary="Submit a synchronous audit",
    description=(
        "Submit an existing marketing copy. Returns a verdict (`pending_review`, "
        "`insufficient_evidence`, `needs_revision`, or `model_error`), the run id, "
        "and the per-request duration. Full evidence bundle is stored on disk and "
        "retrievable via ``GET /v1/audits/{run_id}``."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        429: {"model": ErrorResponse, "description": "Concurrency limit reached"},
    },
)
async def submit_audit(payload: AuditSubmitRequest, request: Request) -> AuditSubmitResponse:
    trace_id = request.headers.get("x-trace-id") or "unknown"
    if _audit_semaphore.locked() and _audit_semaphore._value <= 0:  # noqa: SLF001 - internal check
        return JSONResponse(
            {
                "error": "concurrency_limit_reached",
                "message": f"Service is at capacity ({MAX_CONCURRENT} concurrent audits).",
                "trace_id": trace_id,
            },
            status_code=429,
        )

    with telemetry.audit_span(trace_id=trace_id) as root_span:
        if root_span is not None:
            root_span.set_attribute("product_id", payload.request.product_id)
            root_span.set_attribute("strategy", payload.strategy)
            root_span.set_attribute("mode", payload.mode)

        async with _audit_semaphore:
            knowledge = Path(os.getenv("GROWTH_AUDIT_KNOWLEDGE", str(DEFAULT_AUDIT_KNOWLEDGE)))
            rules = Path(os.getenv("GROWTH_AUDIT_RULES", str(DEFAULT_AUDIT_RULES)))
            output_root = Path(os.getenv("GROWTH_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT)))
            try:
                with metrics.track_audit(strategy=payload.strategy):
                    bundle, _ = await run_audit(
                        payload.request,
                        mode=payload.mode,
                        strategy=payload.strategy,
                        model_transport="async",
                        knowledge_path=knowledge,
                        rules_path=rules,
                        output_root=output_root,
                    )
            except ValueError as exc:
                metrics.record_verdict(strategy=payload.strategy, verdict="invalid_request")
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_request", "message": str(exc)},
                )
            except Exception as exc:  # pragma: no cover - last-resort guard
                logger.exception("audit failed: trace_id=%s", trace_id)
                metrics.record_verdict(strategy=payload.strategy, verdict="model_error")
                raise HTTPException(
                    status_code=500,
                    detail={"error": "audit_failed", "message": str(exc)},
                )

        metrics.record_verdict(strategy=payload.strategy, verdict=bundle.status)
        if root_span is not None:
            root_span.set_attribute("verdict", bundle.status)

    return AuditSubmitResponse(
        run_id=bundle.run_id,
        status=bundle.status,
        strategy=bundle.strategy,
        duration_ms=bundle.duration_ms,
    )


@app.get(
    "/v1/audits/{run_id}",
    response_model=AuditBundle,
    summary="Retrieve a stored audit bundle",
)
async def get_audit(run_id: str) -> AuditBundle:
    if not run_id or "/" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail={"error": "invalid_run_id"})
    output_root = Path(os.getenv("GROWTH_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT)))
    bundle_file = (output_root / run_id / "audit_bundle.json").resolve()
    if not str(bundle_file).startswith(str(output_root.resolve())):
        raise HTTPException(status_code=400, detail={"error": "path_traversal"})
    if not bundle_file.is_file():
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return AuditBundle.model_validate(json.loads(bundle_file.read_text(encoding="utf-8")))


@app.get(
    "/v1/metrics",
    summary="Prometheus exposition",
    description=(
        "Returns Prometheus text-format metrics for scraping. "
        "Requires the ``observability`` extra; otherwise the body is empty."
    )
)
async def metrics_endpoint() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


# ---------- Entrypoint ----------
def main() -> None:
    """Run with ``python -m growth_agent.service`` for local smoke tests."""
    import uvicorn

    uvicorn.run(
        "growth_agent.service:app",
        host=os.getenv("GROWTH_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("GROWTH_SERVICE_PORT", "8080")),
        reload=bool(int(os.getenv("GROWTH_SERVICE_RELOAD", "0"))),
    )


if __name__ == "__main__":  # pragma: no cover
    main()