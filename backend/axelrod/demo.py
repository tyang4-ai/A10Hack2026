"""Demo controls (label them as demo in the UI): planted error injection and user edits, both re-verified."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .contract import m

if TYPE_CHECKING:
    from .runtime import RunContext


async def inject_errors(ctx: RunContext, req: m.InjectErrorsRequest) -> m.InjectErrorsResponse:
    """Corrupt one digit in req.count passing quotes, re-run the checks, emit item.updated (demo_injection)."""
    raise NotImplementedError("B-cli")


async def patch_item(ctx: RunContext, item_id: str, patch: m.ItemPatch) -> m.Item:
    """User edit of one item's text, then re-verify it."""
    raise NotImplementedError("B-cli")
