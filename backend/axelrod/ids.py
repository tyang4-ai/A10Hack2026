"""Ids and timestamps, always made by code: run ids, contract ids (N-01, D-001, REQ-0001) and ISO 8601 UTC times."""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import get_args

from pydantic import BaseModel

from .contract import m

_RUN_ALPHABET = string.ascii_lowercase + string.digits

# Digits per prefix. Agent ids are <role>-<nn> ("extractor-07"), so every agent role gets 2.
DEFAULT_WIDTH = 3
WIDTHS: dict[str, int] = {"N": 2, "HOLE": 2, "REQ": 4, **{role: 2 for role in get_args(m.AgentRole)}}


def now_iso() -> str:
    """Current UTC time with milliseconds and a trailing Z: 2026-11-08T03:00:00.000Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    """run_YYYYMMDD_<6 lowercase alphanumerics>, dated in UTC."""
    suffix = "".join(secrets.choice(_RUN_ALPHABET) for _ in range(6))
    return f"run_{datetime.now(timezone.utc):%Y%m%d}_{suffix}"


class IdAllocator(BaseModel):
    """Per-run counters, one per prefix. Lives in RunState, so ids keep counting after a restart."""

    counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        """N: 2 digits, HOLE: 2, REQ: 4, agent roles: 2, everything else 3. "D" and "D-" mean the same."""
        prefix = prefix.rstrip("-")
        if not prefix:
            raise ValueError("id prefix is empty")
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}-{n:0{WIDTHS.get(prefix, DEFAULT_WIDTH)}d}"
