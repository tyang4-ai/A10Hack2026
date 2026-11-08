"""Stop one agent while the run keeps going (CONTRACT.md 13, docs/BACKEND_V2.md step 2). Built by V2-gateway.

The route (POST /runs/{id}/agents/{agent_id}/stop) has already checked that the run and the agent are both
still going. stop_agent will: mark the agent stopped and emit agent.status with `reason`; add the deny rule
R-stop-<agent_id> (agent_id set, origin user_agent_stop, the agent's action) with a policy version bump and
policy.updated, so Gateway.decide denies its next request right after the kill switch check; discard any
result that lands after the stop; and leave the agent's unfinished targets to finalize, which declares them
as a Hole "agent stopped by the analyst". Stopping Bobby makes every later gate proceed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext

PLAN_ID = "V2-gateway"


async def stop_agent(ctx: RunContext, agent_id: str, reason: str | None) -> m.Agent:
    """Stop one agent and return it (status stopped). Raises NotImplementedError until V2-gateway."""
    raise NotImplementedError(PLAN_ID)
