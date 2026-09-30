"""Deterministic evaluation of saved run bundles against a frozen brief gold set.

These metrics assess workflow behavior and evidence-ID bookkeeping only. In
particular, an ID link is not a judgment that the linked sentence is entailed
by the source; that requires a separate, blinded human review.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from .schemas import Draft


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _gold_cases(path: Path) -> dict[str, dict[str, Any]]:
    gold = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(gold, dict) or not isinstance(gold.get("cases"), list):
        raise ValueError("Gold file must be an object containing a cases list")
    known_ids = gold.get("fact_ids")
    if not isinstance(known_ids, list) or not known_ids or not all(
        isinstance(fact_id, str) and fact_id for fact_id in known_ids
    ) or len(set(known_ids)) != len(known_ids):
        raise ValueError("Gold file must list unique, nonempty fact_ids")

    cases: dict[str, dict[str, Any]] = {}
    for index, case in enumerate(gold["cases"]):
        if not isinstance(case, dict):
            raise ValueError(f"Gold case {index} must be an object")
        brief_id = case.get("brief_id")
        required = case.get("required_fact_ids")
        forbidden = case.get("forbidden_claims")
        abstain = case.get("abstain_expected")
        if not isinstance(brief_id, str) or not brief_id or brief_id in cases:
            raise ValueError(f"Gold case {index} has a missing or duplicate brief_id")
        if not isinstance(required, list) or not all(
            isinstance(value, str) and value for value in required
        ) or len(set(required)) != len(required):
            raise ValueError(f"Gold case {brief_id} has invalid required_fact_ids")
        if not set(required).issubset(known_ids):
            raise ValueError(f"Gold case {brief_id} references an unknown fact ID")
        if not isinstance(forbidden, list) or not all(
            isinstance(value, str) and value.strip() for value in forbidden
        ) or len({value.casefold() for value in forbidden}) != len(forbidden):
            raise ValueError(f"Gold case {brief_id} has invalid forbidden_claims")
        if not isinstance(abstain, bool):
            raise ValueError(f"Gold case {brief_id} needs a boolean abstain_expected")
        if abstain and required:
            raise ValueError(f"Gold case {brief_id} cannot require facts and abstention")
        if not abstain and not required:
            raise ValueError(f"Gold case {brief_id} must require a fact ID")
        cases[brief_id] = case
    return cases


def _draft_passage(draft: dict[str, Any], location: str) -> str | None:
    if location in {"title", "meta_description", "h1", "intro"}:
        value = draft.get(location)
        return value if isinstance(value, str) else None
    match = re.fullmatch(r"(body|social_posts)\[(\d+)\]", location)
    if match is None:
        return None
    parts = draft.get(match.group(1))
    index = int(match.group(2))
    if not isinstance(parts, list) or index >= len(parts):
        return None
    value = parts[index]
    return value if isinstance(value, str) else None


def _linked_fact_ids(bundle: dict[str, Any]) -> set[str]:
    """Return IDs with a retrieved card and a locally consistent claim link."""
    draft = bundle.get("draft")
    if not isinstance(draft, dict):
        return set()
    facts = bundle.get("facts")
    retrieved = {
        fact["id"] for fact in facts if isinstance(fact, dict)
        and isinstance(fact.get("id"), str)
    } if isinstance(facts, list) else set()
    uses = draft.get("claim_uses")
    if not isinstance(uses, list):
        return set()

    linked: set[str] = set()
    for use in uses:
        if not isinstance(use, dict):
            continue
        fact_id = use.get("fact_id")
        sentence = use.get("sentence")
        location = use.get("location")
        if not isinstance(fact_id, str) or fact_id not in retrieved:
            continue
        if not isinstance(sentence, str) or not sentence.strip():
            continue
        if not isinstance(location, str):
            continue
        passage = _draft_passage(draft, location)
        if passage is not None and sentence.casefold() in passage.casefold():
            linked.add(fact_id)
    return linked


def _publishable_text(draft: dict[str, Any]) -> str:
    """Read only user-visible draft fields, never the brief or source quotes."""
    pieces: list[str] = []
    for key in ("title", "meta_description", "h1", "intro"):
        value = draft.get(key)
        if isinstance(value, str):
            pieces.append(value)
    for key in ("body", "social_posts"):
        value = draft.get(key)
        if isinstance(value, list):
            pieces.extend(item for item in value if isinstance(item, str))
    return "\n".join(pieces)


def _valid_draft(draft: Any) -> bool:
    if not isinstance(draft, dict):
        return False
    try:
        Draft.model_validate(draft)
    except ValidationError:
        return False
    return True


def _normalized_words(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    unaccented = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.sub(r"[\W_]+", " ", unaccented).split())


def _forbidden_hits(draft: dict[str, Any], phrases: list[str]) -> list[str]:
    text = f" {_normalized_words(_publishable_text(draft))} "
    return [
        phrase for phrase in phrases
        if f" {_normalized_words(phrase)} " in text
    ]


def _trace_duration_ms(bundle: dict[str, Any]) -> int | None:
    trace = bundle.get("trace")
    if not isinstance(trace, list) or not trace:
        return None
    if any(not isinstance(step, dict) for step in trace):
        return None
    durations = [step["duration_ms"] for step in trace if "duration_ms" in step]
    if not durations or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value < 0
        for value in durations
    ):
        return None
    return round(sum(durations))


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    return sorted(values)[math.ceil(percentile * len(values)) - 1]


def evaluate_bundles(
    bundle_paths: Iterable[str | Path], gold_path: str | Path
) -> dict[str, Any]:
    """Score bundle JSON files or run directories using deterministic proxies.

    Each provided bundle is one evaluation attempt. Repeated brief IDs are
    allowed for paired modes or reruns, and count again in every denominator.
    Unknown brief IDs, invalid gold, and unreadable bundles fail loudly rather
    than silently changing the denominator.
    """
    cases = _gold_cases(Path(gold_path))
    records: list[dict[str, Any]] = []
    latencies: list[int] = []
    covered_total = required_total = 0
    abstain_correct = abstain_total = 0
    forbidden_total = forbidden_hits = drafts_with_hits = draft_total = 0
    successes = 0

    for supplied_path in bundle_paths:
        path = Path(supplied_path)
        if path.is_dir():
            path = path / "bundle.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(bundle, dict) or not isinstance(bundle.get("brief"), dict):
            raise ValueError(f"Bundle {path} has no brief object")
        brief_id = bundle["brief"].get("id")
        if brief_id not in cases:
            raise ValueError(f"Bundle {path} has no gold case for brief ID {brief_id!r}")
        gold = cases[brief_id]
        draft = bundle.get("draft")
        has_draft = isinstance(draft, dict)
        required = gold["required_fact_ids"]
        linked = _linked_fact_ids(bundle)
        covered = sorted(set(required) & linked)
        covered_total += len(covered)
        required_total += len(required)

        should_abstain = gold["abstain_expected"]
        abstained = bundle.get("status") == "insufficient_evidence" and draft is None
        if should_abstain:
            abstain_total += 1
            abstain_correct += int(abstained)

        hits = _forbidden_hits(draft, gold["forbidden_claims"]) if has_draft else []
        if has_draft:
            draft_total += 1
            forbidden_total += len(gold["forbidden_claims"])
            forbidden_hits += len(hits)
            drafts_with_hits += int(bool(hits))

        checks = bundle.get("checks")
        draft_completed = (
            bundle.get("status") == "pending_review"
            and _valid_draft(draft)
            and isinstance(checks, dict)
            and checks.get("passed") is True
        )
        task_success = abstained if should_abstain else draft_completed
        successes += int(task_success)

        latency = _trace_duration_ms(bundle)
        if latency is not None:
            latencies.append(latency)
        records.append({
            "bundle_path": str(path),
            "brief_id": brief_id,
            "run_id": bundle.get("run_id"),
            "mode": bundle.get("generation", {}).get("mode")
            if isinstance(bundle.get("generation"), dict) else None,
            "status": bundle.get("status"),
            "evidence_id_coverage": _ratio(len(covered), len(required)),
            "covered_fact_ids": covered,
            "missing_fact_ids": sorted(set(required) - linked),
            "abstain_expected": should_abstain,
            "correct_abstention": abstained if should_abstain else None,
            "forbidden_phrase_hits": hits,
            "forbidden_phrase_checks": len(gold["forbidden_claims"]) if has_draft else 0,
            "task_success": task_success,
            "traced_step_latency_ms": latency,
        })

    return {
        "scope": "deterministic workflow and evidence-ID proxies; no semantic fact judgment",
        "bundles_evaluated": len(records),
        "evidence_id_coverage": _ratio(covered_total, required_total),
        "correct_abstention": _ratio(abstain_correct, abstain_total),
        "forbidden_phrase_hits": {
            **_ratio(forbidden_hits, forbidden_total),
            "drafts_with_hits": drafts_with_hits,
            "drafts_checked": draft_total,
        },
        "task_success": _ratio(successes, len(records)),
        "traced_step_latency_ms": {
            "measured_bundles": len(latencies),
            "total_bundles": len(records),
            "p50_ms": _nearest_rank(latencies, 0.50),
            "p95_ms": _nearest_rank(latencies, 0.95),
        },
        "definitions": {
            "evidence_id_coverage": "Required fact IDs with a retrieved card and a claim sentence present at its declared draft location / all required fact IDs. This does not establish semantic support.",
            "correct_abstention": "Expected-abstention bundles with status insufficient_evidence and no draft / all expected-abstention bundles.",
            "forbidden_phrase_hits": "Case-specific normalized phrase matches in publishable draft fields / phrases checked across generated drafts. Negation and paraphrases are not understood.",
            "task_success": "Bundles with the expected workflow outcome: valid pending_review draft with passing checks, or expected insufficient_evidence without a draft / all bundles. Human approval and content quality are excluded.",
            "traced_step_latency_ms": "Sum of trace steps that record durations; excludes uninstrumented events and orchestration. P50/P95 use nearest-rank percentiles. Bundles without valid timed steps are excluded and counted separately. Do not compare modes unless they time the same steps.",
        },
        "per_bundle": records,
    }
