"""Embedding backends for dense retrieval.

Two interchangeable providers are supported so the same index code can run
locally on CPU or against a hosted / self-hosted HTTP endpoint. A backend that
cannot start raises instead of silently degrading to lexical search: a silent
fallback would let an evaluation report describe a model that never ran.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_API_MODEL = "bge-m3"


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Turn text into L2-normalised rows so a dot product equals cosine."""

    name: str

    def encode(self, texts: list[str]) -> Any:
        ...
    def dimension(self) -> int | None:
        ...
    def is_available(self) -> bool:
        ...


class LocalEmbeddingBackend:
    """Run a bge model in-process through transformers.

    Requires the ``embed`` extra. Import errors are surfaced as an actionable
    message rather than a bare ModuleNotFoundError.
    """

    def __init__(
        self, model_name: str = DEFAULT_LOCAL_MODEL,
        device: str | None = None, max_length: int = 512,
    ):
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Local embeddings need the 'embed' extra: "
                "pip install -e \".[embed]\""
            ) from exc
        self._torch = torch
        self.name = f"local:{model_name}"
        self._model_name = model_name
        self._max_length = max_length
        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModel.from_pretrained(model_name).to(self._device)
        self._model.eval()
        self._dimension = int(self._model.config.hidden_size)

    def dimension(self) -> int | None:
        return self._dimension

    def is_available(self) -> bool:
        return True

    def encode(self, texts: list[str]) -> Any:
        texts = list(texts)
        if not texts:
            return None
        torch = self._torch
        batch = self._tokenizer(
            texts, padding=True, truncation=True,
            max_length=self._max_length, return_tensors="pt",
        )
        batch = {key: value.to(self._device) for key, value in batch.items()}
        with torch.no_grad():
            output = self._model(**batch)
        # BGE uses the CLS token as the sentence embedding.
        vectors = output.last_hidden_state[:, 0]
        vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        return vectors.float().cpu().numpy()


class ApiEmbeddingBackend:
    """Call an OpenAI-compatible ``/embeddings`` endpoint.

    This covers both hosted providers and a self-hosted Ollama server, which
    exposes the same route under ``/v1``.
    """

    def __init__(
        self, base_url: str, model: str = DEFAULT_API_MODEL,
        api_key: str = "", timeout: float = 60.0,
    ):
        if not base_url or not base_url.strip():
            raise ValueError("The api embedding backend needs a base URL")
        self.name = f"api:{model}"
        self._model_name = model
        self._base = base_url.strip().rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._dimension: int | None = None

    def dimension(self) -> int | None:
        return self._dimension

    def is_available(self) -> bool:
        try:
            self.encode(["probe"])
        except RuntimeError:
            return False
        return True

    def encode(self, texts: list[str]) -> Any:
        texts = list(texts)
        if not texts:
            return None
        import numpy as np

        payload = {"model": self._model_name, "input": texts}
        request = urllib.request.Request(
            f"{self._base}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as reply:
                response = json.load(reply)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Embedding service returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Cannot reach the embedding service") from exc
        rows = response.get("data") if isinstance(response, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("Embedding service returned an unexpected payload")
        ordered = sorted(rows, key=lambda item: item.get("index", 0))
        matrix = np.asarray([item["vector"] for item in ordered], dtype="float32")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._dimension = int(matrix.shape[1])
        return matrix / norms


def build_embedding_backend(
    backend: str | None = None, model: str | None = None,
    base_url: str | None = None, api_key: str | None = None,
) -> Any:
    """Select an embedding provider from arguments or the environment."""
    selected = (backend or os.getenv("GROWTH_EMBED_BACKEND", "local")).strip().lower()
    resolved_model = model or os.getenv("GROWTH_EMBED_MODEL") or ""
    if selected == "local":
        return LocalEmbeddingBackend(resolved_model or DEFAULT_LOCAL_MODEL)
    if selected == "api":
        resolved_base = base_url or os.getenv("GROWTH_EMBED_BASE") or ""
        if not resolved_base:
            raise ValueError(
                "The api embedding backend needs GROWTH_EMBED_BASE, "
                "for example http://127.0.0.1:11434/v1"
            )
        return ApiEmbeddingBackend(
            resolved_base, resolved_model or DEFAULT_API_MODEL,
            api_key=api_key or os.getenv("GROWTH_EMBED_KEY") or "",
        )
    raise ValueError(
        f"Unknown embedding backend {selected!r}; available: local, api"
    )


__all__ = [
    "EmbeddingBackend", "LocalEmbeddingBackend", "ApiEmbeddingBackend",
    "build_embedding_backend", "DEFAULT_LOCAL_MODEL", "DEFAULT_API_MODEL",
]
