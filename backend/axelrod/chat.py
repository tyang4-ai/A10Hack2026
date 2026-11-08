"""Chat with one agent (CONTRACT.md 13, docs/BACKEND_V2.md step 10). Built by V7-chat.

Read only: chat never changes the run, the report or the brief, and replies are not checked, so the UI
labels them. Messages live in RunState.messages (ids AM-001 from ctx.next_id("AM")), oldest first.

send_message will: store the analyst's message (status sent), emit agent.message, and answer in a side task
(ctx.spawn) from that agent's own role and model, through the gateway (AgentRunner.attach(ctx, agent_id,
track_status=False)), with its task, inputs and transcript as context, in about 120 words. Bobby answers
about his calls (RunState.decisions). Then two more agent.message events: the reply (author agent,
reply_to set) and the analyst's message again with status answered (or failed). The route has already
checked that the agent exists and the run is not stopped.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext
    from .store.state import RunState

PLAN_ID = "V7-chat"


async def send_message(ctx: RunContext, agent_id: str, text: str) -> m.AgentMessage:
    """Store the analyst's message, start the reply, return the message. NotImplementedError until V7-chat."""
    raise NotImplementedError(PLAN_ID)


def list_messages(state: RunState, agent_id: str) -> m.AgentMessageList:
    """One agent's chat, oldest first. Raises NotImplementedError until V7-chat."""
    raise NotImplementedError(PLAN_ID)
