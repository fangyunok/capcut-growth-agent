"""BM25 ranking over the fact catalog.

BM25 scores a term by how rare it is across the catalog and how often it
appears in one card, normalising for card length. It is the standard sparse
baseline that sits between plain word overlap and dense vectors: still fully
explainable, but no longer fooled by a long card that merely repeats words.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from typing import Any

from .text import index_terms

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

FIELD_REPEATS = {"feature": 5, "keyword": 3, "statement": 1}


class BM25Retriever:
    """Rank cards with Okapi BM25 over catalog-wide term statistics."""

    name = "bm25"

    def __init__(
        self, facts: list[dict[str, Any]],
        k1: float = DEFAULT_K1, b: float = DEFAULT_B,
    ):
        self._facts = facts
        self._k1 = k1
        self._b = b
        self._frequencies: dict[str, Counter] = {}
        self._lengths: dict[str, int] = {}
        self._build()

    def _document_terms(self, fact: dict[str, Any]) -> list[str]:
        """Build the token stream for one card.

        Field importance is expressed by repetition. A true BM25F would give
        each field its own length normalisation, but at this catalog size the
        added machinery changes neither the ranking nor the review effort, so
        the simpler field boost is used and documented rather than hidden.
        """
        parts: list[str] = []
        parts.extend([fact["feature"], fact["id"]] * FIELD_REPEATS["feature"])
        parts.extend(list(fact["keywords"]) * FIELD_REPEATS["keyword"])
        parts.extend([fact["statement"]] * FIELD_REPEATS["statement"])
        localized = fact.get("localized_statement", {})
        if isinstance(localized, dict):
            parts.extend(str(wording) for wording in localized.values())
        return index_terms(" ".join(parts))

    def _build(self) -> None:
        document_frequency: dict[str, int] = {}
        for fact in self._facts:
            tokens = self._document_terms(fact)
            fact_id = fact["id"]
            self._frequencies[fact_id] = Counter(tokens)
            self._lengths[fact_id] = len(tokens)
            for token in set(tokens):
                document_frequency[token] = document_frequency.get(token, 0) + 1

        total = len(self._facts)
        self._idf = {
            token: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequency.items()
        }
        lengths = list(self._lengths.values())
        self._average_length = (sum(lengths) / len(lengths)) if lengths else 0.0

    def search(
        self, query: str, top_k: int = 5, product_id: str = ""
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or not query.strip():
            return []
        query_terms = set(index_terms(query))
        if not query_terms:
            return []

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for fact in self._facts:
            if product_id and fact.get("product_id") != product_id:
                continue
            fact_id = fact["id"]
            frequencies = self._frequencies[fact_id]
            length = self._lengths[fact_id] or 1
            # An empty catalog would make the length ratio undefined; guard it
            # so a single-card catalog still ranks instead of raising.
            normalisation = (
                1 - self._b + self._b * length / self._average_length
                if self._average_length else 1.0
            )
            score = 0.0
            matched: set[str] = set()
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                score += (
                    self._idf.get(term, 0.0)
                    * frequency * (self._k1 + 1)
                    / (frequency + self._k1 * normalisation)
                )
                matched.add(term)
            if score > 0:
                result = deepcopy(fact)
                result["retrieval_score"] = round(score, 6)
                result["matched_terms"] = sorted(matched)
                scored.append((score, fact_id, result))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [result for _, _, result in scored[:top_k]]
