"""Rerun from a step: fork a finished, stopped or failed run (CONTRACT.md 13, docs/BACKEND_V2.md step 11). Built by V8-fork.

The route (POST /runs/{id}/rerun) has already checked that the parent is done, stopped or failed, that
from_stage isn't complete and that the parent reached it. fork_run will: build a new RunState
(store.fresh_run_id(), Run.parent_run_id, rerun_from, instructions) holding the parent's state up to
from_stage (CONTRACT.md 13 table), with the same item ids so citations resolve; give it its own policy (the
parent's rules at that point) and its own event log from seq 1, whose run.created carries the parent
fields; adopt it with store.put; and start its pipeline from from_stage. AgentRunner passes Run.instructions
to every agent the fork spawns (judges aside, since they are checks). The parent never changes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .contract import m
    from .runtime import RunContext
    from .store import Store
    from .store.state import RunState
    from .store.vault import Vault

PLAN_ID = "V8-fork"


async def fork_run(store: Store, vault: Vault, settings: Settings, parent_state: RunState,
                   req: m.RerunRequest) -> RunContext:
    """Create the forked run and start it. Returns its context. Raises NotImplementedError until V8-fork."""
    raise NotImplementedError(PLAN_ID)
