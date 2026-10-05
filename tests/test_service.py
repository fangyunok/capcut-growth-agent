"""End-to-end tests for the FastAPI service layer.

Tests run against the live ASGI app and a mocked ``run_audit`` so they
do not require a real LLM. They follow the project's existing pattern
of constructing ASGI scopes by hand instead of pulling in ``httpx``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from growth_agent.audit import AuditBundle, AuditProposal, AuditRequest


def _request_body() -> dict:
    return {
        "id": "svc-test",
        "product_id": "obs-studio",
        "product_name": "OBS Studio",
        "locale": "en-US",
        "feature": "virtual-camera",
        "channel": "social_post",
        "original_copy": "Every app supports OBS without configuration.",
        "audience": "creators",
    }


def _fake_bundle(request: AuditRequest) -> AuditBundle:
    return AuditBundle(
        run_id="svc-run-" + uuid.uuid4().hex[:8],
        request=request,
        status="pending_review",
        model="scripted-test",
        strategy="agent",
        facts=[{
            "id": "fact-1", "product_id": request.product_id, "feature": request.feature,
            "source_quote": "Share a scene with webcam-compatible applications.",
            "source_url": "https://example.com/product",
            "checked_at": "2026-10-04",
        }],
        proposal=AuditProposal.model_validate({
            "revised_copy": "Share a scene with webcam-compatible applications.",
            "issues": [],
            "citations": [{"quote": "Share a scene.", "fact_id": "fact-1"}],
        }),
        revised_checks={"passed": True, "semantic_support_verified": False},
        duration_ms=12,
    )


def _fake_runner(req: AuditRequest, **kw):
    """Mimic ``run_audit`` signature: returns ``(AuditBundle, Path)``."""
    return _fake_bundle(req), Path("/tmp/fake-run-dir")


async def _http(app, path: str, *, method: str = "GET", data=None, headers=None):
    extra = {key.lower(): value for key, value in (headers or {}).items()}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        extra.setdefault("content-type", "application/json")
    else:
        body = b""
    extra.setdefault("host", "localhost:8080")
    if method == "POST":
        extra.setdefault("content-length", str(len(body)))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "http", "method": method,
        "path": path, "raw_path": path.encode("utf-8"), "root_path": "", "query_string": b"",
        "headers": [(name.encode("ascii"), value.encode("ascii")) for name, value in extra.items()],
        "server": ("localhost", 8080), "client": ("127.0.0.1", 12345),
    }
    sent = []
    consumed = False
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    await app(scope, receive, send)
    started = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    headers = {name.decode("ascii").lower(): value.decode("ascii") for name, value in started.get("headers", [])}
    return started["status"], headers, payload


def _build_app(tmp_path: Path):
    from growth_agent.service import app as fastapi_app

    fastapi_app.dependency_overrides = {}
    env_patches = {
        "GROWTH_AUDIT_KNOWLEDGE": str(tmp_path / "knowledge.json"),
        "GROWTH_AUDIT_RULES": str(tmp_path / "rules.json"),
        "GROWTH_OUTPUT_ROOT": str(tmp_path / "runs"),
    }
    for k, v in env_patches.items():
        if k != "GROWTH_OUTPUT_ROOT":
            target = Path(v)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return fastapi_app, patch.dict(os.environ, env_patches, clear=False)


class HealthAndReadyzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile_dir())
        self.app, self.env_patch = _build_app(self.tmp)
        self.env_patch.start()

    async def asyncTearDown(self) -> None:
        self.env_patch.stop()

    async def test_healthz_returns_200(self) -> None:
        status, headers, body = await _http(self.app, "/v1/healthz")
        self.assertEqual(status, 200)
        self.assertIn(b"ok", body)
        self.assertIn(b"claim-studio", body)
        self.assertIn("x-trace-id", headers)

    async def test_readyz_returns_200_when_knowledge_present(self) -> None:
        status, _, body = await _http(self.app, "/v1/readyz")
        self.assertEqual(status, 200)
        self.assertIn(b"ok", body)


class SubmitAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile_dir())
        self.app, self.env_patch = _build_app(self.tmp)
        self.env_patch.start()
        self.mock_run = AsyncMock(side_effect=lambda req, **kw: _fake_runner(req, **kw))
        self.patcher = patch("growth_agent.service.run_audit", self.mock_run)
        self.patcher.start()

    async def asyncTearDown(self) -> None:
        self.patcher.stop()
        self.env_patch.stop()

    async def test_submit_audit_happy_path(self) -> None:
        status, headers, body = await _http(
            self.app, "/v1/audits", method="POST",
            data={"request": _request_body(), "strategy": "agent"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "pending_review")
        self.assertEqual(payload["strategy"], "agent")
        self.assertIn("run_id", payload)
        self.assertGreater(payload["duration_ms"], 0)
        self.assertIn("x-trace-id", headers)

    async def test_submit_audit_propagates_client_trace_id(self) -> None:
        custom_trace = "client-supplied-trace-12345"
        _, headers, _ = await _http(
            self.app, "/v1/audits", method="POST",
            data={"request": _request_body()},
            headers={"X-Trace-Id": custom_trace},
        )
        self.assertEqual(headers["x-trace-id"], custom_trace)

    async def test_submit_audit_generates_trace_id_when_absent(self) -> None:
        _, headers, _ = await _http(
            self.app, "/v1/audits", method="POST",
            data={"request": _request_body()},
        )
        trace = headers.get("x-trace-id", "")
        self.assertEqual(len(trace), 36, f"Expected UUID, got '{trace}'")

    async def test_submit_audit_returns_400_on_validation_error(self) -> None:
        bad_request = _request_body()
        bad_request["locale"] = "fr-FR"  # not in the Literal list
        status, _, body = await _http(
            self.app, "/v1/audits", method="POST", data={"request": bad_request},
        )
        self.assertEqual(status, 422)

    async def test_submit_audit_returns_400_on_missing_request(self) -> None:
        status, _, body = await _http(self.app, "/v1/audits", method="POST", data={})
        self.assertEqual(status, 422)

    async def test_submit_audit_returns_413_on_oversized_body(self) -> None:
        huge = "x" * 40000
        bad_request = _request_body()
        bad_request["original_copy"] = huge
        status, _, body = await _http(self.app, "/v1/audits", method="POST", data={"request": bad_request})
        self.assertEqual(status, 413)
        self.assertIn(b"payload_too_large", body)

    async def test_submit_audit_returns_500_on_internal_error(self) -> None:
        with patch("growth_agent.service.run_audit", AsyncMock(side_effect=RuntimeError("boom"))):
            status, _, body = await _http(
                self.app, "/v1/audits", method="POST",
                data={"request": _request_body()},
            )
        self.assertEqual(status, 500)
        self.assertIn(b"audit_failed", body)


class RetrieveAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile_dir())
        self.app, self.env_patch = _build_app(self.tmp)
        self.env_patch.start()
        self.bundle = _fake_bundle(AuditRequest.model_validate(_request_body()))
        self.run_dir = self.tmp / "runs" / self.bundle.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "audit_bundle.json").write_text(
            json.dumps(self.bundle.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def asyncTearDown(self) -> None:
        self.env_patch.stop()

    async def test_get_audit_returns_stored_bundle(self) -> None:
        status, _, body = await _http(self.app, f"/v1/audits/{self.bundle.run_id}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["run_id"], self.bundle.run_id)
        self.assertEqual(payload["status"], "pending_review")

    async def test_get_audit_returns_404_for_missing(self) -> None:
        status, _, _ = await _http(self.app, "/v1/audits/does-not-exist")
        self.assertEqual(status, 404)

    async def test_get_audit_rejects_path_traversal(self) -> None:
        status, _, _ = await _http(self.app, "/v1/audits/..%2Fruns")
        self.assertIn(status, (400, 404))


class OpenApiSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile_dir())
        self.app, self.env_patch = _build_app(self.tmp)
        self.env_patch.start()

    async def asyncTearDown(self) -> None:
        self.env_patch.stop()

    async def test_openapi_schema_lists_endpoints(self) -> None:
        status, _, body = await _http(self.app, "/openapi.json")
        self.assertEqual(status, 200)
        schema = json.loads(body)
        paths = schema.get("paths", {})
        self.assertIn("/v1/audits", paths)
        self.assertIn("/v1/healthz", paths)
        self.assertIn("/v1/readyz", paths)
        self.assertIn("/v1/audits/{run_id}", paths)


# ---------- Helper ----------
import tempfile

def tempfile_dir() -> str:
    return tempfile.mkdtemp(prefix="claim-studio-svc-")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()