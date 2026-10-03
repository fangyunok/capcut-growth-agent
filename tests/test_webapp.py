"""Exercise real ASGI routes and persisted jobs without optional HTTP clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from growth_agent.approval import load_approved_copy
from growth_agent.audit import AuditBundle, AuditProposal, AuditRequest
from growth_agent.jobs import JobManager, JobQueueFull
from growth_agent.media import copy_digest
from growth_agent.webapp import create_app


def _request() -> dict:
    return {
        "id": "asgi-test", "product_id": "obs-studio", "product_name": "OBS Studio",
        "locale": "en-US", "feature": "virtual-camera", "channel": "social_post",
        "original_copy": "Every app supports this product. <script>original()</script>",
        "audience": "creators",
    }


def _bundle(request: AuditRequest, *, status: str = "pending_review", source_url: str = "https://example.com/product") -> AuditBundle:
    text = "Share a scene with webcam-compatible applications. Review compatibility before use."
    return AuditBundle(
        run_id="web-test-" + uuid.uuid4().hex[:12], request=request, status=status, model="scripted-test-model",
        facts=[{
            "id": "fact-1", "product_id": request.product_id, "feature": request.feature,
            "source_title": "Official <script>source()</script>", "source_url": source_url,
            "source_quote": "Share a scene with webcam-compatible applications.",
            "checked_at": "2026-10-03", "availability_note": "Compatibility varies by application.",
        }],
        proposal=AuditProposal.model_validate({
            "revised_copy": text,
            "issues": [{"quote": "Every app", "kind": "overstated", "reason": "Check application compatibility.", "fact_ids": ["fact-1"]}],
            "citations": [{"quote": "Share a scene with webcam-compatible applications.", "fact_id": "fact-1"}],
        }) if status == "pending_review" else None,
        revised_checks={"passed": status == "pending_review", "semantic_support_verified": False},
    )


def _save(root: Path, bundle: AuditBundle) -> Path:
    directory = root / bundle.run_id
    directory.mkdir(parents=True)
    (directory / "audit_bundle.json").write_text(json.dumps(bundle.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


async def _http(app, path: str, *, method: str = "GET", data=None, form=None, raw: bytes | None = None, headers: dict | None = None):
    extra = {key.lower(): value for key, value in (headers or {}).items()}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        extra.setdefault("content-type", "application/json")
    elif form is not None:
        body = urlencode(form).encode("utf-8")
        extra.setdefault("content-type", "application/x-www-form-urlencoded")
    else:
        body = raw or b""
    extra.setdefault("host", "localhost:7860")
    if method == "POST":
        extra.setdefault("content-length", str(len(body)))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "http", "method": method,
        "path": path, "raw_path": path.encode("utf-8"), "root_path": "", "query_string": b"",
        "headers": [(name.encode("ascii"), value.encode("ascii")) for name, value in extra.items()],
        "server": ("localhost", 7860), "client": ("127.0.0.1", 12345),
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
    return started["status"], dict(started.get("headers", [])), payload


async def _finished(manager: JobManager, job_id: str) -> dict:
    for _ in range(200):
        record = await manager.get(job_id)
        if record["status"] not in {"queued", "running"}:
            return record
        await asyncio.sleep(0.005)
    raise AssertionError("A short test job did not finish")


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    async def test_queue_bounds_concurrency_and_persists_completed_records(self) -> None:
        manager = JobManager(self.root, max_pending=2, concurrency=1)
        self.addAsyncCleanup(manager.close)
        gate = asyncio.Event()
        active = peak = 0
        async def operation():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await gate.wait()
            active -= 1
            return {"run_id": "test-run", "result_status": "pending_review"}
        first = await manager.submit({}, operation)
        second = await manager.submit({}, operation)
        with self.assertRaises(JobQueueFull):
            await manager.submit({}, operation)
        await asyncio.sleep(0)
        self.assertEqual((await manager.get(first["job_id"]))["status"], "running")
        self.assertEqual((await manager.get(second["job_id"]))["status"], "queued")
        gate.set()
        self.assertEqual((await _finished(manager, first["job_id"]))["status"], "completed")
        self.assertEqual((await _finished(manager, second["job_id"]))["status"], "completed")
        self.assertEqual(peak, 1)
        restarted = JobManager(self.root)
        self.addAsyncCleanup(restarted.close)
        self.assertEqual((await restarted.get(first["job_id"]))["status"], "completed")

    async def test_timeout_and_exception_record_safe_errors_without_exception_secrets(self) -> None:
        manager = JobManager(self.root, timeout_seconds=0.01)
        self.addAsyncCleanup(manager.close)
        async def blocked():
            await asyncio.sleep(10)
        slow = await manager.submit({}, blocked)
        self.assertEqual((await _finished(manager, slow["job_id"]))["error"]["code"], "job_timeout")
        async def failing():
            raise RuntimeError("secret-test-token")
        failed = await manager.submit({}, failing)
        record = await _finished(manager, failed["job_id"])
        self.assertEqual(record["error"]["type"], "RuntimeError")
        self.assertNotIn("secret-test-token", json.dumps(record))

    async def test_shutdown_before_task_starts_is_marked_interrupted(self) -> None:
        manager = JobManager(self.root)
        async def operation():
            await asyncio.sleep(10)
        job = await manager.submit({}, operation)
        await manager.close()
        record = await manager.get(job["job_id"])
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["error"]["code"], "server_shutdown")

    async def test_restart_marks_pending_records_interrupted_without_replay(self) -> None:
        manager = JobManager(self.root)
        await manager.start()
        job_id = "job-" + uuid.uuid4().hex
        path = manager.record_root / f"{job_id}.json"
        path.write_text(json.dumps({"job_id": job_id, "request": {}, "mode": "qwen", "strategy": "agent", "status": "running"}), encoding="utf-8")
        restarted = JobManager(self.root)
        self.addAsyncCleanup(restarted.close)
        record = await restarted.get(job_id)
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["error"]["code"], "process_restarted")
        self.assertFalse(restarted._tasks)


class WebappTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        async def runner(request, **kwargs):
            bundle = _bundle(request)
            return bundle, _save(Path(kwargs["output_root"]), bundle)
        self.app = create_app(output_root=self.root, audit_runner=runner)
        self.addAsyncCleanup(self.app.state.jobs.close)

    async def test_json_api_creates_background_job_and_returns_structured_review_link(self) -> None:
        status, _, body = await _http(self.app, "/api/audits", method="POST", data={"request": _request(), "mode": "api", "strategy": "rag"}, headers={"origin": "http://localhost:7860"})
        self.assertEqual(status, 202)
        response = json.loads(body)
        self.assertEqual(response["status"], "queued")
        record = await _finished(self.app.state.jobs, response["job_id"])
        self.assertEqual(record["status"], "completed")
        status, _, body = await _http(self.app, response["status_url"])
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertEqual(view["review_url"], f"/audits/{record['run_id']}")
        self.assertEqual(view["mode"], "api")

    async def test_html_form_returns_immediate_job_page_without_waiting_for_model(self) -> None:
        gate = asyncio.Event()
        async def waiting(request, **kwargs):
            await gate.wait()
            bundle = _bundle(request)
            return bundle, _save(Path(kwargs["output_root"]), bundle)
        self.app.state.audit_runner = waiting
        status, headers, _ = await _http(self.app, "/audit", method="POST", form={**_request(), "mode": "qwen"})
        self.assertEqual(status, 303)
        location = headers[b"location"].decode()
        status, _, body = await _http(self.app, location)
        self.assertEqual(status, 200)
        self.assertIn(b"refreshJob", body)
        self.assertFalse(gate.is_set())
        gate.set()
        self.assertEqual((await _finished(self.app.state.jobs, location.rsplit("/", 1)[1]))["status"], "completed")

    async def test_default_web_runner_uses_cancellable_async_model_transport(self) -> None:
        scripted_runner = self.app.state.audit_runner
        self.app.state.audit_runner = None
        with patch('growth_agent.webapp.run_audit', new=AsyncMock(side_effect=scripted_runner)) as real_runner:
            status, _, body = await _http(self.app, '/api/audits', method='POST', data={'request': _request()})
            self.assertEqual(status, 202)
            job = await _finished(self.app.state.jobs, json.loads(body)['job_id'])
            self.assertEqual(job['status'], 'completed')
            real_runner.assert_awaited_once()
            self.assertEqual(real_runner.call_args.kwargs['model_transport'], 'async')

    async def test_malformed_json_schema_encoding_and_oversize_requests_are_rejected(self) -> None:
        inputs = [
            {"raw": b"{", "headers": {"content-type": "application/json"}},
            {"data": []}, {"data": {"request": _request(), "publish": True}},
            {"data": {"request": {**_request(), "locale": "fr-FR"}}},
            {"data": {"request": {**_request(), "audience": 10}}},
            {"data": {"request": {**_request(), "original_copy": "bad\ud800"}}},
        ]
        for kwargs in inputs:
            with self.subTest(kwargs=kwargs):
                status, _, _ = await _http(self.app, "/api/audits", method="POST", **kwargs)
                self.assertEqual(status, 400)
        status, _, _ = await _http(self.app, "/api/audits", method="POST", raw=b"{}")
        self.assertEqual(status, 415)
        status, _, _ = await _http(self.app, "/api/audits", method="POST", raw=b"x" * 32769, headers={"content-type": "application/json"})
        self.assertEqual(status, 413)
        self.assertFalse(await self.app.state.jobs.recent())

    async def test_cross_origin_and_cross_site_posts_are_rejected_on_all_routes(self) -> None:
        for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}, {"origin": "http://localhost:7860", "sec-fetch-site": "same-site"}):
            for path in ("/api/audits", "/audit", "/run", "/audits/no-run/approve"):
                with self.subTest(headers=headers, path=path):
                    status, _, _ = await _http(self.app, path, method="POST", data={"request": _request()}, headers=headers)
                    self.assertEqual(status, 403)

    async def test_review_html_shows_sources_and_escapes_untrusted_text_and_urls(self) -> None:
        bundle = _bundle(AuditRequest.model_validate(_request()), source_url="javascript:source()")
        _save(self.root, bundle)
        status, _, body = await _http(self.app, f"/audits/{bundle.run_id}")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("建议修订稿", text)
        self.assertIn("本次检索到的资料", text)
        self.assertIn("&lt;script&gt;original()&lt;/script&gt;", text)
        self.assertNotIn("<script>original()", text)
        self.assertNotIn("href='javascript:", text)
        self.assertIn(copy_digest(bundle.proposal.revised_copy), text)
        self.assertIn("<details>", text)
        self.assertIn("source_checked", text)

    async def test_approval_requires_source_check_and_current_version_then_displays_confirmation(self) -> None:
        bundle = _bundle(AuditRequest.model_validate(_request()))
        directory = _save(self.root, bundle)
        path = f"/audits/{bundle.run_id}/approve"
        form = {"reviewer": "Test editor", "expected_version": copy_digest(bundle.proposal.revised_copy), "audit_digest": hashlib.sha256((directory / 'audit_bundle.json').read_bytes()).hexdigest()}
        status, _, _ = await _http(self.app, path, method="POST", form=form)
        self.assertEqual(status, 409)
        self.assertFalse((directory / "audit_approval.json").exists())
        status, _, _ = await _http(self.app, path, method="POST", form={**form, "source_checked": "yes", "expected_version": "0" * 64})
        self.assertEqual(status, 409)
        status, _, _ = await _http(self.app, path, method="POST", form={**form, "source_checked": "yes"})
        self.assertEqual(status, 303)
        self.assertEqual(load_approved_copy(directory).text, bundle.proposal.revised_copy)
        status, _, body = await _http(self.app, f"/audits/{bundle.run_id}")
        self.assertIn("当前版本已确认", body.decode())
        self.assertNotIn("name='source_checked'", body.decode())
        raw = json.loads((directory / "audit_bundle.json").read_text(encoding="utf-8"))
        raw["facts"][0]["source_quote"] = "Changed evidence."
        (directory / "audit_bundle.json").write_text(json.dumps(raw), encoding="utf-8")
        _, _, body = await _http(self.app, f"/audits/{bundle.run_id}")
        self.assertIn("旧确认已失效", body.decode())

    async def test_source_changed_after_display_prevents_first_approval(self) -> None:
        bundle = _bundle(AuditRequest.model_validate(_request()))
        directory = _save(self.root, bundle)
        status, _, body = await _http(self.app, f"/audits/{bundle.run_id}")
        self.assertEqual(status, 200)
        expected_digest = hashlib.sha256((directory / 'audit_bundle.json').read_bytes()).hexdigest()
        self.assertIn(expected_digest, body.decode())
        raw = bundle.model_dump()
        raw['facts'][0]['source_quote'] = 'A changed source with the same proposed wording.'
        (directory / 'audit_bundle.json').write_text(json.dumps(raw), encoding='utf-8')
        status, _, _ = await _http(self.app, f"/audits/{bundle.run_id}/approve", method='POST', form={
            'reviewer': 'Test editor', 'expected_version': copy_digest(bundle.proposal.revised_copy),
            'audit_digest': expected_digest, 'source_checked': 'yes',
        })
        self.assertEqual(status, 409)
        self.assertFalse((directory / 'audit_approval.json').exists())

    async def test_model_errors_and_unexpected_runner_failures_are_honest_persisted_jobs(self) -> None:
        async def model_error(request, **kwargs):
            bundle = _bundle(request, status="model_error")
            return bundle, _save(Path(kwargs["output_root"]), bundle)
        self.app.state.audit_runner = model_error
        status, _, body = await _http(self.app, "/api/audits", method="POST", data={"request": _request()})
        job = await _finished(self.app.state.jobs, json.loads(body)["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "model_error")
        _, _, body = await _http(self.app, f"/audits/{job['run_id']}")
        self.assertIn("模型服务调用失败", body.decode())
        self.assertNotIn("name='source_checked'", body.decode())
        async def fail(request, **kwargs):
            raise RuntimeError("secret-test-token")
        self.app.state.audit_runner = fail
        _, _, body = await _http(self.app, "/api/audits", method="POST", data={"request": _request()})
        failed = await _finished(self.app.state.jobs, json.loads(body)["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("secret-test-token", json.dumps(failed))

    async def test_queue_capacity_returns_429_and_does_not_create_extra_record(self) -> None:
        await self.app.state.jobs.close()
        gate = asyncio.Event()
        async def blocked(request, **kwargs):
            await gate.wait()
            bundle = _bundle(request)
            return bundle, _save(Path(kwargs["output_root"]), bundle)
        self.app = create_app(output_root=self.root, audit_runner=blocked, max_pending=1, concurrency=1)
        self.addAsyncCleanup(self.app.state.jobs.close)
        first, _, _ = await _http(self.app, "/api/audits", method="POST", data={"request": _request()})
        second, _, _ = await _http(self.app, "/api/audits", method="POST", data={"request": _request()})
        self.assertEqual((first, second), (202, 429))
        self.assertEqual(len(await self.app.state.jobs.recent()), 1)

    async def test_fixed_media_route_requires_completed_metadata_and_safe_video_path(self) -> None:
        directory = self.root / "video-test"
        directory.mkdir()
        (directory / "video.mp4").write_bytes(b"fixed test video bytes")
        path = directory / "media_bundle.json"
        metadata = {"run_id": directory.name, "status": "failed", "artifacts": {"video": "video.mp4"}}
        path.write_text(json.dumps(metadata), encoding="utf-8")
        status, _, _ = await _http(self.app, "/media/video-test/video")
        self.assertEqual(status, 404)
        metadata["status"] = "completed"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        status, headers, body = await _http(self.app, "/media/video-test/video")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"fixed test video bytes")
        self.assertEqual(headers[b"content-type"], b"video/mp4")
        metadata["artifacts"]["video"] = "../outside.mp4"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertEqual((await _http(self.app, "/media/video-test/video"))[0], 404)

    async def test_symlink_run_escape_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            bundle = _bundle(AuditRequest.model_validate(_request()))
            actual = _save(Path(outside), bundle)
            try:
                (self.root / bundle.run_id).symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("Platform does not allow creating a test symlink")
            self.assertEqual((await _http(self.app, f"/audits/{bundle.run_id}"))[0], 404)

    async def test_home_health_and_legacy_routes_remain_available(self) -> None:
        for path in ("/", "/health", "/generate"):
            with self.subTest(path=path):
                self.assertEqual((await _http(self.app, path))[0], 200)
        self.assertEqual((await _http(self.app, "/api/jobs/job-not-valid"))[0], 404)
        self.assertEqual((await _http(self.app, "/runs/not-found"))[0], 404)


if __name__ == "__main__":
    unittest.main()
