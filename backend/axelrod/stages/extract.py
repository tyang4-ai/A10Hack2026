"""Stage extract: one small-model extractor per page, offsets by code, data checks right away, code extraction for JSON."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-extract")
