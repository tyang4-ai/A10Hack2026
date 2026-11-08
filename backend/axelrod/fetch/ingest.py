"""Ingest: a fetched source becomes a Document with exact page texts in RunState and its files in the vault
(raw bytes, <file>.md with page markers, <file>.meta.json). Also builds DocumentPage views for the API."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..contract import m

if TYPE_CHECKING:
    from ..gateway import FetchResult
    from ..runtime import RunContext
    from ..store.state import RunState


async def ingest(ctx: RunContext, source: m.Source, result: FetchResult) -> m.Document:
    """Store result's body for source and return the Document (pages go into ctx.state.pages)."""
    raise NotImplementedError("B-ingest")


def document_page(state: RunState, doc_id: str, page: int) -> m.DocumentPage:
    """One page with highlights from the items citing it."""
    raise NotImplementedError("B-ingest")
