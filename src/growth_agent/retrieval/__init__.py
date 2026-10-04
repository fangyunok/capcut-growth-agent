"""Pluggable retrieval strategies for the fact-card catalog.

``GROWTH_RETRIEVAL`` selects the strategy at runtime, so the same application,
the same MCP tool signature and the same output shape can be evaluated under
each retriever. That is what makes a comparison honest: only the ranking model
changes between reported numbers.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .base import Retriever
from .bm25 import BM25Retriever
from .hybrid import HybridRetriever
from .lexical import LexicalRetriever
from .vector import VectorRetriever

STRATEGIES = ("lexical", "bm25", "vector", "hybrid", "hybrid-rerank")
DEFAULT_STRATEGY = "lexical"


def _lexical(facts: list[dict[str, Any]]) -> Retriever:
    return LexicalRetriever(facts)


def _bm25(facts: list[dict[str, Any]]) -> Retriever:
    return BM25Retriever(facts)


def _vector(facts: list[dict[str, Any]]) -> Retriever:
    return VectorRetriever(facts)


def _hybrid(facts: list[dict[str, Any]]) -> Retriever:
    return HybridRetriever(facts)


def _hybrid_rerank(facts: list[dict[str, Any]]) -> Retriever:
    # Imported here so the reranker dependency is only touched when selected.
    from .rerank import CrossEncoderReranker

    return HybridRetriever(facts, reranker=CrossEncoderReranker())


_FACTORIES: dict[str, Callable[[list[dict[str, Any]]], Retriever]] = {
    "lexical": _lexical,
    "bm25": _bm25,
    "vector": _vector,
    "hybrid": _hybrid,
    "hybrid-rerank": _hybrid_rerank,
}


def default_strategy() -> str:
    """Read the configured strategy, falling back to the lexical baseline."""
    configured = os.getenv("GROWTH_RETRIEVAL", DEFAULT_STRATEGY).strip().lower()
    return configured or DEFAULT_STRATEGY


def build_retriever(
    facts: list[dict[str, Any]], strategy: str | None = None
) -> Retriever:
    """Instantiate one strategy over a loaded fact catalog."""
    name = (strategy or default_strategy()).strip().lower()
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown retrieval strategy {name!r}; "
            f"available: {', '.join(STRATEGIES)}"
        )
    return factory(facts)


__all__ = [
    "Retriever",
    "LexicalRetriever",
    "BM25Retriever",
    "VectorRetriever",
    "HybridRetriever",
    "build_retriever",
    "default_strategy",
    "STRATEGIES",
    "DEFAULT_STRATEGY",
]
