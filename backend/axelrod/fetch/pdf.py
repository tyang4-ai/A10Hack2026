"""PDF page text: PyMuPDF first, pypdf as the fallback, page n stored as index n-1.

The page text is stored exactly as the library returns it, because the checker matches quotes against
that stored text and code computes offsets into it. Both libraries are imported inside the functions,
never at module import, so the API starts fast.
"""
from __future__ import annotations

import io


class PdfError(ValueError):
    """The bytes could not be read as a PDF by either library."""


def pdf_pages(data: bytes) -> list[str]:
    """Text of every page, in order. Index n-1 is page n of the file (not the printed page label)."""
    try:
        pages = _pymupdf_pages(data)
    except Exception as first:  # noqa: BLE001  any PyMuPDF failure falls back to pypdf
        try:
            return _pypdf_pages(data)
        except Exception as second:  # noqa: BLE001
            raise PdfError(f"not a readable PDF (PyMuPDF: {first}; pypdf: {second})") from second
    if pages and not any(p.strip() for p in pages):
        # No text layer according to PyMuPDF. pypdf sometimes reads fonts PyMuPDF can't, so try it once.
        try:
            other = _pypdf_pages(data)
        except Exception:  # noqa: BLE001
            return pages
        if len(other) == len(pages) and any(p.strip() for p in other):
            return other
    return pages


def _pymupdf_pages(data: bytes) -> list[str]:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


def _pypdf_pages(data: bytes) -> list[str]:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    return [page.extract_text() or "" for page in reader.pages]
