"""Development regression metrics for saved product-claim audit bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def evaluate_audits(bundle_paths: Iterable[Path], gold_path: Path) -> dict:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in gold["cases"]}
    if len(cases) != len(gold["cases"]):
        raise ValueError("Audit gold IDs must be unique")
    records = []
    seen = set()
    correct_outcome = correct_abstention = abstention_total = 0
    removed = removal_total = 0
    citations_valid = citations_total = 0
    for path in bundle_paths:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        case_id = bundle["request"]["id"]
        if case_id not in cases or case_id in seen:
            raise ValueError(f"Unknown or duplicate audit case: {case_id}")
        seen.add(case_id)
        case = cases[case_id]
        proposal = bundle.get("proposal")
        should_abstain = case["abstain_expected"]
        status = bundle.get("status")
        abstained = status == "insufficient_evidence" and proposal is None
        passed = (
            abstained if should_abstain else
            status == "pending_review" and isinstance(proposal, dict)
            and bundle.get("revised_checks", {}).get("passed") is True
        )
        correct_outcome += int(passed)
        if should_abstain:
            abstention_total += 1
            correct_abstention += int(abstained)
        revised = proposal.get("revised_copy", "") if isinstance(proposal, dict) else ""
        failures = []
        for phrase in case["must_remove"]:
            removal_total += 1
            if passed and phrase.casefold() not in revised.casefold():
                removed += 1
            else:
                failures.append(phrase)
        fact_ids = {fact.get("id") for fact in bundle.get("facts", []) if isinstance(fact, dict)}
        for citation in proposal.get("citations", []) if isinstance(proposal, dict) else []:
            citations_total += 1
            citations_valid += int(
                citation.get("fact_id") in fact_ids
                and isinstance(citation.get("quote"), str)
                and citation["quote"].casefold() in revised.casefold()
            )
        records.append({
            "id": case_id, "status": status, "expected_abstention": should_abstain,
            "workflow_success": passed, "remaining_required_removals": failures,
            "run_id": bundle.get("run_id"),
        })
    if seen != set(cases):
        raise ValueError(f"Missing audit cases: {sorted(set(cases) - seen)}")
    return {
        "scope": gold.get("scope", "curated workflow regression; no semantic fact or language quality judgment"),
        "semantic_support_measured": False,
        "cases": len(records),
        "workflow_success": _ratio(correct_outcome, len(records)),
        "correct_abstention": _ratio(correct_abstention, abstention_total),
        "required_risk_phrases_removed": _ratio(removed, removal_total),
        "citation_structure": _ratio(citations_valid, citations_total),
        "per_case": records,
    }
