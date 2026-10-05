# Service Layer (Production-style HTTP API)

`growth_agent.service` ships a FastAPI application that wraps the audit
engine behind a JSON HTTP API. It is layered on top of the existing
single-user web UI (`growth_agent.webapp`) without replacing it — both can
be served side by side.

The service layer adds what a real backend would expose: an OpenAPI schema,
propagated trace identifiers, a global concurrency ceiling, and payload-size
limits. It uses the same Pydantic contracts the agent already enforces
(`AuditRequest`, `AuditBundle`), so the wire format and the audit
internals stay in lock-step.

---

## 1. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/audits` | Submit an audit; returns the verdict inline |
| `GET`  | `/v1/audits/{run_id}` | Fetch a previously stored bundle |
| `GET`  | `/v1/healthz` | Liveness probe |
| `GET`  | `/v1/readyz` | Readiness probe (verifies knowledge base is reachable) |
| `GET`  | `/openapi.json` | Generated OpenAPI 3 schema |
| `GET`  | `/docs` | Swagger UI |

Full OpenAPI is generated from the same Pydantic models the audit engine
uses internally — no second copy of the schema, no drift.

---

## 2. Run it

```bash
pip install -e ".[service]"
uvicorn growth_agent.service:app --host 0.0.0.0 --port 8080
```

Or run as a module:

```bash
python -m growth_agent.service
```

### Configuration

| Variable | Default | Effect |
|---|---|---|
| `GROWTH_SERVICE_HOST` | `0.0.0.0` | Bind address |
| `GROWTH_SERVICE_PORT` | `8080` | Bind port |
| `GROWTH_SERVICE_CONCURRENCY` | `8` | Global audit concurrency ceiling |
| `GROWTH_SERVICE_MAX_BODY` | `32768` | Reject payloads larger than this (bytes) |
| `GROWTH_SERVICE_RELOAD` | `0` | `1` enables uvicorn auto-reload (dev only) |
| `GROWTH_AUDIT_KNOWLEDGE` | repo path | Path to the fact-card library |
| `GROWTH_AUDIT_RULES` | repo path | Path to the editorial rules |
| `GROWTH_OUTPUT_ROOT` | repo path | Where audit bundles are persisted |

---

## 3. Example request

```bash
curl -X POST http://localhost:8080/v1/audits \
     -H "Content-Type: application/json" \
     -d '{
       "request": {
         "id": "demo",
         "product_id": "obs-studio",
         "product_name": "OBS Studio",
         "locale": "en-US",
         "feature": "virtual-camera",
         "channel": "social_post",
         "original_copy": "OBS works with every app without setup.",
         "audience": "creators"
       },
       "strategy": "agent"
     }'
```

Response:

```json
{
  "run_id": "20261004T120259123Z-audit-demo",
  "status": "pending_review",
  "strategy": "agent",
  "duration_ms": 12
}
```

The full bundle — facts, citations, issues, trace — is persisted to
`audit_bundle.json` under `${GROWTH_OUTPUT_ROOT}/${run_id}/` and is
retrievable via `GET /v1/audits/{run_id}`.

---

## 4. Trace propagation

Every response carries two headers:

| Header | Source |
|---|---|
| `X-Trace-Id` | Echoed from the request header, or auto-generated as a UUIDv4 |
| `X-Duration-Ms` | End-to-end service-layer wall time |

Clients can supply their own `X-Trace-Id` so a single trace spans
multiple services; otherwise the service generates one for log
correlation.

---

## 5. Concurrency ceiling

`asyncio.Semaphore(GROWTH_SERVICE_CONCURRENCY)` caps how many audits run
in the worker at once. New requests past the limit receive `429` with a
machine-readable error payload.

Tuning guidance:

* `8` (default) suits a 4-core worker with a small model.
* Each concurrent audit holds one async LLM call; scale the watcher to
  `(cpu_cores × 2)` for I/O-bound models.
* Beyond ~50 concurrent calls, **add a second worker**, not more
  concurrency in a single process — the semaphore only protects against
  runaway LLM latency, not memory growth.

---

## 6. Payload size

`BodySizeLimitMiddleware` rejects bodies larger than
`GROWTH_SERVICE_MAX_BODY` (default 32 KiB) before parsing, returning
`413` with `error: "payload_too_large"`. This prevents slowloris-style
stalling where a malicious client streams bytes without ever closing.

---

## 7. Test coverage

`tests/test_service.py` covers 13 end-to-end scenarios:

* liveness and readiness probes
* happy-path audit submission
* trace-id propagation and generation
* schema validation (422) and oversized payload (413) rejection
* internal-error fallback to 500
* stored-bundle retrieval, including 404 and path-traversal rejections
* OpenAPI schema endpoint

Run with `pytest tests/test_service.py`.

---

## 8. Benchmark snapshot

The included benchmark `scripts/bench_service.py` exercises
`POST /v1/audits` at three concurrency levels with a 50 ms stand-in for
the model call. Latest run (`reports/perf-service.json`):

| Concurrency | Total | QPS | p50 ms | p95 ms | p99 ms | 2xx | 429 |
|---|---|---|---|---|---|---|---|
| 10  | 200 | 18.8 | 53.0 | 53.7 | 54.4 | 200 | 0 |
| 50  | 400 | 18.8 | 53.1 | 53.6 | 54.0 | 400 | 0 |
| 100 | 400 | 18.9 | 53.0 | 53.6 | 54.0 | 400 | 0 |

The headline P99 of ~54 ms when the simulated model is ~50 ms means the
service-layer overhead is **~4 ms** end-to-end — a number that lets a
future operator reason about horizontal scaling without guessing.

Run with: `python scripts/bench_service.py`.

---

## 9. Roadmap

| Now | Next |
|---|---|
| Synchronous `POST /v1/audits` returning the verdict inline | `POST /v1/audits/async` returning `202` + polling via `GET /v1/audits/{id}` |
| In-process concurrency cap | Queue front depth + per-IP rate limit |
| Per-request `X-Trace-Id` | OpenTelemetry trace export |
| Single-bundle retrieval | List endpoint with time-range and product filter |
| Synthetic benchmark | Live-model benchmark once a GPU is wired in |
| Health probes only | `/metrics` endpoint (Prometheus) |