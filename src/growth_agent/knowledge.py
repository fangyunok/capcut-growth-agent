"""Load, validate and rank a replaceable product evidence catalog.

Validation stays here; ranking is delegated to a pluggable :mod:`retrieval`
strategy. The default strategy reproduces the original lexical behaviour, so
callers see the same results unless they explicitly select another retriever.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .retrieval import Retriever, build_retriever


class KnowledgeBase:
    """Hold curated facts and rank matches through the configured retriever."""

    def __init__(self, facts: list[dict], retriever: Retriever | None = None):
        self._facts = deepcopy(facts)
        self._by_id = {fact["id"]: fact for fact in self._facts}
        if len(self._by_id) != len(self._facts):
            raise ValueError("Duplicate fact IDs in the knowledge base")
        self._retriever = (
            retriever if retriever is not None else build_retriever(self._facts)
        )

    @property
    def retriever_name(self) -> str:
        """Active strategy name, recorded in run traces and evaluation reports."""
        return getattr(self._retriever, "name", "unknown")

    @classmethod
    def from_file(
        cls, path: str | Path, retriever: Retriever | None = None
    ) -> KnowledgeBase:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
            raise ValueError("Knowledge file must contain a 'facts' list")
        required = {
            "id", "feature", "statement", "source_quote", "source_url",
            "source_title", "checked_at", "keywords", "availability_note",
        }
        for position, fact in enumerate(data["facts"]):
            if not isinstance(fact, dict) or required - fact.keys():
                raise ValueError(f"Fact at index {position} is missing required fields")
            if not isinstance(fact["id"], str) or not fact["id"].strip():
                raise ValueError(f"Fact at index {position} has an invalid ID")
            for field in ("feature", "statement", "source_quote", "source_url", "source_title", "checked_at"):
                if not isinstance(fact[field], str) or not fact[field].strip():
                    raise ValueError(f"Fact {fact['id']} has invalid {field}; a nonblank string is required")
            if not isinstance(fact["availability_note"], str):
                raise ValueError(f"Fact {fact['id']} has invalid availability_note")
            source = urlsplit(fact["source_url"])
            if source.scheme not in {"https", "http"} or not source.hostname or source.username or source.password:
                raise ValueError(f"Fact {fact['id']} requires an HTTP(S) source URL without credentials")
            try:
                date.fromisoformat(fact["checked_at"])
            except ValueError as exc:
                raise ValueError(f"Fact {fact['id']} requires checked_at in YYYY-MM-DD format") from exc
            if "product_id" in fact and (
                not isinstance(fact["product_id"], str) or not fact["product_id"].strip()
            ):
                raise ValueError(f"Fact {fact['id']} has an invalid product_id")
            localized = fact.get("localized_statement", {})
            if not isinstance(localized, dict) or not all(
                isinstance(locale, str) and locale.strip()
                and isinstance(wording, str) and wording.strip()
                for locale, wording in localized.items()
            ):
                raise ValueError(f"Fact {fact['id']} requires a locale-to-nonblank-text mapping")
            if not isinstance(fact["keywords"], list) or not all(
                isinstance(keyword, str) and keyword.strip() for keyword in fact["keywords"]
            ):
                raise ValueError(f"Fact {fact['id']} must have a string keywords list")
        return cls(data["facts"], retriever=retriever)

    def get(self, fact_id: str) -> dict | None:
        fact = self._by_id.get(fact_id)
        return deepcopy(fact) if fact is not None else None

    def all(self) -> list[dict]:
        return deepcopy(self._facts)

    def search(
        self, query: str, top_k: int = 5, product_id: str = ""
    ) -> list[dict[str, Any]]:
        return self._retriever.search(query, top_k=top_k, product_id=product_id)
