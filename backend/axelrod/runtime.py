"""Per-run context handed to every stage: state, store, vault, gateway, settings, plus emit, next_id and check_stop.

One RunContext per run, shared by the pipeline task and the API handlers, so they emit into the same
gapless event log.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .config import Settings
from .contract import m
from .store.events import lock_for

if TYPE_CHECKING:
    from .gateway import Gateway
    from .store import Store
    from .store.state import RunState
    from .store.vault import Vault


log = logging.getLogger(__name__)


class StopRequested(Exception):
    """Raised by RunContext.check_stop when the kill switch is on. The orchestrator turns it into `stopped`."""


class InvalidState(Exception):
    """A control action the run's state doesn't allow right now (rerun a fetcher, resume a running run).
    Control modules raise it, and the API answers 409 invalid_state with the message."""


@dataclass
class RunContext:
    state: RunState
    store: Store
    vault: Vault
    gateway: Gateway
    settings: Settings
    approval: asyncio.Event = field(default_factory=asyncio.Event)   # set by POST /approval (and by POST /stop)
    task: asyncio.Task | None = None                                 # the pipeline task, kept so it isn't collected
    background: set[asyncio.Task] = field(default_factory=set)        # spawn()ed side tasks (comments, reruns, chat)

    @classmethod
    def create(cls, state: RunState, store: Store, vault: Vault, settings: Settings,
               gateway: Gateway | None = None) -> RunContext:
        """Build the context and its Gateway (Gateway(ctx) needs the context first)."""
        ctx = cls(state=state, store=store, vault=vault, gateway=None, settings=settings)  # type: ignore[arg-type]
        if gateway is None:
            from .gateway import Gateway

            gateway = Gateway(ctx)
        ctx.gateway = gateway
        return ctx

    @property
    def run_id(self) -> str:
        return self.state.run.run_id

    def emit(self, type: str, data: dict | BaseModel, *, message: str, stage: str | None = None,
             agent_id: str | None = None, level: str = "info") -> m.Event:
        """Validate against the contract event union, assign seq and ts, append, update Run.counts and last_seq.
        Thread-safe: seq is assigned under the run's lock."""
        return self.store.append(self.state, type, data, message=message, stage=stage, agent_id=agent_id,
                                 level=level)

    def next_id(self, prefix: str) -> str:
        """Next contract id for this run from state.ids: next_id("D") gives D-001."""
        with lock_for(self.run_id):
            return self.state.ids.next(prefix)

    def stopped(self) -> bool:
        """True once POST /stop ran, or the policy's kill switch is on."""
        policy = self.state.policy
        return self.state.kill_switch or self.state.run.status == "stopped" or bool(policy and policy.kill_switch)

    def check_stop(self) -> None:
        """Raise StopRequested when the kill switch is on."""
        if self.stopped():
            raise StopRequested(self.state.run.stop_reason or "Stopped by user")

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """Run a side task for this run (a comment for Bobby, a rerun, a chat reply) next to the pipeline.
        The context keeps a reference until it ends, so it is never garbage collected mid-flight, and an
        exception is logged instead of vanishing. A StopRequested just ends the task."""
        task = asyncio.get_running_loop().create_task(coro, name=f"{name}-{self.run_id}")
        self.background.add(task)
        task.add_done_callback(self._side_task_done)
        return task

    def _side_task_done(self, task: asyncio.Task) -> None:
        self.background.discard(task)
        if task.cancelled():
            return
        err = task.exception()
        if err is not None and not isinstance(err, StopRequested):
            log.error("side task %s failed", task.get_name(), exc_info=err)
