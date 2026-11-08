"""Event log: gapless seq per run, the GET /events?after&limit paging rules (CONTRACT.md section 4),
and the DynamoDB event writes in aws mode."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..contract import m

if TYPE_CHECKING:
    from .state import RunState


def events_page(state: RunState, after: int = 0, limit: int = 500) -> m.EventsPage:
    """Events with seq > after, at most limit (default 500, max 2000), plus last_seq, run_status and stage."""
    raise NotImplementedError("B-store-api")
