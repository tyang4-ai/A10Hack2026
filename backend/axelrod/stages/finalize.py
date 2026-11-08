"""Stage complete, plus finalize_report: certainty for every item, the rollup, the holes register, report.ready,
and the final reports and check files written to the vault."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import RunContext


def finalize_report(ctx: RunContext) -> None:
    """Certainty for every item, the rollup from the rubric, holes, ReportCertainty, then report.ready."""
    raise NotImplementedError("B-reason")


async def run(ctx: RunContext) -> None:
    """Write 09-Analysis/agent-report.json/.md, human-report.json/.md and 12-Verification/*.json to the vault."""
    raise NotImplementedError("B-reason")
