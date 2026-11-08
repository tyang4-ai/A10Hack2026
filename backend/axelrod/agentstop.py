"""Stop one agent while the run keeps going (CONTRACT.md 13, docs/BACKEND_V2.md step 2).

The route (POST /runs/{id}/agents/{agent_id}/stop) has already checked that the run and the agent are both
still going. stop_agent then, in the contract's order: marks the agent stopped and emits agent.status with
`reason`; adds the deny rule R-stop-<agent_id> (agent_id set, origin user_agent_stop, the agent's action)
with a policy version bump and policy.updated, so Gateway.decide denies its next request right after the
kill switch check. A result that lands after the stop is discarded by the gateway. The agent's unfinished
work is declared by finalize.build_holes as a Hole "agent stopped by the analyst", never silently missing.

Stopping Bobby (the orchestrator) means every later gate proceeds: V3's gates read is_stopped(state, "bobby").
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .contract import m
from .gateway import STOP_REASON, stop_rule
from .ids import now_iso
from .runtime import InvalidState
from .store.state import BOBBY_ID

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

PLAN_ID = "V2-gateway"
TERMINAL = ("done", "failed", "stopped")


def is_stopped(state: RunState, agent_id: str) -> bool:
    """True once the analyst stopped this one agent: its R-stop rule is in the policy. A STOP of the whole
    run is the kill switch, not this."""
    return stop_rule(state.policy, agent_id) is not None


async def stop_agent(ctx: RunContext, agent_id: str, reason: str | None) -> m.Agent:
    """Stop one agent and return it (status stopped). Raises KeyError for an unknown agent and InvalidState
    when the run is over or the agent already finished (the route checks both first, this is the backstop)."""
    state = ctx.state
    agent = state.agents.get(agent_id)
    if agent is None:
        raise KeyError(agent_id)
    if state.is_finished():
        raise InvalidState(f"Run is already {state.run.status}")
    if agent.status in TERMINAL:
        raise InvalidState(f"{agent_id} is already {agent.status}")
    why = (reason or "").strip() or STOP_REASON
    agent.status, agent.finished_at = "stopped", now_iso()
    ctx.emit("agent.status", m.AgentStatusData(agent_id=agent_id, status="stopped", task=agent.task,
                                               usage=agent.usage.model_copy(), reason=why),
             message=f"{agent_id} stopped: {why}", agent_id=agent_id, level="warn")
    ctx.gateway.stop_agent(agent, why)
    if agent_id == BOBBY_ID:
        ctx.emit("agent.log", m.AgentLogData(text="Bobby is stopped: every later stage gate proceeds without his call."),
                 message="Bobby is stopped: every later stage gate proceeds without his call", agent_id=agent_id,
                 level="warn")
    return agent.model_copy(deep=True)
