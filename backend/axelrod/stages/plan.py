"""Stage plan: stream the frozen SHL plan (plan.need, plan.source, plan.ready) when the company matches an alias."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-store-api")
