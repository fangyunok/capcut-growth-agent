"""MCP v2 tools for evidence lookup and deterministic editorial checks."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from growth_agent.knowledge import KnowledgeBase
from growth_agent.resources import DEFAULT_AUDIT_KNOWLEDGE, DEFAULT_AUDIT_RULES


class SearchResponse(BaseModel):
    query: str
    results: list[dict[str, Any]]
    count: int


class RulesResponse(BaseModel):
    locale: str
    tone: str
    forbidden_phrases: list[str]
    cta: str
    disclaimer: str


class CheckResponse(BaseModel):
    passed: bool
    unknown_fact_ids: list[str]
    forbidden_phrases: list[str]
    unsupported_numbers: list[str]
    checked_fact_ids: list[str]
    manual_review_required: bool = True
    limitation: str = Field(
        default=(
            "Deterministic checks only. Linked IDs, forbidden phrases, and numbers "
            "are checked; factual entailment, translations, and product availability "
            "still require human review."
        )
    )


_NUMBER = re.compile(r"(?<!\d)\d+(?:[.,]\d+)*(?:\s?%|[Kk])?")


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _numbers(value: str) -> set[str]:
    return {match.group().replace(" ", "") for match in _NUMBER.finditer(value)}


def create_server(knowledge_path: str | Path, rules_path: str | Path) -> MCPServer:
    """Create a local MCP server using independently verifiable source files."""

    knowledge = KnowledgeBase.from_file(knowledge_path)
    with Path(rules_path).open("r", encoding="utf-8") as handle:
        rule_data = json.load(handle)
    if not isinstance(rule_data, dict) or not isinstance(rule_data.get("locales"), dict):
        raise ValueError("Editorial rules must contain a 'locales' object")
    locales = rule_data["locales"]
    for locale, rules in locales.items():
        if not isinstance(locale, str) or not isinstance(rules, dict):
            raise ValueError("Editorial locales must map language codes to rule objects")
        for field in ("tone", "cta"):
            if not isinstance(rules.get(field), str) or not rules[field].strip():
                raise ValueError(f"Editorial rules for {locale} require a nonblank {field}")
        phrases = rules.get("forbidden_phrases")
        if not isinstance(phrases, list) or not all(isinstance(phrase, str) and phrase.strip() for phrase in phrases):
            raise ValueError(f"Editorial rules for {locale} require a list of nonblank forbidden_phrases")
    disclaimer = str(rule_data.get("disclaimer", ""))
    server = MCPServer("Product Evidence Review", version="0.2.0")

    def locale_rules(locale: str) -> dict:
        rules = locales.get(locale)
        if not isinstance(rules, dict):
            raise ValueError(
                f"Unsupported locale {locale!r}; available: {', '.join(sorted(locales))}"
            )
        return rules

    @server.tool()
    def search_knowledge(query: str, top_k: int = 5, product_id: str = "") -> SearchResponse:
        """Search the selected product fact catalog for source-backed evidence."""
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        results = knowledge.search(query, top_k=top_k, product_id=product_id)
        return SearchResponse(query=query, results=results, count=len(results))

    @server.tool()
    def get_editorial_rules(locale: str) -> RulesResponse:
        """Return the prototype's editorial constraints for one locale."""
        rules = locale_rules(locale)
        return RulesResponse(
            locale=locale,
            tone=str(rules["tone"]),
            forbidden_phrases=list(rules["forbidden_phrases"]),
            cta=str(rules["cta"]),
            disclaimer=disclaimer,
        )

    @server.tool()
    def check_draft(text: str, fact_ids: list[str], locale: str) -> CheckResponse:
        """Flag unknown references, forbidden phrases, and unsupported numbers.

        This is a deterministic pre-review guard, not semantic fact verification.
        """
        rules = locale_rules(locale)
        unique_ids = list(dict.fromkeys(fact_ids))
        known_facts = [knowledge.get(fact_id) for fact_id in unique_ids]
        unknown = [fact_id for fact_id, fact in zip(unique_ids, known_facts) if fact is None]
        checked = [fact_id for fact_id, fact in zip(unique_ids, known_facts) if fact is not None]
        normalized_text = _fold(text)
        forbidden = [
            phrase for phrase in rules["forbidden_phrases"]
            if _fold(phrase) in normalized_text
        ]

        supported_numbers: set[str] = set()
        for fact in known_facts:
            if fact is None:
                continue
            supported_numbers |= _numbers(fact["statement"])
            localized = fact.get("localized_statement", {})
            if isinstance(localized, dict) and isinstance(localized.get(locale), str):
                supported_numbers |= _numbers(localized[locale])
        unsupported = sorted(_numbers(text) - supported_numbers)

        return CheckResponse(
            passed=not (unknown or forbidden or unsupported),
            unknown_fact_ids=unknown,
            forbidden_phrases=forbidden,
            unsupported_numbers=unsupported,
            checked_fact_ids=checked,
        )

    return server


mcp = create_server(
    DEFAULT_AUDIT_KNOWLEDGE,
    DEFAULT_AUDIT_RULES,
)


if __name__ == "__main__":
    mcp.run()
