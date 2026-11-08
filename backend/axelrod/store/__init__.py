"""Run persistence: Store keeps RunState as local JSON or in DynamoDB plus S3. RunState and Vault are re-exported."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..contract import m
from .state import RunState
from .vault import Vault

__all__ = ["Store", "RunState", "Vault"]


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_run(self, req: m.CreateRunRequest) -> RunState:
        raise NotImplementedError("B-store-api")

    def get(self, run_id: str) -> RunState:
        """Raises KeyError for an unknown run (the API turns it into 404)."""
        raise NotImplementedError("B-store-api")

    def list_runs(self) -> list[m.Run]:
        raise NotImplementedError("B-store-api")

    def save(self, state: RunState) -> None:
        raise NotImplementedError("B-store-api")

    def load_all(self) -> None:
        """At startup: every stored run, plus backend/data/replay/* as mode "replay"."""
        raise NotImplementedError("B-store-api")

    def export_run(self, run_id: str, dest: Path) -> None:
        """Write a finished run (state, events, page texts, never PDFs) to dest."""
        raise NotImplementedError("B-store-api")
