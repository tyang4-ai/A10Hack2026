"""Per-run context handed to every stage: state, store, vault, gateway, settings, plus emit, next_id and check_stop."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .config import Settings
from .contract import m

if TYPE_CHECKING:
    from .gateway import Gateway
    from .store import Store
    from .store.state import RunState
    from .store.vault import Vault


class StopRequested(Exception):
    """Raised by RunContext.check_stop when the kill switch is on. The orchestrator turns it into `stopped`."""


@dataclass
class RunContext:
    state: RunState
    store: Store
    vault: Vault
    gateway: Gateway
    settings: Settings
    approval: asyncio.Event                      # set by POST /approval

    def emit(self, type: str, data: dict | BaseModel, *, message: str, stage: str | None = None,
             agent_id: str | None = None, level: str = "info") -> m.Event:
        """Validate against the contract event union, assign seq and ts, append, update Run.counts and last_seq."""
        raise NotImplementedError("B-store-api")

    def next_id(self, prefix: str) -> str:
        """Next contract id for this run from state.ids: next_id("D") gives D-001."""
        raise NotImplementedError("B-store-api")

    def check_stop(self) -> None:
        """Raise StopRequested when the kill switch is on."""
        raise NotImplementedError("B-store-api")
