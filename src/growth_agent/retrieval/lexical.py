"""Lexical baseline: explainable weighted word overlap.

This is the original retrieval behaviour, kept deliberately unchanged so that
every later strategy is measured *against* it instead of replacing it. Without
a fixed baseline there is no way to claim that a hybrid retriever improved
anything.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .text import normalize, terms

FEATURE_WEIGHT = 5
KEYWORD_WEIGHT = 3
STATEMENT_WEIGHT = 1
PHRASE_BONUS = 5


class LexicalRetriever:
    """Rank cards by weighted term overlap.

    It is deliberately not a semantic or web search service: a query with no
    useful overlapping term returns nothing.
    """

    name = "lexical"

    def __init__(self, facts: list[dict[str, Any]]):
        self._facts = facts

    def search(
        self, query: str, top_k: int = 5, product_id: str = ""
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or not query.strip():
            return []
        query_terms = terms(query)
        if not query_terms:
            return []
        normalized_query = normalize(query)
        ranked: list[tuple[int, str, dict[str, Any]]] = []

        for fact in self._facts:
            if product_id and fact.get("product_id") != product_id:
                continue
            feature_terms = terms(fact["feature"] + " " + fact["id"])
            keyword_terms = terms(" ".join(fact["keywords"]))
            statement_terms = terms(fact["statement"])
            localized = fact.get("localized_statement", {})
            if isinstance(localized, dict):
                statement_terms |= terms(" ".join(map(str, localized.values())))

            score = FEATURE_WEIGHT * len(query_terms & feature_terms)
            score += KEYWORD_WEIGHT * len(query_terms & keyword_terms)
            score += STATEMENT_WEIGHT * len(query_terms & statement_terms)
            matched_terms = query_terms & (
                feature_terms | keyword_terms | statement_terms
            )
            for phrase in fact["keywords"]:
                normalized_phrase = normalize(phrase).strip()
                if len(normalized_phrase) >= 3 and normalized_phrase in normalized_query:
                    score += PHRASE_BONUS
                    matched_terms |= terms(phrase)

            if score > 0:
                result = deepcopy(fact)
                result["retrieval_score"] = score
                result["matched_terms"] = sorted(matched_terms)
                ranked.append((score, fact["id"], result))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [result for _, _, result in ranked[:top_k]]
