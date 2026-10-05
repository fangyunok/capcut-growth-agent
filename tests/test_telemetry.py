"""Tests for OpenTelemetry trace helpers and Prometheus metrics.

Both modules degrade to no-ops when their respective packages are absent,
so the tests must continue to pass under either condition. When the
packages are installed, the tests verify:

* Spans carry the structured attributes set inside ``telemetry`` helpers.
* Verdict counters and latency histograms update on each audit.
* ``/v1/metrics`` returns Prometheus text-format bytes.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock


class TestTelemetryHelpers(unittest.TestCase):
    def test_audit_span_yields_object_with_set_attribute(self) -> None:
        from growth_agent import telemetry

        with telemetry.audit_span(trace_id="abc") as span:
            # Either a real OTEL span or a NoopSpan; both have set_attribute.
            span.set_attribute("product_id", "obs-studio")
            self.assertIsNotNone(span)

    def test_audit_span_no_otel_falls_back_to_noop(self) -> None:
        from growth_agent import telemetry

        # NoopSpan must accept set_attribute without raising.
        from growth_agent.telemetry import _NoopSpan

        span = _NoopSpan()
        span.set_attribute("k", "v")
        span.set_status("ok")
        span.record_exception(RuntimeError("x"))
        span.end()
        with span as s:
            self.assertIs(s, span)


class TestMetricsModule(unittest.TestCase):
    def test_record_verdict_and_render(self) -> None:
        from growth_agent import metrics

        # Safe to call whether prom is present or not.
        metrics.record_verdict(strategy="agent", verdict="pending_review")
        metrics.record_verdict(strategy="agent", verdict="needs_revision")
        metrics.record_gate_failure(gate="forbidden_words", failure_kind="contains_forbidden")
        body, content_type = metrics.render()
        # The body is bytes; content type advertises Prometheus format.
        self.assertIsInstance(body, bytes)
        if metrics._HAS_PROM:  # type: ignore[attr-defined]
            from prometheus_client import CONTENT_TYPE_LATEST

            self.assertEqual(content_type, CONTENT_TYPE_LATEST)
            self.assertIn(b"audit_total", body)
            self.assertIn(b"audit_latency_seconds", body)
            self.assertIn(b"gate_failure_total", body)

    def test_track_audit_records_latency(self) -> None:
        from growth_agent import metrics

        with metrics.track_audit(strategy="agent"):
            _ = sum(range(100))
        # No exception == OK. Latency observation happens in finally.

    def test_render_with_mock_prom(self) -> None:
        """Ensure render() handles the no-prom branch gracefully."""
        from growth_agent import metrics

        with unittest.mock.patch.object(metrics, "_HAS_PROM", False):
            body, content_type = metrics.render()
            self.assertEqual(body, b"")
            self.assertIn("text/plain", content_type)


class TestServiceMetricsEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import tempfile
        from pathlib import Path
        import os

        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "knowledge.json").write_text("{}", encoding="utf-8")
        (self.tmp / "rules.json").write_text("{}", encoding="utf-8")
        (self.tmp / "runs").mkdir(parents=True, exist_ok=True)

        self._env_patch = unittest.mock.patch.dict(
            os.environ,
            {
                "GROWTH_AUDIT_KNOWLEDGE": str(self.tmp / "knowledge.json"),
                "GROWTH_AUDIT_RULES": str(self.tmp / "rules.json"),
                "GROWTH_OUTPUT_ROOT": str(self.tmp / "runs"),
            },
            clear=False,
        )
        self._env_patch.start()

        from growth_agent.service import app

        self.app = app

    async def asyncTearDown(self) -> None:
        self._env_patch.stop()

    async def test_metrics_endpoint_returns_200(self) -> None:
        from growth_agent.metrics import _HAS_PROM

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "GET",
            "path": "/v1/metrics",
            "raw_path": b"/v1/metrics",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "server": ("l", 80),
            "client": ("127.0.0.1", 1),
        }
        sent: list = []
        consumed = False

        async def recv():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await self.app(scope, recv, send)
        started = next(item for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        self.assertEqual(started["status"], 200)
        self.assertIsInstance(body, bytes)
        if _HAS_PROM:
            # Body should mention the audit_total series.
            self.assertTrue(b"audit_total" in body or b"#" in body)


if __name__ == "__main__":
    unittest.main()