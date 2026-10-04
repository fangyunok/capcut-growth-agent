"""Hybrid retrieval: fuse sparse and dense rankings, then optionally rerank.

Fusion uses Reciprocal Rank Fusion rather than a weighted sum of raw scores.
BM25 scores are unbounded and embedding similarities are bounded, so summing
them requires a normalisation constant that has no defensible value and would
silently change meaning whenever either component is swapped. RRF consumes only
rank positions, which makes it comparable across retrievers of different scales.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .bm25 import BM25Retriever
from .base import Retriever
from .vector import VectorRetriever

DEFAULT_RRF_K = 60
DEFAULT_CANDIDATES = 10


class HybridRetriever:
    """Fuse several retrievers with RRF and optionally rerank the result."""

    name = "hybrid"

    def __init__(
        self, facts: list[dict[str, Any]], retrievers: list[Retriever] | None = None,
        rrf_k: int = DEFAULT_RRF_K, candidates: int = DEFAULT_CANDIDATES,
        reranker: Any | None = None,
    ):
        self._facts = facts
        self._retrievers = (
            retrievers if retrievers is not None
            else [BM25Retriever(facts), VectorRetriever(facts)]
        )
        self._rrf_k = rrf_k
        self._candidates = candidates
        self._reranker = reranker
        components = "+".join(getattr(item, "name", "?") for item in self._retrievers)
        self.name = f"hybrid[{components}]"
        if reranker is not None:
            self.name += "+rerank"

    @property
    def components(self) -> list[str]:
        return [getattr(item, "name", "?") for item in self._retrievers]

    def search(
        self, query: str, top_k: int = 5, product_id: str = ""
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or not query.strip():
            return []

        fused: dict[str, float] = {}
        cards: dict[str, dict[str, Any]] = {}
        for retriever in self._retrievers:
            ranked = retriever.search(
                query, top_k=self._candidates, product_id=product_id
            )
            for rank, card in enumerate(ranked, start=1):
                fact_id = card["id"]
                # Rank-only fusion: a component that cannot score a card simply
                # does not contribute to it, instead of casting a zero vote.
                fused[fact_id] = fused.get(fact_id, 0.0) + 1.0 / (self._rrf_k + rank)
                cards.setdefault(fact_id, card)

        ordered = [cards[fact_id] for fact_id, _ in sorted(
            fused.items(), key=lambda item: (-item[1], item[0])
        )]
        if not ordered:
            return []

        rerank_scores: dict[str, float] = {}
        if self._reranker is not None:
            reranked = self._reranker.rerank(query, ordered)
            ordered = [card for card, _ in reranked]
            rerank_scores = {card["id"]: score for card, score in reranked}

        results: list[dict[str, Any]] = []
        for card in ordered[:top_k]:
            result = deepcopy(card)
            result["retrieval_score"] = round(rerank_scores.get(
                result["id"], fused.get(result["id"], 0.0)
            ), 6)
            results.append(result)
        return results
