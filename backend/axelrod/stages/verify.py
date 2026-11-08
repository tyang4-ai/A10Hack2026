"""Stage verify: code checks, then three isolated judges per conclusion and extrapolation (two of three pass)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-reason")
