"""Stage repair: weaken failed items toward the evidence, at most two attempts, then holes and finalize_report."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-reason")
