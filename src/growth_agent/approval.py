"""Local human approval records bound to immutable audit content and sources."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditBundle
from .media import ApprovedCopy, copy_digest


def _audit(path: Path) -> tuple[AuditBundle, str]:
    raw = path.read_bytes()
    bundle = AuditBundle.model_validate(json.loads(raw))
    if bundle.status != "pending_review" or bundle.proposal is None or bundle.revised_checks.get("passed") is not True:
        raise ValueError("Only a valid pending_review audit can be approved")
    return bundle, hashlib.sha256(raw).hexdigest()


def audit_digest(run_dir: Path) -> str:
    """Return the digest of the exact source-and-copy snapshot to review."""
    return _audit(run_dir / "audit_bundle.json")[1]


def approve_audit(
    run_dir: Path, *, reviewer: str, expected_version: str | None = None, notes: str = "",
    expected_audit_digest: str | None = None,
) -> Path:
    """Record an explicit local editor decision, not authenticated identity proof."""
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 100:
        raise ValueError("reviewer must contain 1 to 100 characters")
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValueError("notes must be a string of at most 2000 characters")
    bundle, bundle_digest = _audit(run_dir / "audit_bundle.json")
    copy_version = copy_digest(bundle.proposal.revised_copy)
    if expected_version is not None and expected_version != copy_version:
        raise ValueError("The proposed copy changed; review and confirm the new version")
    if expected_audit_digest is not None and expected_audit_digest != bundle_digest:
        raise ValueError("The audit or its sources changed; review and confirm the new snapshot")
    record = {
        "run_id": bundle.run_id, "copy_version": copy_version,
        "audit_digest": bundle_digest, "reviewer": reviewer.strip(),
        "confirmed": True, "notes": notes,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "decision_scope": "local human editorial approval; identity is not authenticated",
    }
    target = run_dir / "audit_approval.json"
    if target.exists():
        load_approved_copy(run_dir)
        return target
    with target.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


def load_approved_copy(run_dir: Path) -> ApprovedCopy:
    bundle, bundle_digest = _audit(run_dir / "audit_bundle.json")
    record = json.loads((run_dir / "audit_approval.json").read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("audit_digest") != bundle_digest or record.get("run_id") != bundle.run_id:
        raise ValueError("Audit content or evidence changed after approval")
    return ApprovedCopy(
        run_id=bundle.run_id, copy_version=record.get("copy_version"),
        text=bundle.proposal.revised_copy, reviewer=record.get("reviewer"),
        confirmed=record.get("confirmed"),
    )
