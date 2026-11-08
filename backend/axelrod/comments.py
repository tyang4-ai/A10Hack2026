"""Analyst comments go to Bobby (docs/BACKEND_V2.md step 5).

POST /comments stores the comment, emits comment.created and schedules on_comment as a side task. V3-rework
fills it: Bobby reads the comment with its target item or sentence and that target's citations, then either
reruns the goon that produced it with the comment as instructions (rework.rerun_agent, recheck on) or
answers without a change. Either way the comment ends with comment.resolved carrying his answer in
`response`, and an agent.decision with `comment_id`. A stopped run rejects comments before they get here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext


async def on_comment(ctx: RunContext, comment: m.Comment) -> None:
    """Bobby acts on one stored comment. A no-op until V3-rework, so the comment stays queued."""
    return None
