"""Delete a finished run's own data (v0.3.1, DELETE /runs/{run_id}). Owned by B-delete-pace.

Only a run that is done, stopped or failed can go. A recorded replay can't (its files are in the repo), and
neither can a run listed in PROTECTED_RUNS (the demo run). What goes: the run's state record, its event rows,
its vault folder and its in-memory state and context, so it leaves GET /runs and GET /runs/{id} answers 404.
What stays: the shared caches (cache/docs/, cache/models/, archive/), which other runs reuse.

The Store does the deleting in the fail-safe order (vault, events, then the record), so a delete that fails
half way leaves the run listed and the same call finishes it later.
"""
from __future__ import annotations

from typing import Any

from .store.state import TERMINAL_STATUSES

NOTE = "Run data removed. The shared document cache and recorded model answers stay."


class CannotDelete(Exception):
    """The run is running, a replay, or protected."""


def check_deletable(state: Any, protected: list[str]) -> None:
    """Raise CannotDelete with the reason the API shows (409), or return when the run may go."""
    run = state.run
    if run.mode == "replay":
        raise CannotDelete("A recorded replay can't be deleted")
    if run.run_id in protected:
        raise CannotDelete(f"{run.run_id} is protected (the demo run)")
    if run.status not in TERMINAL_STATUSES:
        raise CannotDelete(f"Run is {run.status}. Stop it before deleting it")


async def delete_run(rt: Any, state: Any) -> dict:
    """Remove the run's own data and return the RunDeleted shape. Raises CannotDelete (the API answers 409)."""
    run_id = state.run.run_id
    check_deletable(state, list(rt.settings.protected_runs))
    ctx = rt.contexts.get(run_id)
    if ctx is not None:
        await ctx.close()                     # a chat reply or a Bobby comment must not write after the delete
        check_deletable(state, list(rt.settings.protected_runs))   # still finished once its tasks ended
    events, files = await rt.store.remove_run(run_id)
    rt.contexts.pop(run_id, None)
    from . import docimage

    docimage.forget_run(run_id)               # its rendered pages and PDF bytes leave the image cache too
    return {"run_id": run_id, "events": events, "vault_files": files, "note": NOTE}
