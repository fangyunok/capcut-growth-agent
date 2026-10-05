"""Benchmark the production-style audit service and write ``reports/perf-service.json``.

Runs the FastAPI app in-process via httpx's ASGI transport (no real network)
and exercises ``/v1/audits`` under concurrent load. Mocked ``run_audit`` keeps
the cost flat — what we are measuring is the **service layer overhead**
(middleware, validation, semaphore, JSON serialization).

Real-model benchmarks require a live LLM and live in
``reports/perf-model-llm.json``; see ``docs/BUILD_LOG.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make src/ importable when invoked from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402


def _request_body() -> dict:
    return {
        "id": "bench",
        "product_id": "obs-studio",
        "product_name": "OBS Studio",
        "locale": "en-US",
        "feature": "virtual-camera",
        "channel": "social_post",
        "original_copy": "OBS works with every app without setup.",
        "audience": "creators",
    }


def _fake_bundle_factory(bench_delay: float):
    from growth_agent.audit import AuditBundle, AuditProposal

    def runner(req, **kw):
        time.sleep(bench_delay)  # simulate model call
        bundle = AuditBundle(
            run_id="bench-" + str(time.time_ns()),
            request=req,
            status="pending_review",
            model="scripted-bench",
            strategy="agent",
            proposal=AuditProposal.model_validate({
                "revised_copy": "ok",
                "issues": [],
                "citations": [{"quote": "ok", "fact_id": "f1"}],
            }),
            duration_ms=int(bench_delay * 1000),
        )
        return bundle, Path("/tmp/bench")

    return runner


async def _bench_once(
    concurrency: int, total: int, bench_delay_ms: int
) -> dict:
    from growth_agent.audit import AuditBundle  # noqa: F401  (registers types)
    from growth_agent.service import app as fastapi_app

    delay = bench_delay_ms / 1000.0
    factory = _fake_bundle_factory(delay)
    mock_run = AsyncMock(side_effect=factory)

    transport = ASGITransport(app=fastapi_app)
    sem = asyncio.Semaphore(concurrency)

    latencies: list[float] = []
    statuses: list[int] = []
    started_at = time.perf_counter()

    with patch("growth_agent.service.run_audit", mock_run):
        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def one():
                async with sem:
                    t0 = time.perf_counter()
                    r = await client.post(
                        "/v1/audits",
                        json={"request": _request_body(), "strategy": "agent"},
                    )
                    return time.perf_counter() - t0, r.status_code

            tasks = [asyncio.create_task(one()) for _ in range(total)]
            for coro in asyncio.as_completed(tasks):
                latency, status = await coro
                latencies.append(latency)
                statuses.append(status)

    wall = time.perf_counter() - started_at
    latencies.sort()
    p = lambda q: latencies[int(len(latencies) * q) - 1] * 1000
    return {
        "concurrency": concurrency,
        "total": total,
        "wall_seconds": round(wall, 3),
        "qps": round(total / wall, 2),
        "latency_p50_ms": round(p(0.50), 2),
        "latency_p95_ms": round(p(0.95), 2),
        "latency_p99_ms": round(p(0.99), 2),
        "latency_max_ms": round(latencies[-1] * 1000, 2),
        "status_2xx": sum(1 for s in statuses if 200 <= s < 300),
        "status_4xx": sum(1 for s in statuses if 400 <= s < 500),
        "status_5xx": sum(1 for s in statuses if 500 <= s < 600),
        "status_429": sum(1 for s in statuses if s == 429),
    }


async def main() -> None:
    # Set up temp env so service.py does not blow up
    tmp = Path(tempfile.mkdtemp(prefix="claim-studio-bench-"))
    (tmp / "runs").mkdir(parents=True, exist_ok=True)
    for name in ("knowledge.json", "rules.json"):
        path = tmp / name
        path.write_text("{}", encoding="utf-8")
    os.environ["GROWTH_AUDIT_KNOWLEDGE"] = str(tmp / "knowledge.json")
    os.environ["GROWTH_AUDIT_RULES"] = str(tmp / "rules.json")
    os.environ["GROWTH_OUTPUT_ROOT"] = str(tmp / "runs")

    # Three runs at three concurrency levels — covers both ends of the curve
    runs = [
        {"concurrency": 10, "total": 200, "bench_delay_ms": 50},
        {"concurrency": 50, "total": 400, "bench_delay_ms": 50},
        {"concurrency": 100, "total": 400, "bench_delay_ms": 50},
    ]

    print("=" * 70)
    print(f"{'concurrency':>12} {'total':>6} {'QPS':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'2xx':>5} {'429':>5}")
    print("=" * 70)

    results = []
    for cfg in runs:
        result = await _bench_once(**cfg)
        results.append(result)
        print(
            f"{result['concurrency']:>12} {result['total']:>6} "
            f"{result['qps']:>8.1f} {result['latency_p50_ms']:>8.1f} "
            f"{result['latency_p95_ms']:>8.1f} {result['latency_p99_ms']:>8.1f} "
            f"{result['status_2xx']:>5} {result['status_429']:>5}"
        )

    out_dir = Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "perf-service.json"
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service": "growth_agent.service",
        "endpoint": "POST /v1/audits",
        "model": "scripted (no real LLM)",
        "concurrency_limit": int(os.getenv("GROWTH_SERVICE_CONCURRENCY", "8")),
        "bench_delay_ms_per_call": 50,
        "runs": results,
        "notes": [
            "Bench delay of 50ms stands in for a real LLM call; the service-layer overhead is what is measured here.",
            "Live-model benchmarks live in reports/perf-model-llm.json (see docs/BUILD_LOG.md).",
            "Status 429 indicates the global asyncio.Semaphore (default 8) refused; tune GROWTH_SERVICE_CONCURRENCY upward.",
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())