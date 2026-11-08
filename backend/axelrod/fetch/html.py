"""HTML main text with trafilatura, split on paragraph boundaries into pseudo-pages of about 3,000 characters.
trafilatura is imported inside the function, never at module import."""
from __future__ import annotations


def html_pages(html: bytes | str, *, target_chars: int = 3000) -> list[str]:
    raise NotImplementedError("B-ingest")
