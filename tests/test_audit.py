"""Check the new claim-review workflow with scripted model responses."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from growth_agent.audit import AuditRequest, _structural_errors, run_audit
from growth_agent.audit_evaluation import evaluate_audits
from growth_agent.knowledge import KnowledgeBase


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _response(content: str | None = None, calls: list[dict] | None = None) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": calls}}], "usage": {}}


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_preserves_evidence_and_flags_original_overclaim(self) -> None:
        request = AuditRequest(
            id="test-virtual", product_id="obs-studio", product_name="OBS Studio",
            locale="en-US", feature="virtual-camera", channel="social_post",
            original_copy="OBS virtual camera works with every app. Start it from the Controls dock.",
            audience="creators",
        )
        revised = "OBS Virtual Camera can share a scene with apps that accept a webcam. Start it from the Controls dock."
        proposal = {
            "revised_copy": revised,
            "issues": [],
            "citations": [{
                "quote": "OBS Virtual Camera can share a scene with apps that accept a webcam.",
                "fact_id": "obs-virtual-camera",
            }],
        }
        responses = iter([
            _response(calls=[
                _call("search", "search_knowledge", {"query": "obs-studio virtual-camera", "top_k": 5}),
                _call("rules", "get_editorial_rules", {"locale": "en-US"}),
            ]),
            _response(content=json.dumps(proposal)),
        ])
        with tempfile.TemporaryDirectory() as temporary, patch(
            "growth_agent.audit._chat_complete", side_effect=lambda *_: next(responses)
        ):
            result, output = await run_audit(request, output_root=Path(temporary))
            self.assertTrue((output / "audit_review.md").is_file())
        self.assertEqual(result.status, "pending_review")
        self.assertEqual(result.proposal.revised_copy, revised)
        self.assertTrue(any("works with every app" in issue.quote for issue in result.proposal.issues))
        self.assertFalse(result.revised_checks["semantic_support_verified"])
        self.assertEqual([step["tool"] for step in result.trace if step["step"] == "tool_call"], [
            "search_knowledge", "get_editorial_rules",
        ])

    async def test_exact_product_feature_filter_stops_unsupported_request(self) -> None:
        request = AuditRequest(
            id="test-missing", product_id="obs-studio", product_name="OBS Studio",
            locale="zh-CN", feature="auto-publish", channel="social_post",
            original_copy="OBS 会自动发布所有录像。", audience="运营人员",
        )
        responses = iter([_response(calls=[
            _call("search", "search_knowledge", {"query": "obs-studio auto-publish", "top_k": 5}),
            _call("rules", "get_editorial_rules", {"locale": "zh-CN"}),
        ])])
        with tempfile.TemporaryDirectory() as temporary, patch(
            "growth_agent.audit._chat_complete", side_effect=lambda *_: next(responses)
        ):
            result, _ = await run_audit(request, output_root=Path(temporary))
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertIsNone(result.proposal)

    def test_wrong_citation_location_is_rejected(self) -> None:
        from growth_agent.audit import AuditProposal

        proposal = AuditProposal.model_validate({
            "revised_copy": "Use OBS Studio's Virtual Camera with webcam-compatible apps.",
            "issues": [],
            "citations": [{"quote": "Unrelated claim", "fact_id": "obs-virtual-camera"}],
        })
        self.assertIn(
            "citation quote is absent from revised copy",
            _structural_errors(proposal, "Original copy", {"obs-virtual-camera"}),
        )

    async def test_fixed_rag_uses_one_model_call_with_scoped_evidence(self) -> None:
        request = AuditRequest(
            id="rag-supported", product_id="obs-studio", product_name="OBS Studio",
            locale="en-US", feature="virtual-camera", channel="social_post",
            original_copy="Start Virtual Camera from the Controls dock.", audience="creators",
        )
        proposal = {"revised_copy": request.original_copy, "issues": [], "citations": [
            {"quote": request.original_copy, "fact_id": "obs-virtual-camera-start"},
        ]}
        observed = []
        def model(generator, messages, json_mode):
            observed.append((messages, json_mode))
            return {"choices": [{"message": {"content": json.dumps(proposal)}}],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 16}}
        with tempfile.TemporaryDirectory() as temporary, patch("growth_agent.audit._chat_complete", side_effect=model):
            result, _ = await run_audit(request, strategy="rag", output_root=Path(temporary))
        self.assertEqual(result.status, "pending_review")
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][1])
        supplied = json.loads(observed[0][0][2]["content"])
        self.assertTrue(supplied["retrieved_evidence"])
        self.assertTrue(all(f["product_id"] == request.product_id for f in supplied["retrieved_evidence"]))
        self.assertEqual(result.usage, {"model_calls": 1, "tool_calls": 4, "input_tokens": 42, "output_tokens": 16})

    async def test_fixed_rag_missing_evidence_avoids_model_call(self) -> None:
        request = AuditRequest(
            id="rag-missing", product_id="other-product", product_name="Other product",
            locale="zh-CN", feature="virtual-camera", channel="social_post",
            original_copy="开启虚拟摄像头。", audience="创作者",
        )
        with tempfile.TemporaryDirectory() as temporary, patch("growth_agent.audit._chat_complete") as model:
            result, output = await run_audit(request, strategy="rag", output_root=Path(temporary))
            self.assertTrue((output / "audit_bundle.json").is_file())
        model.assert_not_called()
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertIsNone(result.proposal)
        self.assertEqual(result.facts, [])

    def test_product_filter_prevents_cross_product_evidence(self) -> None:
        catalog = KnowledgeBase.from_file(Path(__file__).resolve().parents[1] / "data" / "audit_knowledge.json")
        self.assertTrue(catalog.search("virtual-camera", product_id="obs-studio"))
        self.assertEqual(catalog.search("virtual-camera", product_id="another-product"), [])

    def test_regression_report_counts_failed_removal_and_abstention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gold = root / "gold.json"
            gold.write_text(json.dumps({"cases": [
                {"id": "normal", "abstain_expected": False, "must_remove": ["guaranteed"]},
                {"id": "missing", "abstain_expected": True, "must_remove": []},
            ]}), encoding="utf-8")
            normal = root / "normal.json"
            normal.write_text(json.dumps({
                "request": {"id": "normal"}, "status": "pending_review",
                "facts": [{"id": "f1"}], "revised_checks": {"passed": True},
                "proposal": {
                    "revised_copy": "Still guaranteed.",
                    "citations": [{"quote": "Still guaranteed.", "fact_id": "f1"}],
                },
            }), encoding="utf-8")
            missing = root / "missing.json"
            missing.write_text(json.dumps({
                "request": {"id": "missing"}, "status": "insufficient_evidence",
                "proposal": None, "facts": [],
            }), encoding="utf-8")
            report = evaluate_audits([normal, missing], gold)
        self.assertEqual(report["workflow_success"]["numerator"], 2)
        self.assertEqual(report["correct_abstention"]["numerator"], 1)
        self.assertEqual(report["required_risk_phrases_removed"]["numerator"], 0)


if __name__ == "__main__":
    unittest.main()
