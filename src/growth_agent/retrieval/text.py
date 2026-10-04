"""Shared normalization and tokenization for every retrieval strategy.

The lexical baseline and BM25 must agree on what counts as a term, otherwise a
comparison between them measures the tokenizer instead of the ranking model.
"""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = {
    "a", "and", "are", "can", "capcut", "create", "de", "do", "el", "en",
    "for", "from", "how", "in", "is", "la", "las", "los", "of", "on",
    "or", "para", "the", "to", "un", "una", "use", "video", "videos",
    "with", "y",
}

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_LATIN = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def normalize(value: str) -> str:
    """Casefold and strip accents so a query folds together with its source."""
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def words(value: str) -> list[str]:
    """Word tokens longer than one character, minus stopwords."""
    return [
        token for token in _WORDS.findall(normalize(value))
        if len(token) > 1 and token not in STOPWORDS
    ]


def terms(value: str) -> set[str]:
    return set(words(value))


def index_terms(value: str) -> list[str]:
    """Tokens for BM25: Latin words plus CJK character bigrams.

    Chinese text has no spaces, so a whitespace tokenizer would treat a whole
    sentence as a single term. Character bigrams give partial matching without
    shipping a segmentation dictionary as a dependency.
    """
    normalized = normalize(value)
    output = [
        token for token in _LATIN.findall(normalized)
        if len(token) > 1 and token not in STOPWORDS
    ]
    for run in _CJK.findall(normalized):
        if len(run) == 1:
            output.append(run)
        else:
            output.extend(run[index:index + 2] for index in range(len(run) - 1))
    return output
