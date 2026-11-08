"""Stage summarize: the summarizer writes the 3-page brief, code assigns H ids and streams summary.sentence."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-brief")
