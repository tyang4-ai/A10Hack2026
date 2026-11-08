"""The pipeline loop for one run: stages in contract order, run.status between them,
StopRequested becomes `stopped`, any other exception becomes `failed` with Run.error.

Bobby (gates.spawn_bobby) is the run's first agent, spawned right after the first run.status. After plan,
extract, repair and verify_summary the loop calls gates.after_stage, his gate. A gate never fails the run:
an error there is logged and the run proceeds, as if he had said proceed."""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from . import gates
from .contract import m
from .ids import now_iso
from .runtime import RunContext, StopRequested

if TYPE_CHECKING:
    from .config import Settings
    from .store import Store
    from .store.vault import Vault

log = logging.getLogger(__name__)

# (contract stage, module under axelrod/stages). Tests may shorten this list or put an async callable
# `fn(ctx)` in place of a module name.
StageRunner = Callable[["RunContext"], Awaitable[None]]
STAGES: list[tuple[str, str | StageRunner]] = [
    ("plan", "plan"), ("approve", "approve"), ("collect", "collect"), ("extract", "extract"), ("draft", "draft"),
    ("verify", "verify"), ("repair", "repair"), ("summarize", "summarize"), ("verify_summary", "verify_summary"),
    ("complete", "finalize"),
]
SHUTDOWN = "Interrupted: the API shut down while this run was in progress"


class StageNotBuilt(RuntimeError):
    """A stage module is still a NotImplementedError stub."""


async def start_run(store: Store, vault: Vault, settings: Settings, req: m.CreateRunRequest) -> RunContext:
    """Create the run, emit run.created, save, and start its pipeline task."""
    state = store.create_run(req)
    ctx = RunContext.create(state, store, vault, settings)
    ctx.emit("run.created", {"run": state.run}, message=f"Run created for {state.run.company}", stage="plan")
    await store.save_async(state)
    ctx.task = asyncio.create_task(run_pipeline(ctx), name=f"pipeline-{state.run.run_id}")
    return ctx


async def run_pipeline(ctx: RunContext) -> None:
    """Run every stage's `run(ctx)` in order (one asyncio task per run). Never raises, except cancellation."""
    try:
        for stage, module in STAGES:
            ctx.check_stop()
            ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
            gates.spawn_bobby(ctx)                  # once: the first agent of the run
            await _run_stage(ctx, stage, module)
            if stage in gates.GATE_STAGES:
                await _gate(ctx, stage)
            await ctx.store.save_async(ctx.state)
        ctx.check_stop()
        gates.finish_bobby(ctx)
        ctx.emit("run.status", {"status": "done", "stage": "complete"}, stage="complete", message="Run done")
        ctx.emit("run.completed", {}, stage="complete", message=f"Run complete for {ctx.state.run.company}")
    except StopRequested as e:
        _on_stop(ctx, str(e))
    except asyncio.CancelledError:
        if ctx.stopped():
            _on_stop(ctx, ctx.state.run.stop_reason or "Stopped by user")
        else:
            _on_fail(ctx, SHUTDOWN)
        ctx.store.save(ctx.state)
        raise
    except Exception as e:
        if ctx.stopped():
            _on_stop(ctx, ctx.state.run.stop_reason or "Stopped by user")
        else:
            log.exception("run %s failed at %s", ctx.run_id, ctx.state.run.stage)
            _on_fail(ctx, describe(e))
    await ctx.store.save_async(ctx.state)


async def _run_stage(ctx: RunContext, stage: str, module: str | StageRunner) -> None:
    """Import the stage at run time, so a broken or unfinished stage fails one run, not the API."""
    try:
        if callable(module):
            await module(ctx)
            return
        mod = importlib.import_module(f"{__package__}.stages.{module}")
        await mod.run(ctx)
    except NotImplementedError as e:
        raise StageNotBuilt(f"stage {stage} is not built yet ({e or 'stub'})") from e


async def _gate(ctx: RunContext, stage: str) -> None:
    """Bobby's call after a gate stage. STOP still stops the run. Any other error is logged and the run
    proceeds: Bobby advises, code decides, so he can never fail a run."""
    try:
        await gates.after_stage(ctx, stage)
    except (StopRequested, asyncio.CancelledError):
        raise
    except Exception as e:
        ctx.check_stop()
        log.exception("gate after %s failed for %s", stage, ctx.run_id)
        if gates.bobby(ctx) is not None:
            ctx.emit("agent.log", m.AgentLogData(text=f"Gate after {stage} failed ({describe(e)}). The run proceeds."),
                     agent_id=gates.BOBBY_ID, level="warn", message=f"bobby: gate after {stage} failed, proceeding")


def describe(e: BaseException) -> str:
    if isinstance(e, StageNotBuilt):
        return str(e)
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _on_stop(ctx: RunContext, reason: str) -> None:
    """Emit the stop once (POST /stop usually did already), then stop the agents still marked busy."""
    state = ctx.state
    stage = state.run.stage
    if state.run.status != "stopped":
        state.kill_switch = True
        state.run.stop_reason = state.run.stop_reason or reason
        gates.finish_bobby(ctx, "stopped")          # Bobby ends with the run, before its last events
        ctx.emit("run.status", {"status": "stopped", "stage": stage}, stage=stage, level="warn",
                 message="Kill switch on")
        ctx.emit("run.stopped", {"reason": state.run.stop_reason}, stage=stage, level="warn",
                 message=f"Run stopped: {state.run.stop_reason}")
    _settle_agents(ctx, "stopped")


def _on_fail(ctx: RunContext, error: str) -> None:
    state = ctx.state
    if state.run.status in ("done", "stopped", "failed"):
        return
    stage = state.run.stage
    gates.finish_bobby(ctx, "failed")               # Bobby ends with the run, before its last events
    ctx.emit("run.status", {"status": "failed", "stage": stage}, stage=stage, level="error",
             message=f"Run failed at {stage}")
    ctx.emit("run.failed", {"error": error}, stage=stage, level="error", message=f"Run failed: {error}")
    _settle_agents(ctx, "failed")


def _settle_agents(ctx: RunContext, status: str) -> None:
    """Agents still queued or working when the run ends get a final status, so the swarm view settles."""
    for agent in list(ctx.state.agents.values()):
        if agent.status not in ("queued", "working"):
            continue
        agent.status = status
        agent.finished_at = now_iso()
        ctx.emit("agent.status", {"agent_id": agent.agent_id, "status": status, "task": agent.task,
                                  "usage": agent.usage}, agent_id=agent.agent_id, level="warn",
                 message=f"{agent.agent_id} {status}")
