"""Stage approve: wait for POST /approval, apply the ApprovalRequest rules, build the policy."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-store-api")
