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
DEFAULT_BATCH_SIZE = 32


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
        ranked = self.rerank_many([(query, cards)])
        return ranked[0]

    def rerank_many(
        self, groups: list[tuple[str, list[dict[str, Any]]]],
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> list[list[tuple[dict[str, Any], float]]]:
        """Score many candidate groups in a single batched pass.

        Ranking one query at a time leaves the CPU idle between small batches.
        Measured on this machine over 60 groups of 10 pairs, one group at a
        time took 93 s while flattening the pairs into batches of 32 took 65 s,
        for identical rankings. Scoring a single pair per call was worst at
        roughly 239 s, which is why batching matters more than any other knob.

        Note that ``max_length`` is *not* the lever here: candidate pairs are
        only 27-104 tokens, so 512 / 256 / 128 all measure within noise.
        """
        output: list[list[tuple[dict[str, Any], float]]] = [[] for _ in groups]
        flat: list[list[str]] = []
        owners: list[int] = []
        cards: list[dict[str, Any]] = []
        for index, (query, group) in enumerate(groups):
            for card in group:
                flat.append([query, self._passage(card)])
                owners.append(index)
                cards.append(card)
        if not flat:
            return output

        torch = self._torch
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(flat), batch_size):
                chunk = flat[start:start + batch_size]
                batch = self._tokenizer(
                    chunk, padding=True, truncation=True,
                    max_length=self._max_length, return_tensors="pt",
                )
                batch = {key: value.to(self._device) for key, value in batch.items()}
                scores.extend(
                    self._model(**batch).logits.view(-1).float().cpu().tolist()
                )

        for owner, card, score in zip(owners, cards, scores):
            output[owner].append((card, float(score)))
        for group in output:
            group.sort(key=lambda item: -item[1])
        return output


__all__ = ["CrossEncoderReranker", "DEFAULT_RERANK_MODEL"]
