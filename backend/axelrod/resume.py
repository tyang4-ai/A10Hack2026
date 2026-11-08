"""Resume a stopped run (CONTRACT.md 13, docs/BACKEND_V2.md step 9). Built by V6-resume.

The route (POST /runs/{id}/resume) has already checked that the run is stopped. resume_run will: turn the
kill switch off with a policy version bump and policy.updated; emit run.resumed (reason, the stage it picks
up at) and run.status running; restart the stage STOP cut into, skipping work that already finished, in a
new pipeline task. Agents the analyst stopped one by one stay stopped (their R-stop rules stay in force).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext

PLAN_ID = "V6-resume"


async def resume_run(ctx: RunContext, reason: str | None = None) -> m.Run:
    """Resume the stopped run and return it (status running). Raises NotImplementedError until V6-resume."""
    raise NotImplementedError(PLAN_ID)
