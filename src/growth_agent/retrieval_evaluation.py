"""Compare retrieval strategies against labelled gold fact IDs.

This measures ranking quality only: did the right evidence card surface, and
how high. It deliberately reports no claim accuracy and no copy quality, so a
retrieval improvement can never be presented as a correctness improvement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import AuditRequest
from .knowledge import KnowledgeBase
from .retrieval import build_retriever

DEFAULT_TOP_K = 5


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _load_requests(path: str | Path) -> list[AuditRequest]:
    records = [
        AuditRequest.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len({item.id for item in records}) != len(records):
        raise ValueError("Retrieval case IDs must be unique")
    return records


def evaluate_retrieval(
    strategy: str,
    cases_path: str | Path,
    gold_path: str | Path,
    knowledge_path: str | Path,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Rank every case's feature query and score it against its gold fact IDs."""
    requests = _load_requests(cases_path)
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    labels = {case["id"]: case for case in gold["cases"]}
    if {item.id for item in requests} != set(labels):
        raise ValueError("Retrieval evaluation inputs must match the gold case IDs")

    loaded = KnowledgeBase.from_file(knowledge_path)
    facts = loaded.all()
    catalog = KnowledgeBase(facts, retriever=build_retriever(facts, strategy))

    gold_recalled = gold_total = 0
    reciprocal_sum = 0.0
    ranked_cases = 0
    top1_hits = 0
    leakage = 0
    leaked_cases = 0
    expected_absent = 0
    absent_false_positive = 0
    per_case: list[dict[str, Any]] = []

    for request in requests:
        label = labels[request.id]
        expected = list(label["gold_fact_ids"])
        # The feature itself is the retrieval intent here: it is what the
        # application scopes a real audit to, so a retriever is judged on how
        # well it ranks within that scope.
        results = catalog.search(
            request.feature, top_k=top_k, product_id=request.product_id
        )
        returned = [card["id"] for card in results]
        leaked = [
            card["id"] for card in results
            if card.get("product_id") != request.product_id
        ]
        leakage += len(leaked)
        leaked_cases += int(bool(leaked))

        if not expected:
            # No card supports this case: returning evidence here is a
            # false positive that would defeat the abstention guard.
            expected_absent += 1
            absent_false_positive += int(bool(results))
            per_case.append({
                "id": request.id, "expected_abstention": True,
                "returned": returned, "leaked": leaked,
            })
            continue

        hits = [fact_id for fact_id in returned if fact_id in expected]
        gold_recalled += len(hits)
        gold_total += len(expected)
        ranked_cases += 1
        first_rank = (returned.index(hits[0]) + 1) if hits else None
        if first_rank is not None:
            reciprocal_sum += 1.0 / first_rank
            top1_hits += int(first_rank == 1)
        per_case.append({
            "id": request.id, "expected_abstention": False,
            "expected": expected, "returned": returned, "hits": hits,
            "first_hit_rank": first_rank, "leaked": leaked,
        })

    return {
        "scope": (
            "Retrieval ranking quality only. Measured against curated gold fact "
            "IDs; it does not establish claim accuracy, semantic support or copy "
            "quality. Product leakage must stay at zero."
        ),
        "strategy": strategy,
        "top_k": top_k,
        "cases": len(requests),
        "recall_at_k": _ratio(gold_recalled, gold_total),
        "mean_reciprocal_rank": (
            round(reciprocal_sum / ranked_cases, 6) if ranked_cases else None
        ),
        "hit_at_1": _ratio(top1_hits, ranked_cases),
        "product_leakage": _ratio(leaked_cases, len(requests)),
        "absent_evidence_false_positive": _ratio(
            absent_false_positive, expected_absent
        ),
        "per_case": per_case,
    }


def _load_queries(path: str | Path) -> list[dict[str, Any]]:
    required = {"id", "query", "locale", "product_id", "expected_feature"}
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        missing = required - set(record)
        if missing:
            raise ValueError(
                f"Query {record.get('id')!r} is missing fields: {sorted(missing)}"
            )
        records.append(record)
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("Retrieval query IDs must be unique")
    return records


def evaluate_feature_retrieval(
    strategy: str,
    queries_path: str | Path,
    knowledge_path: str | Path,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Score topic-level retrieval for naturally written queries.

    Queries are phrased the way a person would ask them, not as the catalog's
    own slug, so a retriever cannot pass by reproducing its index structure.
    Results are reported per locale because a Chinese query aimed at English
    evidence is precisely the case a lexical retriever cannot serve at all.
    """
    queries = _load_queries(queries_path)
    loaded = KnowledgeBase.from_file(knowledge_path)
    facts = loaded.all()
    catalog = KnowledgeBase(facts, retriever=build_retriever(facts, strategy))

    known_features = {fact["feature"] for fact in facts}
    unknown = sorted(
        {item["expected_feature"] for item in queries} - known_features
    )
    if unknown:
        raise ValueError(
            f"Queries expect features that are absent from the catalog: {unknown}"
        )

    buckets: dict[str, dict[str, float]] = {}
    leakage = 0
    per_case: list[dict[str, Any]] = []

    # One batched call instead of one per query: the reranked strategy can then
    # run its cross-encoder over all candidates at once.
    all_results = catalog.search_many(
        [(item["query"], item["product_id"]) for item in queries], top_k=top_k
    )

    for item, results in zip(queries, all_results):
        features = [card["feature"] for card in results]
        expected = item["expected_feature"]
        rank = features.index(expected) + 1 if expected in features else None
        leaked = any(
            card.get("product_id") != item["product_id"] for card in results
        )
        leakage += int(leaked)

        bucket = buckets.setdefault(
            item["locale"],
            {"cases": 0, "hits": 0, "top1": 0, "reciprocal": 0.0},
        )
        bucket["cases"] += 1
        if rank is not None:
            bucket["hits"] += 1
            bucket["top1"] += int(rank == 1)
            bucket["reciprocal"] += 1.0 / rank

        per_case.append({
            "id": item["id"], "locale": item["locale"], "query": item["query"],
            "expected_feature": expected, "rank": rank,
            "top_features": features[:3], "leaked": leaked,
        })

    def summarise(bucket: dict[str, float]) -> dict[str, Any]:
        cases = int(bucket["cases"])
        return {
            "cases": cases,
            "hit_at_k": _ratio(int(bucket["hits"]), cases),
            "top1_accuracy": _ratio(int(bucket["top1"]), cases),
            "mean_reciprocal_rank": (
                round(bucket["reciprocal"] / cases, 6) if cases else None
            ),
        }

    overall = {
        "cases": 0, "hits": 0, "top1": 0,
        "reciprocal": sum(bucket["reciprocal"] for bucket in buckets.values()),
    }
    for bucket in buckets.values():
        overall["cases"] += bucket["cases"]
        overall["hits"] += bucket["hits"]
        overall["top1"] += bucket["top1"]

    return {
        "scope": (
            "Topic-level retrieval for naturally written queries. Measures "
            "whether the correct documented topic surfaced, not whether any "
            "claim is factually supported. Product leakage must stay at zero."
        ),
        "strategy": strategy,
        "top_k": top_k,
        "catalog_cards": len(facts),
        "catalog_features": len(known_features),
        "overall": summarise(overall),
        "by_locale": {
            locale: summarise(bucket) for locale, bucket in sorted(buckets.items())
        },
        "product_leakage": _ratio(leakage, len(queries)),
        "per_case": per_case,
    }
