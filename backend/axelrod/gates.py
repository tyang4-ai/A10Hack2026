"""Stage gates: Bobby, the orchestrator, spawns first and decides after plan, extract, repair and verify_summary.

V1-shapes makes spawn_bobby and finish_bobby real and after_stage a no-op. V3-rework fills after_stage:
code builds a short digest of the stage (counts, failures by check, holes, budget left), Bobby answers with
a forced tool call (proceed, rework at most 3 goons once each with instructions, or escalate), code
validates the call, runs the reworks through rework.rerun_agent(by="orchestrator") and emits
agent.decision. Code keeps him in bounds (CONTRACT.md 14): he never touches sources, policy or check
results, every call goes through the gateway, and a stopped Bobby means proceed at every later gate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .contract import m
from .ids import now_iso
from .store.state import BOBBY_ID, ORCHESTRATOR_ROLE

if TYPE_CHECKING:
    from .runtime import RunContext

GATE_STAGES = ("plan", "extract", "repair", "verify_summary")
BOBBY_TASK = "Orchestrate the run: review each stage gate, send goons back to rework, answer comments"
TERMINAL = ("done", "failed", "stopped")


def bobby(ctx: RunContext) -> m.Agent | None:
    """The run's orchestrator agent, if he was spawned."""
    return ctx.state.agents.get(BOBBY_ID)


def spawn_bobby(ctx: RunContext) -> m.Agent:
    """Register Bobby (role orchestrator, the large model, status working) and emit agent.spawned.
    Idempotent: a run that already has him keeps him as he is."""
    existing = bobby(ctx)
    if existing is not None:
        return existing
    agent = m.Agent(agent_id=BOBBY_ID, role=ORCHESTRATOR_ROLE, model_id=ctx.settings.model_large,
                    status="working", task=BOBBY_TASK, target_ids=[], started_at=now_iso())
    ctx.state.register_agent(agent)
    ctx.emit("agent.spawned", m.AgentSpawnedData(agent=agent.model_copy(deep=True)), agent_id=BOBBY_ID,
             message="Bobby spawned: the orchestrator makes the big calls, his goons do the work")
    return agent


def finish_bobby(ctx: RunContext, status: str = "done") -> None:
    """Bobby works until the run ends: mark him done (or failed, stopped) once, with agent.status."""
    agent = bobby(ctx)
    if agent is None or agent.status in TERMINAL:
        return
    agent.status, agent.finished_at = status, now_iso()
    ctx.emit("agent.status", m.AgentStatusData(agent_id=BOBBY_ID, status=status, task=agent.task,
                                               usage=agent.usage.model_copy()),
             agent_id=BOBBY_ID, message=f"Bobby {status}: the run is over")


async def after_stage(ctx: RunContext, stage: str) -> None:
    """Bobby's call after one of GATE_STAGES. A no-op until V3-rework: the run proceeds."""
    return None
