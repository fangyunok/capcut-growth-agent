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


def _load_cases(path: Path) -> list[AuditRequest]:
    return [
        AuditRequest.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class AuditSuiteIntegrityTests(unittest.TestCase):
    """Both audit suites must be internally consistent and mutually disjoint.

    The earlier version of this test hard-coded "20 cases" and "10 per locale",
    which made the numbers the contract instead of the properties. These tests
    assert the properties, so the suites can grow without weakening anything.
    """

    SUITES = {
        "dev": ("audit_eval_cases.jsonl", "audit_eval_gold.json"),
        "test": ("audit_test_cases.jsonl", "audit_test_gold.json"),
    }
    MINIMUM_CASES_PER_SUITE = 20
    REQUIRED_RISK_TAGS = {
        "supported_copy", "missing_feature", "wrong_product", "instruction_injection",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.data_root = Path(__file__).resolve().parents[1] / "data"
        cls.facts = {
            fact["id"]: fact
            for fact in KnowledgeBase.from_file(DEFAULT_AUDIT_KNOWLEDGE).all()
        }
        cls.demo_cases = _load_cases(cls.data_root / "audit_cases.jsonl")
        cls.suites: dict[str, tuple[list[AuditRequest], dict]] = {}
        for name, (cases_file, gold_file) in cls.SUITES.items():
            cls.suites[name] = (
                _load_cases(cls.data_root / cases_file),
                json.loads((cls.data_root / gold_file).read_text(encoding="utf-8")),
            )

    def _labels(self, name: str) -> dict[str, dict]:
        _, gold = self.suites[name]
        labels = {item["id"]: item for item in gold["cases"]}
        self.assertEqual(len(labels), len(gold["cases"]), "gold IDs must be unique")
        return labels

    def test_each_suite_is_large_enough_to_be_meaningful(self) -> None:
        for name, (cases, _) in self.suites.items():
            with self.subTest(suite=name):
                self.assertGreaterEqual(len(cases), self.MINIMUM_CASES_PER_SUITE)

    def test_each_suite_labels_every_case_exactly_once(self) -> None:
        for name, (cases, _) in self.suites.items():
            with self.subTest(suite=name):
                labels = self._labels(name)
                self.assertEqual(len({case.id for case in cases}), len(cases))
                self.assertEqual({case.id for case in cases}, set(labels))

    def test_suites_do_not_overlap_each_other_or_the_demo_set(self) -> None:
        ids = {name: {case.id for case in cases}
               for name, (cases, _) in self.suites.items()}
        copies = {name: {case.original_copy for case in cases}
                  for name, (cases, _) in self.suites.items()}
        demo_ids = {case.id for case in self.demo_cases}
        demo_copies = {case.original_copy for case in self.demo_cases}

        for name, case_ids in ids.items():
            self.assertTrue(case_ids.isdisjoint(demo_ids), f"{name} reuses a demo id")
            self.assertTrue(copies[name].isdisjoint(demo_copies), f"{name} reuses demo copy")
        self.assertTrue(ids["dev"].isdisjoint(ids["test"]), "suites share an id")
        self.assertTrue(copies["dev"].isdisjoint(copies["test"]), "suites share copy")

    def test_locales_are_balanced_within_each_suite(self) -> None:
        for name, (cases, _) in self.suites.items():
            with self.subTest(suite=name):
                english = sum(case.locale == "en-US" for case in cases)
                chinese = sum(case.locale == "zh-CN" for case in cases)
                self.assertEqual(english + chinese, len(cases))
                self.assertEqual(english, chinese, "each suite must be bilingual")

    def test_labels_are_well_formed_and_self_consistent(self) -> None:
        for name, (cases, _) in self.suites.items():
            labels = self._labels(name)
            for case in cases:
                label = labels[case.id]
                with self.subTest(suite=name, case_id=case.id):
                    self.assertIs(type(label["abstain_expected"]), bool)
                    self.assertTrue(label["risk_tags"])
                    self.assertTrue(all(
                        isinstance(tag, str) and tag.strip()
                        for tag in label["risk_tags"]
                    ))
                    for phrase in label["must_remove"]:
                        self.assertTrue(isinstance(phrase, str) and phrase.strip())
                        self.assertIn(
                            phrase.casefold(), case.original_copy.casefold(),
                            "must_remove must be a literal fragment of the copy",
                        )

    def test_gold_sources_match_the_case_product_and_feature(self) -> None:
        for name, (cases, _) in self.suites.items():
            labels = self._labels(name)
            for case in cases:
                label = labels[case.id]
                matching = [
                    fact for fact in self.facts.values()
                    if fact["product_id"] == case.product_id
                    and fact["feature"] == case.feature
                ]
                with self.subTest(suite=name, case_id=case.id):
                    for fact_id in label["gold_fact_ids"]:
                        self.assertIn(fact_id, self.facts)
                        self.assertEqual(self.facts[fact_id]["product_id"], case.product_id)
                        self.assertEqual(self.facts[fact_id]["feature"], case.feature)
                    if label["abstain_expected"]:
                        self.assertFalse(matching, "an abstaining case must have no source")
                        self.assertEqual(label["gold_fact_ids"], [])
                        self.assertEqual(label["must_remove"], [])
                    else:
                        self.assertTrue(matching, "a reviewable case needs a source")
                        self.assertTrue(label["gold_fact_ids"])

    def test_every_locale_covers_the_required_risk_scenarios(self) -> None:
        for name, (cases, _) in self.suites.items():
            labels = self._labels(name)
            by_locale: dict[str, set[str]] = {}
            for case in cases:
                by_locale.setdefault(case.locale, set()).update(
                    labels[case.id]["risk_tags"]
                )
            with self.subTest(suite=name):
                self.assertEqual(set(by_locale), {"en-US", "zh-CN"})
                for locale, tags in by_locale.items():
                    with self.subTest(suite=name, locale=locale):
                        self.assertTrue(self.REQUIRED_RISK_TAGS <= tags,
                                        f"missing {self.REQUIRED_RISK_TAGS - tags}")

    def test_suites_together_cover_the_whole_catalog(self) -> None:
        """Every source card should be exercised by at least one labelled case."""
        covered = {
            fact_id
            for name in self.suites
            for fact_id in (
                f for label in self._labels(name).values()
                for f in label["gold_fact_ids"]
            )
        }
        self.assertEqual(covered, set(self.facts))

    def test_scope_declares_the_limits_of_the_labels(self) -> None:
        for name, (_, gold) in self.suites.items():
            with self.subTest(suite=name):
                self.assertIn("not a blinded", gold["scope"])
                self.assertIn("semantic factual accuracy", gold["scope"])


if __name__ == "__main__":
    unittest.main()
