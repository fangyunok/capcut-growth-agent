"""Protect approval against changed copy, sources and unsuccessful model output."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from growth_agent.approval import approve_audit, audit_digest, load_approved_copy
from growth_agent.audit import AuditBundle, AuditProposal, AuditRequest
from growth_agent.media import copy_digest


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        request = AuditRequest(
            id="case", product_id="demo", product_name="Demo", locale="en-US",
            feature="feature", channel="social_post", original_copy="Original.", audience="creators",
        )
        self.bundle = AuditBundle(
            run_id="audit-case", request=request, status="pending_review", model="scripted-test",
            proposal=AuditProposal(revised_copy="Reviewed copy.", issues=[], citations=[{"quote": "Reviewed copy.", "fact_id": "f1"}]),
            facts=[{"id": "f1", "source_quote": "Source passage"}], revised_checks={"passed": True},
        )
        self._write()

    def _write(self) -> None:
        (self.root / "audit_bundle.json").write_text(self.bundle.model_dump_json(), encoding="utf-8")

    def test_explicit_approval_returns_exact_copy_and_is_idempotent(self) -> None:
        path = approve_audit(self.root, reviewer="Editor", expected_version=copy_digest("Reviewed copy."))
        approved = load_approved_copy(self.root)
        self.assertEqual(approved.text, "Reviewed copy.")
        self.assertEqual(approved.reviewer, "Editor")
        before = path.read_bytes()
        self.assertEqual(approve_audit(self.root, reviewer="Editor"), path)
        self.assertEqual(before, path.read_bytes())

    def test_missing_or_failed_review_cannot_be_approved(self) -> None:
        self.bundle.status = "needs_revision"
        self._write()
        with self.assertRaises(ValueError):
            approve_audit(self.root, reviewer="Editor")
        self.assertFalse((self.root / "audit_approval.json").exists())

    def test_stale_copy_hash_rejected_before_record_is_written(self) -> None:
        with self.assertRaises(ValueError):
            approve_audit(self.root, reviewer="Editor", expected_version=copy_digest("Old copy."))
        self.assertFalse((self.root / "audit_approval.json").exists())

    def test_source_mutation_after_approval_invalidates_media_input(self) -> None:
        approve_audit(self.root, reviewer="Editor")
        self.bundle.facts[0]["source_quote"] = "Different passage"
        self._write()
        with self.assertRaises(ValueError):
            load_approved_copy(self.root)

    def test_source_mutation_between_review_and_approval_is_rejected(self) -> None:
        reviewed_digest = audit_digest(self.root)
        self.bundle.facts[0]["source_quote"] = "New unseen passage"
        self._write()
        with self.assertRaises(ValueError):
            approve_audit(self.root, reviewer="Editor", expected_version=copy_digest("Reviewed copy."),
                          expected_audit_digest=reviewed_digest)
        self.assertFalse((self.root / "audit_approval.json").exists())

    def test_changed_text_and_forged_confirmation_are_rejected(self) -> None:
        approve_audit(self.root, reviewer="Editor")
        path = self.root / "audit_approval.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["confirmed"] = "true"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_approved_copy(self.root)

    def test_blank_reviewer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            approve_audit(self.root, reviewer="   ")


if __name__ == "__main__":
    unittest.main()
