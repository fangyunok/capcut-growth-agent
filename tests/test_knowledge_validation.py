"""Reject malformed source cards before retrieval or model calls begin."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from growth_agent.audit import AuditRequest, DEFAULT_AUDIT_KNOWLEDGE
from growth_agent.knowledge import KnowledgeBase


class KnowledgeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = json.loads(DEFAULT_AUDIT_KNOWLEDGE.read_text(encoding="utf-8"))
        self.fact = catalog["facts"][0]

    def _assert_invalid(self, field: str, value) -> None:
        fact = deepcopy(self.fact)
        fact[field] = value
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "knowledge.json"
            path.write_text(json.dumps({"facts": [fact]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                KnowledgeBase.from_file(path)

    def test_non_string_statement_fails_when_catalog_is_loaded(self) -> None:
        self._assert_invalid("statement", {"wrong": "type"})

    def test_localized_wording_must_be_a_mapping_of_nonblank_strings(self) -> None:
        for value in (["wrong container"], {"zh-CN": {"wrong": "value"}}, {"zh-CN": " \t "}):
            with self.subTest(value=value):
                self._assert_invalid("localized_statement", value)

    def test_source_links_require_public_web_schemes(self) -> None:
        for value in (
            "javascript:alert(1)",
            "file:///C:/private-note.txt",
            "data:text/html,<h1>Not a source</h1>",
            "https:///missing-host",
        ):
            with self.subTest(url=value):
                self._assert_invalid("source_url", value)

    def test_valid_catalog_preserves_source_evidence_and_product_identity(self) -> None:
        knowledge = KnowledgeBase.from_file(DEFAULT_AUDIT_KNOWLEDGE)
        evidence = knowledge.get(self.fact["id"])
        self.assertEqual(evidence["source_quote"], self.fact["source_quote"])
        self.assertEqual(evidence["source_url"], self.fact["source_url"])
        self.assertEqual(evidence["product_id"], self.fact["product_id"])


class CuratedAuditEvaluationDataTests(unittest.TestCase):
    def test_case_labels_match_valid_inputs_and_exact_product_feature_sources(self) -> None:
        data_root = Path(__file__).resolve().parents[1] / "data"
        cases = [
            AuditRequest.model_validate_json(line)
            for line in (data_root / "audit_eval_cases.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        gold = json.loads((data_root / "audit_eval_gold.json").read_text(encoding="utf-8"))
        labels = {item["id"]: item for item in gold["cases"]}
        demos = [
            AuditRequest.model_validate_json(line)
            for line in (data_root / "audit_cases.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        facts = {fact["id"]: fact for fact in KnowledgeBase.from_file(DEFAULT_AUDIT_KNOWLEDGE).all()}
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case.id for case in cases}), len(cases))
        self.assertEqual(len(labels), len(gold["cases"]))
        self.assertEqual({case.id for case in cases}, set(labels))
        self.assertTrue({case.id for case in cases}.isdisjoint(case.id for case in demos))
        self.assertTrue({case.original_copy for case in cases}.isdisjoint(case.original_copy for case in demos))
        self.assertEqual(sum(case.locale == "en-US" for case in cases), 10)
        self.assertEqual(sum(case.locale == "zh-CN" for case in cases), 10)
        self.assertEqual(sum(label["abstain_expected"] for label in labels.values()), 4)
        self.assertEqual({fact_id for label in labels.values() for fact_id in label["gold_fact_ids"]}, set(facts))
        tags_by_locale = {"en-US": set(), "zh-CN": set()}
        for case in cases:
            label = labels[case.id]
            with self.subTest(case_id=case.id):
                self.assertIs(type(label["abstain_expected"]), bool)
                self.assertTrue(label["risk_tags"])
                self.assertTrue(all(isinstance(tag, str) and tag.strip() for tag in label["risk_tags"]))
                tags_by_locale[case.locale].update(label["risk_tags"])
                for phrase in label["must_remove"]:
                    self.assertTrue(isinstance(phrase, str) and phrase.strip())
                    self.assertIn(phrase.casefold(), case.original_copy.casefold())
                for fact_id in label["gold_fact_ids"]:
                    self.assertIn(fact_id, facts)
                    self.assertEqual(facts[fact_id]["product_id"], case.product_id)
                    self.assertEqual(facts[fact_id]["feature"], case.feature)
                matching = [fact for fact in facts.values() if (
                    fact["product_id"] == case.product_id and fact["feature"] == case.feature
                )]
                if label["abstain_expected"]:
                    self.assertFalse(matching)
                    self.assertEqual(label["gold_fact_ids"], [])
                    self.assertEqual(label["must_remove"], [])
                else:
                    self.assertTrue(matching)
                    self.assertTrue(label["gold_fact_ids"])
        for locale, tags in tags_by_locale.items():
            with self.subTest(locale=locale):
                self.assertTrue({"supported_copy", "missing_feature", "wrong_product", "instruction_injection"} <= tags)
        self.assertIn("not a blinded", gold["scope"])
        self.assertIn("semantic factual accuracy", gold["scope"])


if __name__ == "__main__":
    unittest.main()
