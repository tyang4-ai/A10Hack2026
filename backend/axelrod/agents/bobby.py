"""Bobby, the orchestrator (CONTRACT.md 14, docs/BACKEND_V2.md steps 4 and 5). Built by V3-rework.

Bobby makes the big calls and never does the legwork. He runs on the large model, as agent "bobby" (spawned
by gates.spawn_bobby), and asks through AgentRunner.attach(ctx, "bobby"), so every call goes through the
gateway under his own role scope, the budget and the kill switch. His prompts stay short (a gate should
cost cents and a few seconds): a code-built digest in, one forced tool call out.

V3-rework adds here: the output model for a gate call (proceed, rework with at most 3 goons once each and
instructions for each, or escalate with a note for the analyst), the output model for a comment (rework the
producing goon with the comment as instructions, or answer without a change), their prompts and the fakes
registered for FAKE_MODELS. Code validates every call before anything happens.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..contract import m
    from ..runtime import RunContext

PLAN_ID = "V3-rework"


async def gate_call(ctx: RunContext, stage: str, digest: str) -> m.AgentDecisionData:
    """Bobby's call on one stage digest. Raises NotImplementedError until V3-rework."""
    raise NotImplementedError(PLAN_ID)
