"""Keep failed runs in evaluation and protect reports from accidental overwrite."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from growth_agent.cli import _audit_eval_execute
from growth_agent.resources import DEFAULT_AUDIT_KNOWLEDGE, DEFAULT_AUDIT_RULES


class AuditBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_failure_does_not_skip_later_case_or_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases.jsonl"
            base = {"product_id": "demo", "product_name": "Demo", "locale": "en-US", "feature": "missing", "channel": "social_post", "original_copy": "Claim.", "audience": "creators"}
            cases.write_text("\n".join(json.dumps({**base, "id": name}) for name in ("failed", "later")), encoding="utf-8")
            gold = root / "gold.json"
            gold.write_text(json.dumps({"cases": [{"id": name, "abstain_expected": True, "must_remove": []} for name in ("failed", "later")]}), encoding="utf-8")
            seen = []

            async def runner(request, **kwargs):
                seen.append(request.id)
                if request.id == "failed":
                    raise RuntimeError("sensitive-provider-message")
                target = kwargs["output_root"] / "later"
                target.mkdir()
                (target / "audit_bundle.json").write_text(json.dumps({"run_id": "later", "request": request.model_dump(), "status": "insufficient_evidence", "proposal": None, "facts": []}), encoding="utf-8")
                return SimpleNamespace(status="insufficient_evidence"), target

            args = argparse.Namespace(cases=cases, gold=gold, output=root / "output", mode="qwen", strategy="agent", suite="dev", knowledge=DEFAULT_AUDIT_KNOWLEDGE, rules=DEFAULT_AUDIT_RULES)
            with patch("growth_agent.cli.run_audit", side_effect=runner), contextlib.redirect_stdout(io.StringIO()):
                result = await _audit_eval_execute(args)
                with self.assertRaises(FileExistsError):
                    await _audit_eval_execute(args)
            report = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(seen, ["failed", "later"])
            self.assertEqual(result, 2)
            self.assertEqual(report["workflow_success"]["denominator"], 2)
            self.assertEqual(report["workflow_success"]["numerator"], 1)
            self.assertNotIn("sensitive-provider-message", (args.output / "report.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
