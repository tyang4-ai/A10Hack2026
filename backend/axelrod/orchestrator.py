"""The pipeline loop for one run: stages in contract order, run.status between them,
StopRequested becomes `stopped`, any other exception becomes `failed` with Run.error."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import RunContext


async def run_pipeline(ctx: RunContext) -> None:
    """Run every stage's `run(ctx)` in order (one asyncio task per run)."""
    raise NotImplementedError("B-store-api")
