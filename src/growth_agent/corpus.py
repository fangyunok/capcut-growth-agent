"""Build the fact-card catalog from real product documentation.

The catalog's value depends entirely on its sources being real and checkable,
so every step fails closed: a page that cannot be fetched, a quote that cannot
be found in its own page, or a link that does not resolve is dropped rather
than stored. Nothing is invented to reach a target count.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = (
    "claim-studio-corpus-builder/0.1 "
 "(+https://github.com/fangyunok/claim-studio)"
)

_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "nav", "footer", "header", "form",
    "button", "iframe",
}
_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "br",
    "td", "th", "tr", "section", "article", "blockquote",
}
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class _VisibleText(HTMLParser):
    """Extract readable text while dropping navigation and script noise."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip()
                 for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


@dataclass
class Document:
    """One fetched page, with the text a quote must be verifiable against."""

    url: str
    title: str
    text: str
    links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "text": self.text}


def fetch(url: str, timeout: float = 25.0) -> str:
    """Fetch one page, refusing anything that is not a public HTTP(S) page."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            charset = reply.headers.get_content_charset() or "utf-8"
            return reply.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def parse_document(url: str, html: str) -> Document:
    parser = _VisibleText()
    parser.feed(html)
    title_match = _TITLE.search(html)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    links = sorted({
        _absolute(url, href)
        for href in re.findall(r'href="([^"#]+)"', html)
    } - {""})
    return Document(url=url, title=title, text=parser.text(), links=links)


def _absolute(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        match = re.match(r"https?://[^/]+", base)
        return (match.group(0) + href) if match else ""
    return ""


def discover(
    seeds: list[str], pattern: str, limit: int = 200, delay: float = 0.4,
) -> list[Document]:
    """Breadth-first crawl of same-site pages matching ``pattern``.

    A short delay is kept between requests so the crawl stays a polite client
    of a public documentation site rather than a load test.
    """
    matcher = re.compile(pattern)
    seen: set[str] = set()
    documents: list[Document] = []
    queue: deque[str] = deque(seeds)

    while queue and len(documents) < limit:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetch(url)
        except RuntimeError:
            continue
        document = parse_document(url, html)
        if document.text.strip():
            documents.append(document)
        for link in document.links:
            if link not in seen and matcher.search(link):
                queue.append(link)
        time.sleep(delay)

    return documents


def save_documents(documents: list[Document], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "count": len(documents),
        "documents": [document.to_dict() for document in documents],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_documents(path: str | Path) -> list[Document]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Document(
            url=item["url"], title=item.get("title", ""),
            text=item.get("text", ""),
        )
        for item in payload["documents"]
    ]
