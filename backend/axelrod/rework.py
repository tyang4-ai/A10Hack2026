"""Rerun one goon in the same run, with instructions (CONTRACT.md 14, docs/BACKEND_V2.md step 3). Built by V3-rework.

The analyst (POST /runs/{id}/agents/{agent_id}/rerun, by="analyst") and Bobby (at a gate or on a comment,
by="orchestrator") share this one machine. The route has already checked: the agent exists, the run is not
stopped, the agent is not queued or working, it isn't Bobby and it isn't a fetcher.

rerun_agent will: register a new agent `<root>.<k>` (extractor-03.2, then extractor-03.3) with the same role
and targets, parent_agent_id, the instructions and rerun_by, through AgentRunner(..., agent_id=,
parent_agent_id=, instructions=, rerun_by=), which adds the labeled Instructions block to every prompt and
names the agent; run it under the current policy, through the gateway; turn its outputs into new versions of
the same item ids (item.updated reason "rework", the old version kept as an attempt with action "rework");
with recheck, re-check everything citing the changed items (item.verified, summary.verified) and reopen a
finished run, then complete it again. It returns the new agent as soon as it is registered and does the
work in a side task (ctx.spawn), the way the mock answers 201 at once. Raise runtime.InvalidState for a
state the route can't see (the API answers 409 invalid_state).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext

PLAN_ID = "V3-rework"


async def rerun_agent(ctx: RunContext, agent_id: str, instructions: str, recheck: bool, *,
                      by: Literal["analyst", "orchestrator"] = "analyst") -> m.Agent:
    """Rerun one goon with instructions and return the new agent. Raises NotImplementedError until V3-rework."""
    raise NotImplementedError(PLAN_ID)
