"""Check evaluation denominators and the boundary of deterministic claims."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from growth_agent.cli import main
from growth_agent.evaluation import evaluate_bundles
from growth_agent.schemas import Brief


ROOT = Path(__file__).resolve().parents[1]
EVAL_BRIEFS = ROOT / "data" / "eval_briefs.jsonl"
EVAL_GOLD = ROOT / "data" / "eval_gold.json"
DEMO_BRIEFS = ROOT / "data" / "demo_briefs.jsonl"


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _draft(text: str, claim_id: str, *, valid_link: bool = True) -> dict:
    return {
        "title": text,
        "meta_description": "A useful description",
        "h1": "A practical guide",
        "intro": "An introduction",
        "body": ["A supported sentence."],
        "social_posts": ["Post one", "Post two"],
        "claim_uses": [{
            "fact_id": claim_id,
            "sentence": "A supported sentence.",
            "location": "body[0]" if valid_link else "body[1]",
        }],
    }


class EvaluationTests(unittest.TestCase):
    def test_held_out_bilingual_set_has_expected_scope(self) -> None:
        briefs = [
            Brief.model_validate_json(line) for line in EVAL_BRIEFS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        gold = json.loads(EVAL_GOLD.read_text(encoding="utf-8"))
        demo_ids = {
            json.loads(line)["id"] for line in DEMO_BRIEFS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        self.assertEqual(len(briefs), 24)
        self.assertEqual(len({brief.id for brief in briefs}), 24)
        self.assertTrue({brief.id for brief in briefs}.isdisjoint(demo_ids))
        self.assertEqual({brief.id for brief in briefs}, {
            case["brief_id"] for case in gold["cases"]
        })
        self.assertEqual({brief.id.removesuffix("-en").removesuffix("-es") for brief in briefs}, {
            f"eval-{index:02}" for index in range(1, 13)
        })
        for index in range(1, 13):
            pair = [brief for brief in briefs if brief.id.startswith(f"eval-{index:02}-")]
            self.assertEqual({brief.locale for brief in pair}, {"en-US", "es-ES"})
            self.assertEqual(len({brief.feature for brief in pair}), 1)
        self.assertEqual(len(gold["fact_ids"]), 5)
        self.assertEqual(sum(case["abstain_expected"] for case in gold["cases"]), 6)
        self.assertEqual({fact_id for case in gold["cases"] for fact_id in case["required_fact_ids"]},
                         set(gold["fact_ids"]))

    def test_metrics_report_numerators_and_denominators(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            gold_path = _write_json(root / "gold.json", {
                "fact_ids": ["fact-a", "fact-b"],
                "cases": [
                    {"brief_id": "positive-a", "required_fact_ids": ["fact-a"],
                     "abstain_expected": False, "forbidden_claims": ["guaranteed traffic"]},
                    {"brief_id": "positive-b", "required_fact_ids": ["fact-b"],
                     "abstain_expected": False, "forbidden_claims": ["100 % accurate"]},
                    {"brief_id": "negative", "required_fact_ids": [],
                     "abstain_expected": True, "forbidden_claims": ["always free"]},
                ],
            })
            good = _write_json(root / "good.json", {
                "run_id": "good", "brief": {"id": "positive-a", "intent": "guaranteed traffic"},
                "status": "pending_review", "facts": [{"id": "fact-a", "source_quote": "guaranteed traffic"}],
                "draft": _draft("Normal title", "fact-a"), "checks": {"passed": True},
                "trace": [{"duration_ms": 2}, {"step": "tool_call"}, {"duration_ms": 8}],
            })
            bad = _write_json(root / "bad.json", {
                "run_id": "bad", "brief": {"id": "positive-b"},
                "status": "needs_revision", "facts": [{"id": "fact-b"}],
                "draft": _draft("100% ACCURATE feature", "fact-b", valid_link=False),
                "checks": {"passed": False}, "trace": [{"duration_ms": 20}],
            })
            abstained = _write_json(root / "abstained.json", {
                "run_id": "abstained", "brief": {"id": "negative", "intent": "always free"},
                "status": "insufficient_evidence", "facts": [], "draft": None,
                "checks": {"passed": False}, "trace": [{"duration_ms": 30}],
            })
            report = evaluate_bundles([good, bad, abstained], gold_path)

        self.assertEqual(report["bundles_evaluated"], 3)
        self.assertEqual(report["evidence_id_coverage"]["numerator"], 1)
        self.assertEqual(report["evidence_id_coverage"]["denominator"], 2)
        self.assertEqual(report["correct_abstention"]["numerator"], 1)
        self.assertEqual(report["correct_abstention"]["denominator"], 1)
        self.assertEqual(report["forbidden_phrase_hits"]["numerator"], 1)
        self.assertEqual(report["forbidden_phrase_hits"]["denominator"], 2)
        self.assertEqual(report["forbidden_phrase_hits"]["drafts_checked"], 2)
        self.assertEqual(report["task_success"]["numerator"], 2)
        self.assertEqual(report["task_success"]["denominator"], 3)
        self.assertEqual(report["traced_step_latency_ms"]["p50_ms"], 20)
        self.assertEqual(report["traced_step_latency_ms"]["p95_ms"], 30)
        self.assertEqual(report["per_bundle"][1]["missing_fact_ids"], ["fact-b"])
        self.assertIn("no semantic fact judgment", report["scope"])

    def test_unknown_brief_fails_instead_of_changing_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            gold_path = _write_json(root / "gold.json", {
                "fact_ids": ["fact-a"],
                "cases": [{"brief_id": "known", "required_fact_ids": ["fact-a"],
                           "abstain_expected": False, "forbidden_claims": []}],
            })
            bundle_path = _write_json(root / "bundle.json", {"brief": {"id": "unknown"}})
            with self.assertRaisesRegex(ValueError, "no gold case"):
                evaluate_bundles([bundle_path], gold_path)

    def test_invalid_trace_is_excluded_with_measured_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            gold_path = _write_json(root / "gold.json", {
                "fact_ids": ["fact-a"],
                "cases": [{"brief_id": "known", "required_fact_ids": ["fact-a"],
                           "abstain_expected": False, "forbidden_claims": []}],
            })
            bundle_path = _write_json(root / "bundle.json", {
                "brief": {"id": "known"}, "status": "pending_review",
                "facts": [{"id": "fact-a"}], "draft": _draft("Title", "fact-a"),
                "checks": {"passed": True}, "trace": [{"duration_ms": -1}],
            })
            report = evaluate_bundles([root], gold_path)
        self.assertEqual(report["traced_step_latency_ms"]["measured_bundles"], 0)
        self.assertEqual(report["traced_step_latency_ms"]["total_bundles"], 1)
        self.assertIsNone(report["traced_step_latency_ms"]["p95_ms"])

    def test_evaluate_cli_reads_run_root_and_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            gold_path = _write_json(root / "gold.json", {
                "fact_ids": ["fact-a"],
                "cases": [{"brief_id": "known", "required_fact_ids": ["fact-a"],
                           "abstain_expected": False, "forbidden_claims": []}],
            })
            run_dir = root / "runs" / "run-a"
            run_dir.mkdir(parents=True)
            _write_json(run_dir / "bundle.json", {
                "brief": {"id": "known"}, "status": "pending_review",
                "facts": [{"id": "fact-a"}], "draft": _draft("Title", "fact-a"),
                "checks": {"passed": True}, "trace": [{"duration_ms": 9}],
            })
            output = root / "report.json"
            with redirect_stdout(io.StringIO()):
                code = main(["evaluate", str(root / "runs"), str(run_dir / "bundle.json"),
                             "--gold", str(gold_path), "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(report["bundles_evaluated"], 1)
        self.assertEqual(report["task_success"]["numerator"], 1)


if __name__ == "__main__":
    unittest.main()
