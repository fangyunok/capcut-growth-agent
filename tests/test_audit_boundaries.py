"""Regression checks for invalid model output and cross-product evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from growth_agent.audit import (
    AuditCitation,
    AuditIssue,
    AuditProposal,
    AuditRequest,
    DEFAULT_AUDIT_KNOWLEDGE,
    run_audit,
)


def _request() -> AuditRequest:
    return AuditRequest(
        id="audit-boundary", product_id="obs-studio", product_name="OBS Studio",
        locale="en-US", feature="virtual-camera", channel="social_post",
        original_copy="OBS Virtual Camera shares a scene with webcam-compatible apps.",
        audience="content creators",
    )


def _response(content: str | None = None, calls: list[dict] | None = None) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": calls}}]}


def _tools_response() -> dict:
    return _response(calls=[
        {
            "id": "evidence", "type": "function",
            "function": {
                "name": "search_knowledge",
                "arguments": json.dumps({
                    "query": "virtual-camera", "product_id": "obs-studio", "top_k": 5,
                }),
            },
        },
        {
            "id": "rules", "type": "function",
            "function": {
                "name": "get_editorial_rules",
                "arguments": json.dumps({"locale": "en-US"}),
            },
        },
    ])


def _proposal(quote: str, fact_id: str = "obs-virtual-camera") -> dict:
    return {
        "revised_copy": quote, "issues": [],
        "citations": [{"quote": quote, "fact_id": fact_id}],
    }


class AuditBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, responses: list, request: AuditRequest | None = None, **kwargs):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "growth_agent.audit._chat_complete", side_effect=responses,
        ):
            bundle, output = await run_audit(
                request or _request(), output_root=Path(temporary), **kwargs,
            )
            saved = json.loads((output / "audit_bundle.json").read_text(encoding="utf-8"))
            self.assertTrue((output / "audit_review.md").is_file())
            self.assertEqual(saved["status"], bundle.status)
        return bundle, saved

    def test_blank_input_and_review_fields_are_rejected(self) -> None:
        cases = [
            (AuditRequest, {**_request().model_dump(), "original_copy": " \n\t "}),
            (AuditProposal, _proposal(" \n\t ")),
            (AuditCitation, {"quote": " \t ", "fact_id": "obs-virtual-camera"}),
            (AuditIssue, {
                "quote": "OBS", "kind": "needs_review", "reason": " \n ", "fact_ids": [],
            }),
        ]
        for model, value in cases:
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    model.model_validate(value)

    async def test_blank_model_revision_cannot_become_pending_review(self) -> None:
        blank = _response(content=json.dumps(_proposal(" \t ")))
        bundle, saved = await self._run([_tools_response(), blank, blank, blank])
        self.assertEqual(bundle.status, "needs_revision")
        self.assertIsNone(saved["proposal"])

    async def test_string_model_message_is_saved_as_model_error(self) -> None:
        bundle, saved = await self._run([{"choices": [{"message": "not a mapping"}]}])
        self.assertEqual(bundle.status, "model_error")
        self.assertIsNone(saved["proposal"])

    async def test_integer_tool_calls_is_saved_as_model_error(self) -> None:
        malformed = {"choices": [{"message": {"content": None, "tool_calls": 42}}]}
        bundle, saved = await self._run([malformed])
        self.assertEqual(bundle.status, "model_error")
        self.assertEqual(saved["facts"], [])

    async def test_list_usage_is_saved_as_model_error(self) -> None:
        malformed = {**_tools_response(), "usage": ["invalid token statistics"]}
        bundle, saved = await self._run([malformed])
        self.assertEqual(bundle.status, "model_error")
        self.assertIsNone(saved["proposal"])

    async def test_empty_choices_is_saved_as_model_error(self) -> None:
        bundle, saved = await self._run([{"choices": []}])
        self.assertEqual(bundle.status, "model_error")
        self.assertIsNone(saved["proposal"])

    async def test_final_network_failure_is_not_a_revision_failure(self) -> None:
        responses = [_tools_response()] + [RuntimeError("Cannot reach model service")] * 3
        bundle, saved = await self._run(responses)
        self.assertEqual(bundle.status, "model_error")
        self.assertIsNone(saved["proposal"])
        self.assertTrue(saved["facts"], "Retrieved evidence should survive the endpoint failure")

    async def test_spaced_percentage_is_still_exposed_when_model_omits_the_issue(self) -> None:
        request = _request().model_copy(update={"original_copy": "OBS achieves 99 % accuracy."})
        revised = "OBS Virtual Camera shares a scene with webcam-compatible apps."
        bundle, saved = await self._run(
            [_tools_response(), _response(content=json.dumps(_proposal(revised)))],
            request=request,
        )
        self.assertEqual(bundle.status, "pending_review")
        self.assertIn("99%", saved["original_checks"]["unsupported_numbers"])
        self.assertTrue(any("99 %" in issue["quote"] for issue in saved["proposal"]["issues"]))

    async def test_same_feature_other_product_cannot_supply_a_citation(self) -> None:
        catalog = json.loads(DEFAULT_AUDIT_KNOWLEDGE.read_text(encoding="utf-8"))
        own = next(fact for fact in catalog["facts"] if fact["id"] == "obs-virtual-camera")
        foreign = deepcopy(own)
        foreign.update({
            "id": "other-virtual-camera", "product_id": "other-product",
            "statement": "The other product guarantees compatible scenes.",
        })
        quote = "OBS Virtual Camera shares a scene with webcam-compatible apps."
        foreign_proposal = _response(content=json.dumps(_proposal(quote, foreign["id"])))
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = Path(temporary) / "mixed-products.json"
            knowledge.write_text(json.dumps({"facts": [foreign, own]}), encoding="utf-8")
            bundle, saved = await self._run(
                [_tools_response(), foreign_proposal, foreign_proposal, foreign_proposal],
                knowledge_path=knowledge,
            )
        self.assertEqual(bundle.status, "needs_revision")
        self.assertIsNone(saved["proposal"])
        self.assertEqual({fact["product_id"] for fact in saved["facts"]}, {"obs-studio"})
        self.assertNotIn(foreign["id"], {fact["id"] for fact in saved["facts"]})


if __name__ == "__main__":
    unittest.main()
