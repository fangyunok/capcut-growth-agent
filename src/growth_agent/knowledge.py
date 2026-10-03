"""Small, auditable lexical retriever for the local product fact catalog."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


_STOPWORDS = {
    "a", "and", "are", "can", "capcut", "create", "de", "do", "el", "en",
    "for", "from", "how", "in", "is", "la", "las", "los", "of", "on",
    "or", "para", "the", "to", "un", "una", "use", "video", "videos",
    "with", "y",
}
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _terms(value: str) -> set[str]:
    return {
        term for term in _WORDS.findall(_normalize(value))
        if len(term) > 1 and term not in _STOPWORDS
    }


class KnowledgeBase:
    """Load curated facts and rank matches with explainable word overlap.

    Search returns no results when the query contains no useful matching terms.
    It is deliberately not a semantic or web search service.
    """

    def __init__(self, facts: list[dict]):
        self._facts = deepcopy(facts)
        self._by_id = {fact["id"]: fact for fact in self._facts}
        if len(self._by_id) != len(self._facts):
            raise ValueError("Duplicate fact IDs in the knowledge base")

    @classmethod
    def from_file(cls, path: str | Path) -> KnowledgeBase:
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
                and isinstance(text, str) and text.strip()
                for locale, text in localized.items()
            ):
                raise ValueError(f"Fact {fact['id']} requires a locale-to-nonblank-text mapping")
            if not isinstance(fact["keywords"], list) or not all(
                isinstance(keyword, str) and keyword.strip() for keyword in fact["keywords"]
            ):
                raise ValueError(f"Fact {fact['id']} must have a string keywords list")
        return cls(data["facts"])

    def get(self, fact_id: str) -> dict | None:
        fact = self._by_id.get(fact_id)
        return deepcopy(fact) if fact is not None else None

    def all(self) -> list[dict]:
        return deepcopy(self._facts)

    def search(self, query: str, top_k: int = 5, product_id: str = "") -> list[dict]:
        if top_k <= 0 or not query.strip():
            return []
        query_terms = _terms(query)
        if not query_terms:
            return []
        normalized_query = _normalize(query)
        ranked: list[tuple[int, str, dict]] = []

        for fact in self._facts:
            if product_id and fact.get("product_id") != product_id:
                continue
            feature_terms = _terms(fact["feature"] + " " + fact["id"])
            keyword_terms = _terms(" ".join(fact["keywords"]))
            statement_terms = _terms(fact["statement"])
            localized = fact.get("localized_statement", {})
            if isinstance(localized, dict):
                statement_terms |= _terms(" ".join(map(str, localized.values())))

            score = 5 * len(query_terms & feature_terms)
            score += 3 * len(query_terms & keyword_terms)
            score += len(query_terms & statement_terms)
            matched_terms = query_terms & (
                feature_terms | keyword_terms | statement_terms
            )
            for phrase in fact["keywords"]:
                normalized_phrase = _normalize(phrase).strip()
                if len(normalized_phrase) >= 3 and normalized_phrase in normalized_query:
                    score += 5
                    matched_terms |= _terms(phrase)

            if score > 0:
                result = deepcopy(fact)
                result["retrieval_score"] = score
                result["matched_terms"] = sorted(matched_terms)
                ranked.append((score, fact["id"], result))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [result for _, _, result in ranked[:top_k]]
