"""PDF page text: PyMuPDF first, pypdf as the fallback, page n stored as index n-1.
Both libraries are imported inside the function, never at module import."""
from __future__ import annotations


def pdf_pages(data: bytes) -> list[str]:
    """Text of every page, in order."""
    raise NotImplementedError("B-ingest")
