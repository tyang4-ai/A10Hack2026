"""Stage collect: fetch approved sources through the gateway in tier then rank order, ingest, scan untrusted ones."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


async def run(ctx: RunContext) -> None:
    raise NotImplementedError("B-ingest")
