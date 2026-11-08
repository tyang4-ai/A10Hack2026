"""Delete a finished run's own data (v0.3.1). Owned by B-delete."""
from __future__ import annotations

from typing import Any


class CannotDelete(Exception):
    """The run is running, a replay, or protected."""


async def delete_run(rt: Any, state: Any) -> dict:
    raise CannotDelete("not built yet: deleting runs")
