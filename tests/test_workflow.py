"""Integration checks for the risks that matter in the portfolio demo."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import Client

from growth_agent.cli import load_briefs
from growth_agent.generation import CompatibleApiGenerator, OfflineTemplateGenerator
from growth_agent.knowledge import KnowledgeBase
from growth_agent.mcp_server import create_server
from growth_agent.pipeline import DEFAULT_KNOWLEDGE, DEFAULT_RULES, PROJECT_ROOT, run_brief
from growth_agent.schemas import GenerationResult


BRIEFS = PROJECT_ROOT / "data" / "demo_briefs.jsonl"


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_mode_uses_same_tool_agent_path(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "en-auto-captions-tutorial")
        adapter = CompatibleApiGenerator("http://localhost:9999/v1", "remote-qwen")
        agent_output = {
            "facts": [], "draft": None,
            "checks": {"passed": False, "reason": "model_endpoint_or_protocol_error"},
            "trace": [{"step": "model_error"}],
            "generation": None, "status": "model_error",
        }
        with tempfile.TemporaryDirectory() as scratch:
            with (
                patch("growth_agent.pipeline.CompatibleApiGenerator.from_environment", return_value=adapter),
                patch("growth_agent.pipeline.run_llm_agent", new_callable=AsyncMock) as agent,
            ):
                agent.return_value = agent_output
                result, run_dir = await run_brief(
                    brief, mode="api", output_root=Path(scratch)
                )
            self.assertEqual(result.status, "model_error")
            self.assertEqual(
                json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))["status"],
                "model_error",
            )
            self.assertIs(agent.await_args.args[1], adapter)

    async def test_qwen_agent_model_error_is_persisted_and_readable(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "en-auto-captions-tutorial")
        agent_output = {
            "facts": [], "draft": None,
            "checks": {"passed": False, "reason": "model_endpoint_or_protocol_error"},
            "trace": [{"step": "model_error", "error_type": "RuntimeError"}],
            "generation": None, "status": "model_error",
        }
        with tempfile.TemporaryDirectory() as scratch:
            with patch("growth_agent.pipeline.run_llm_agent", new_callable=AsyncMock) as agent:
                agent.return_value = agent_output
                result, run_dir = await run_brief(
                    brief, mode="qwen", output_root=Path(scratch)
                )
            self.assertEqual(result.status, "model_error")
            bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "model_error")
            self.assertIn(
                "model service or response protocol failed",
                (run_dir / "review.md").read_text(encoding="utf-8"),
            )
            agent.assert_awaited_once()

    async def test_qwen_agent_success_is_persisted_with_model_metadata(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "en-auto-captions-tutorial")
        fact = KnowledgeBase.from_file(DEFAULT_KNOWLEDGE).get("capcut-auto-captions")
        rules = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))["locales"]["en-US"]
        draft = OfflineTemplateGenerator().generate(brief, [fact], rules).draft
        agent_output = {
            "facts": [fact], "draft": draft,
            "checks": {"passed": True, "manual_review_required": True},
            "trace": [{"step": "model_response", "turn": 1}],
            "generation": GenerationResult(
                draft=draft, mode="qwen_tool_agent", model="mock-qwen",
                prompt_version="test", duration_ms=42, input_tokens=10, output_tokens=5,
            ),
            "status": "pending_review",
        }
        with tempfile.TemporaryDirectory() as scratch:
            with patch("growth_agent.pipeline.run_llm_agent", new_callable=AsyncMock) as agent:
                agent.return_value = agent_output
                result, run_dir = await run_brief(
                    brief, mode="qwen", output_root=Path(scratch)
                )
            self.assertEqual(result.status, "pending_review")
            bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["generation"]["mode"], "qwen_tool_agent")
            self.assertEqual(bundle["generation"]["model"], "mock-qwen")
            self.assertIn("mock-qwen", (run_dir / "review.md").read_text(encoding="utf-8"))

    async def test_offline_draft_has_source_and_stays_pending(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "en-auto-captions-tutorial")
        with tempfile.TemporaryDirectory() as scratch:
            result, run_dir = await run_brief(brief, output_root=Path(scratch))
            self.assertEqual(result.status, "pending_review")
            self.assertFalse(result.reviewed)
            self.assertEqual(result.generation.mode, "offline_template_demo")
            self.assertIn("capcut-auto-captions", [fact["id"] for fact in result.facts])
            self.assertTrue(result.checks["manual_review_required"])
            self.assertIn("https://www.capcut.com/help/", (run_dir / "review.md").read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "editor_decision.json").exists())

    async def test_unverified_promise_does_not_generate_draft(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "es-insufficient-evidence")
        with tempfile.TemporaryDirectory() as scratch:
            result, run_dir = await run_brief(brief, output_root=Path(scratch))
            self.assertEqual(result.status, "insufficient_evidence")
            self.assertIsNone(result.draft)
            self.assertIsNone(result.generation)
            bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["facts"], [])

    async def test_mcp_guardrail_flags_invalid_fact_and_promise(self) -> None:
        server = create_server(DEFAULT_KNOWLEDGE, DEFAULT_RULES)
        async with Client(server) as client:
            response = await client.call_tool(
                "check_draft",
                {
                    "text": "100% accurate and guaranteed traffic",
                    "fact_ids": ["missing-fact"],
                    "locale": "en-US",
                },
            )
        self.assertFalse(response.is_error)
        checks = response.structured_content
        self.assertFalse(checks["passed"])
        self.assertIn("missing-fact", checks["unknown_fact_ids"])
        self.assertIn("100% accurate", checks["forbidden_phrases"])
        self.assertIn("guaranteed traffic", checks["forbidden_phrases"])

    def test_api_adapter_validates_compatible_json_response(self) -> None:
        brief = next(item for item in load_briefs(BRIEFS) if item.id == "en-auto-captions-tutorial")
        fact = KnowledgeBase.from_file(DEFAULT_KNOWLEDGE).get("capcut-auto-captions")
        rules = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))["locales"]["en-US"]
        draft = OfflineTemplateGenerator().generate(brief, [fact], rules).draft
        adapter = CompatibleApiGenerator("http://localhost:9999/v1", "mock-model")
        adapter._request = lambda _messages: {
            "choices": [{"message": {"content": json.dumps(draft.model_dump())}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80},
        }
        result = adapter.generate(brief, [fact], rules)
        self.assertEqual(result.mode, "api")
        self.assertEqual(result.model, "mock-model")
        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.output_tokens, 80)


if __name__ == "__main__":
    unittest.main()
