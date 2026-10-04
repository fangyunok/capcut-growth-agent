"""Cross-encoder reranking for fused candidates.

A bi-encoder compares a query and a passage without ever looking at them
together, so it can rank a topically similar passage above the one that
actually answers the query. A cross-encoder reads the pair jointly and is far
more accurate, at the cost of one forward pass per candidate. That trade-off is
why reranking is applied only to a short fused candidate list, never to the
whole catalog.
"""

from __future__ import annotations

from typing import Any

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


class CrossEncoderReranker:
    """Score (query, passage) pairs jointly with a bge reranker."""

    def __init__(
        self, model_name: str = DEFAULT_RERANK_MODEL, device: str | None = None,
        max_length: int = 512,
    ):
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Reranking needs the 'embed' extra: pip install -e \".[embed]\""
            ) from exc
        self._torch = torch
        self.name = f"cross-encoder:{model_name}"
        self._max_length = max_length
        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self._model = self._model.to(self._device)
        self._model.eval()

    @staticmethod
    def _passage(card: dict[str, Any]) -> str:
        return f"{card['feature']} {card['statement']}"

    def rerank(
        self, query: str, cards: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], float]]:
        """Return (card, score) pairs ordered most relevant first."""
        if not cards:
            return []
        pairs = [[query, self._passage(card)] for card in cards]
        torch = self._torch
        with torch.no_grad():
            batch = self._tokenizer(
                pairs, padding=True, truncation=True,
                max_length=self._max_length, return_tensors="pt",
            )
            batch = {key: value.to(self._device) for key, value in batch.items()}
            scores = self._model(**batch).logits.view(-1).float().cpu().tolist()
        ordered = sorted(zip(cards, scores), key=lambda item: -item[1])
        return [(card, float(score)) for card, score in ordered]


__all__ = ["CrossEncoderReranker", "DEFAULT_RERANK_MODEL"]
