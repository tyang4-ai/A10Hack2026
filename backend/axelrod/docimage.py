"""PDF page images for the document viewer (v0.3.1). Owned by B-pdfpage."""
from __future__ import annotations

from typing import Any


class NoPdf(LookupError):
    """The document has no stored PDF (HTML or JSON source, a replay, or the file is missing)."""


async def page_image(rt: Any, state: Any, doc: Any, page: int, item_id: str | None) -> bytes:
    raise NoPdf("not built yet: PDF page images")
