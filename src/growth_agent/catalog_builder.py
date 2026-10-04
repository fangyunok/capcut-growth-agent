"""Turn real documentation into verifiable fact cards.

Every card must carry a quote that appears *verbatim* in its own source page.
The same "citations must be visible" rule the application enforces on model
output is therefore enforced on the catalog itself: a sentence that cannot be
located in its page is dropped, never hand-repaired, because a catalog nobody
can re-verify is worth less than a small catalog everybody can.

Extraction here is deliberately model-free. It is reproducible, costs nothing,
and cannot hallucinate. A model can be layered on later to add localized
wording, but it is never trusted to produce the quote.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Iterable

from .corpus import Document

MIN_SENTENCE_CHARS = 60
MAX_SENTENCE_CHARS = 400
KEYWORD_LIMIT = 6

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_HAS_TEXT = re.compile(r"[A-Za-z\u4e00-\u9fff]")

# Sentences that state a capability, rather than navigation or marketing filler.
_CAPABILITY = re.compile(
    r"\b(can|allows?|supports?|lets|enables|provides?|requires?|defaults?|"
    r"click|select|set|choose|add|configure|record|stream|capture|remux|"
    r"export|import)\b"
    r"|可以|支持|允许|使用|点击|选择|设置|添加|配置|录制|直播|捕获|导出|导入",
    re.IGNORECASE,
)

_BOILERPLATE = re.compile(
    r"^(table of contents|back to|next|previous|related|share|download|"
    r"subscribe|cookie|privacy|terms|skip to|jump to|on this page|"
    r"was this|still need|copyright|all rights)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}")

# Words that carry no retrieval signal in a documentation sentence. Kept local
# so the shared tokenizer used by the ranking strategies is not changed by a
# catalog-building concern.
_NOISE = {
    "this", "that", "these", "those", "when", "then", "than", "with", "from",
    "have", "will", "also", "into", "your", "you", "our", "its", "any", "all",
    "not", "but", "are", "was", "were", "been", "being", "some", "such",
    "more", "most", "other", "each", "there", "their", "them", "they",
}

SITES: dict[str, dict[str, str]] = {
    "obsproject.com": {"product_id": "obs-studio", "product_name": "OBS Studio"},
}


def feature_from_url(url: str) -> str:
    """Use the documentation slug as the feature it documents.

    The slug is stable, unique and traceable, which matters more here than a
    hand-curated taxonomy: every card can be traced back to one page and one
    topic without a mapping table that could silently drift.
    """
    match = re.search(r"/kb/([a-z0-9][a-z0-9-]*)/?$", url)
    return match.group(1) if match else ""


def candidate_sentences(text: str) -> list[str]:
    """Split page text into sentences that could carry a checkable claim."""
    sentences = []
    for block in text.split("\n"):
        for raw in _SENTENCE_SPLIT.split(block):
            sentence = raw.strip()
            if not MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS:
                continue
            if not _HAS_TEXT.search(sentence):
                continue
            if _BOILERPLATE.match(sentence):
                continue
            if not _CAPABILITY.search(sentence):
                continue
            sentences.append(sentence)
    return sentences


def quote_is_verifiable(quote: str, source_text: str) -> bool:
    """The only check that matters: the quote exists in its own source."""
    return bool(quote) and quote in source_text


def keywords_for(sentence: str, feature: str) -> list[str]:
    """Cheap lexical anchors so sparse retrievers are not left with nothing."""
    seen: list[str] = []
    candidates = [feature.replace("-", " ")]
    candidates.extend(_WORD.findall(sentence))
    for token in candidates:
        folded = token.casefold()
        if len(folded) > 2 and folded not in _NOISE and folded not in seen:
            seen.append(folded)
        if len(seen) >= KEYWORD_LIMIT:
            break
    return seen


def _fingerprint(sentence: str) -> str:
    folded = unicodedata.normalize("NFKD", sentence.casefold())
    return re.sub(r"\W+", " ", folded).strip()


def build_cards(
    documents: Iterable[Document], domain: str, checked_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract cards and report what was rejected, with the reason.

    Returning the rejections matters: it is the evidence that the catalog was
    filtered by a rule rather than by a target count.
    """
    site = SITES.get(domain)
    if site is None:
        raise ValueError(f"Unknown documentation domain: {domain}")
    product_id = site["product_id"]
    stamp = checked_at or date.today().isoformat()

    cards: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    counters: dict[str, int] = {}

    for document in documents:
        feature = feature_from_url(document.url)
        if not feature:
            rejected.append({"url": document.url, "reason": "no feature slug"})
            continue
        for sentence in candidate_sentences(document.text):
            if not quote_is_verifiable(sentence, document.text):
                rejected.append({
                    "url": document.url, "reason": "quote not found in source",
                })
                continue
            fingerprint = _fingerprint(sentence)
            if fingerprint in seen:
                rejected.append({"url": document.url, "reason": "duplicate"})
                continue
            seen.add(fingerprint)

            counters[feature] = counters.get(feature, 0) + 1
            cards.append({
                "id": f"{product_id}-{feature}-{counters[feature]:03d}",
                "product_id": product_id,
                "product_name": site["product_name"],
                "feature": feature,
                "statement": sentence,
                "source_quote": sentence,
                "source_url": document.url,
                "source_title": document.title or document.url,
                "checked_at": stamp,
                "keywords": keywords_for(sentence, feature),
                "availability_note": (
                    "Automatically extracted from the linked public page. "
                    "Re-check the live page before publishing."
                ),
                "localized_statement": {},
            })

    return cards, rejected


def catalog_payload(
    cards: list[dict[str, Any]], rejected: list[dict[str, str]],
    documents: int, domain: str,
) -> dict[str, Any]:
    """Wrap cards with provenance so a reader can audit how they were built."""
    return {
        "version": f"auto-extracted-{date.today().isoformat()}",
        "scope": (
            "Fact cards extracted verbatim from public product documentation. "
            "Every source_quote is a literal substring of its source_url page. "
            "No model was used to produce or reword a quote."
        ),
        "provenance": {
            "domain": domain,
            "documents_crawled": documents,
            "cards_kept": len(cards),
            "candidates_rejected": len(rejected),
            "extraction": "model-free sentence selection plus verbatim check",
        },
        "facts": cards,
    }
