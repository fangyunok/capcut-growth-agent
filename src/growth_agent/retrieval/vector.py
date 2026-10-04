"""Dense retrieval over bge embeddings.

Vectors are cached on disk so a repeated evaluation does not re-encode an
unchanged catalog. Similarity search is a brute-force matrix product, which is
the correct choice at catalog sizes in the hundreds: an approximate index such
as FAISS or Milvus would add a dependency and a recall trade-off while changing
neither the latency nor the ranking at this scale.
"""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .embedding import build_embedding_backend
from .text import normalize

DEFAULT_TOP_K = 5
MIN_SCORE_ENV = "GROWTH_VECTOR_MIN_SCORE"


def configured_min_score() -> float | None:
    """Read the relevance floor, if one has been calibrated.

    A dense retriever has no natural "no match" state: cosine similarity is
    defined for every pair, so an unrelated query still returns a ranked list.
    Without a floor, the abstention guarantee that the rest of the application
    depends on becomes unreachable.
    """
    raw = os.getenv(MIN_SCORE_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{MIN_SCORE_ENV} must be a number, got {raw!r}") from exc


def index_directory() -> Path:
    """Where cached vectors live; overridable so wheels stay read-only."""
    configured = os.getenv("GROWTH_INDEX_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd() / ".index"


class VectorRetriever:
    """Rank cards by cosine similarity to the query embedding."""

    name = "vector"

    def __init__(
        self, facts: list[dict[str, Any]], backend: Any | None = None,
        cache_dir: str | Path | None = None, min_score: float | None = None,
    ):
        self._facts = facts
        self._backend = backend if backend is not None else build_embedding_backend()
        self.name = f"vector:{self._backend.name}"
        self._documents = [self._document(fact) for fact in facts]
        self._cache_dir = Path(cache_dir) if cache_dir else index_directory()
        self._min_score = (
            min_score if min_score is not None else configured_min_score()
        )
        self._matrix = self._load_or_build()

    def _document(self, fact: dict[str, Any]) -> str:
        """Text a card is embedded as: the same fields every retriever may match."""
        parts = [fact["feature"], " ".join(fact["keywords"]), fact["statement"]]
        localized = fact.get("localized_statement", {})
        if isinstance(localized, dict):
            parts.extend(str(wording) for wording in localized.values())
        return normalize(" ".join(parts))

    def _fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(self._backend.name.encode("utf-8"))
        for document in self._documents:
            digest.update(b"\x00")
            digest.update(document.encode("utf-8"))
        return digest.hexdigest()[:16]

    def _load_or_build(self) -> Any:
        import numpy as np

        if not self._documents:
            return np.zeros((0, 0), dtype="float32")
        cache = self._cache_dir / f"vectors-{self._fingerprint()}.npz"
        if cache.is_file():
            try:
                with np.load(cache) as stored:
                    return stored["matrix"]
            except (OSError, ValueError, KeyError):
                # A corrupt cache must never block a run; rebuild below.
                pass
        matrix = self._backend.encode(self._documents)
        if matrix is None:
            return np.zeros((0, 0), dtype="float32")
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache, matrix=matrix)
        except OSError:
            pass
        return matrix

    def search(
        self, query: str, top_k: int = DEFAULT_TOP_K, product_id: str = ""
    ) -> list[dict[str, Any]]:
        import numpy as np

        if top_k <= 0 or not query.strip() or self._matrix.size == 0:
            return []
        vector = self._backend.encode([query])
        if vector is None:
            return []
        scores = self._matrix @ vector[0]
        order = np.argsort(-scores)

        results: list[dict[str, Any]] = []
        for row in order:
            position = int(row)
            fact = self._facts[position]
            # Product isolation is enforced here as well, so a dense retriever
            # can never leak another product's card into an audit.
            if product_id and fact.get("product_id") != product_id:
                continue
            score = float(scores[position])
            if self._min_score is not None and score < self._min_score:
                # Rows are ordered by descending score, so the first card below
                # the floor ends the list. This is what lets a dense retriever
                # report "no evidence" instead of always returning a neighbour.
                break
            result = deepcopy(fact)
            result["retrieval_score"] = round(score, 6)
            result["matched_terms"] = []
            results.append(result)
            if len(results) >= top_k:
                break
        return results
