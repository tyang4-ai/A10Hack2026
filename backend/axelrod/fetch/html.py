"""HTML main text with trafilatura, split on paragraph boundaries into pseudo-pages of about 3,000 characters.

HTML has no real pages, so the backend makes pseudo-pages (`Document.paginated` is false) small enough for
one extractor each. Links are read from the raw markup with the standard library, because trafilatura
drops them from the main text. trafilatura is imported inside the function, never at module import.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.IGNORECASE)


def decode_html(body: bytes | str, content_type: str = "") -> str:
    """Bytes to text: the header charset, then a <meta charset>, then UTF-8, then cp1252."""
    if isinstance(body, str):
        return body
    candidates: list[str] = []
    header = re.search(r"charset\s*=\s*([A-Za-z0-9_\-]+)", content_type or "", re.IGNORECASE)
    if header:
        candidates.append(header.group(1))
    meta = _CHARSET.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for enc in [*candidates, "utf-8"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("cp1252", errors="replace")


def html_text(html: bytes | str) -> str:
    """Main text of the page. trafilatura first, a plain tag stripper when it finds nothing."""
    import trafilatura

    markup = decode_html(html)
    text = trafilatura.extract(markup, output_format="txt", include_tables=True, include_comments=False,
                               favor_recall=True)
    if text and text.strip():
        return text
    return _plain_text(markup)


def html_pages(html: bytes | str, *, target_chars: int = 3000) -> list[str]:
    """Pseudo-pages of the main text. Empty list when the page has no text."""
    return split_pages(html_text(html), target_chars=target_chars)


def split_pages(text: str, *, target_chars: int = 3000) -> list[str]:
    """Group paragraphs (lines) into pages of at most target_chars. A longer paragraph is cut at spaces."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        para = line.strip()
        if not para:
            continue
        for piece in _cut(para, target_chars):
            extra = len(piece) + (1 if current else 0)
            if current and size + extra > target_chars:
                pages.append("\n".join(current))
                current, size, extra = [], 0, len(piece)
            current.append(piece)
            size += extra
    if current:
        pages.append("\n".join(current))
    return pages


def _cut(para: str, limit: int) -> list[str]:
    """Split one paragraph at the last space before limit, so no page grows far past the target."""
    pieces: list[str] = []
    while len(para) > limit:
        cut = para.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(para[:cut].rstrip())
        para = para[cut:].lstrip()
    if para:
        pieces.append(para)
    return pieces


def html_links(html: bytes | str, base_url: str) -> list[str]:
    """Absolute http(s) links in the markup, fragments dropped, first appearance order, no duplicates."""
    parser = _LinkParser()
    parser.feed(decode_html(html))
    seen: set[str] = set()
    out: list[str] = []
    for href in parser.hrefs:
        url, _ = urldefrag(urljoin(base_url, href.strip()))
        if urlsplit(url).scheme not in ("http", "https") or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


_SKIP = {"script", "style", "noscript", "template", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
          "blockquote", "pre", "ul", "ol", "header", "footer"}


class _TextParser(HTMLParser):
    """Visible-ish text with one line per block element. Only used when trafilatura returns nothing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _plain_text(markup: str) -> str:
    parser = _TextParser()
    parser.feed(markup)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)
