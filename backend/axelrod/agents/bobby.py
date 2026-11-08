"""Bobby, the orchestrator (CONTRACT.md 14, docs/BACKEND_V2.md steps 4 and 5).

Bobby makes the big calls and never does the legwork. He runs on the large model, as agent "bobby" (spawned
by gates.spawn_bobby), and asks through AgentRunner.attach(ctx, "bobby"), so every call goes through the
gateway under his own role scope, the budget and the kill switch. His prompts stay short (a gate should
cost cents and a few seconds): a code-built digest in, one forced tool call out.

Two calls live here: gate_call (proceed, rework at most 3 goons once each with instructions, or escalate
with a note for the analyst) and comment_call (rework the goon that produced the target, with the comment
turned into instructions, or answer without a change). Code validates both before anything happens
(gates.validate_reworks, comments.on_comment), and only code checks ever decide pass or fail.

The fakes (FAKE_MODELS=1) are simulated: a gate always proceeds, a comment with a producing goon is sent back
with the comment as instructions, any other comment is answered. Their texts say "Simulated".
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from .base import AgentRunner, register_fake, tool_name

if TYPE_CHECKING:
    from ..runtime import RunContext

BOBBY_ID = "bobby"
GATE_MAX_TOKENS = 700          # a call is a few sentences and at most 3 short instructions
COMMENT_MAX_TOKENS = 600
MAX_INSTRUCTIONS = 1000        # characters per rework order, clipped by code


# ---------------------------------------------------------------------------
# Output models (flat, no contract ids beyond the agent ids he picks from the digest)
# ---------------------------------------------------------------------------


class GateRework(BaseModel):
    """One goon sent back, with what to fix."""

    agent_id: str = Field(description="A goon id copied from the digest's list, e.g. 'extractor-07'.")
    instructions: str = Field(description="One to three plain sentences: what to redo and what to fix.")


class GateCall(BaseModel):
    """Your one call at this stage gate."""

    TOOL_NAME: ClassVar[str] = "bobby_gate_call"
    decision: Literal["proceed", "rework", "escalate"]
    summary: str = Field(description="One or two plain sentences for the log. For escalate, the note for the analyst.")
    reworks: list[GateRework] = Field(default_factory=list, description="Only for rework: at most 3 goons, each once.")

    @field_validator("reworks", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return [] if v is None else v


class CommentCall(BaseModel):
    """Your answer to one analyst comment."""

    TOOL_NAME: ClassVar[str] = "bobby_comment_call"
    action: Literal["rework", "answer"]
    answer: str = Field(description="One to three plain sentences for the analyst.")
    instructions: str = Field(default="", description="Only for rework: the comment turned into concrete "
                                                      "instructions for the goon that produced the target.")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GATE_SYSTEM = """You are Bobby, the orchestrator of a research harness that screens one public company. Your goons fetch, extract, draft, verify and repair. Code checks every quote and number, and only code decides pass or fail.

At each stage gate you read a short digest of the stage and make one call:
- proceed: the stage is good enough. This is the usual call. Failures that are few, or already declared as gaps, are not a reason to rework.
- rework: send back at most 3 goons from the digest's list, each once, with short instructions that name the page, check or number to fix. Use it only when a rerun can plausibly fix a failure the digest shows.
- escalate: something only the analyst can settle, for example a criterion with no data at all. Put the note in summary. The run carries on.

You never approve, reject or revoke sources, change the policy, or mark anything as passed. Never invent agent ids. Keep summary to one or two plain sentences."""

COMMENT_SYSTEM = """You are Bobby, the orchestrator of a research harness that screens one public company. The analyst left a comment on one part of the report or the brief. You see the comment, its target, the target's evidence and the goon that produced it.

Make one call:
- rework: send that goon back, with instructions that turn the comment into a concrete change the evidence can support. Code checks whatever it produces, and only code decides pass or fail.
- answer: reply without changing anything, when the comment is a question, is already true of the target, asks for something the evidence cannot support, or there is no goon to send back.

answer is always one to three plain sentences for the analyst. You never approve or revoke sources or mark anything as passed."""


def gate_user(digest: str) -> str:
    return f"<digest>\n{digest.strip()}\n</digest>\n\nMake your call with {GateCall.TOOL_NAME}."


def comment_user(context: str) -> str:
    return f"<comment_context>\n{context.strip()}\n</comment_context>\n\nAnswer with {CommentCall.TOOL_NAME}."


# ---------------------------------------------------------------------------
# The calls
# ---------------------------------------------------------------------------


def runner(ctx: RunContext) -> AgentRunner:
    """Bobby's runner. While the run goes on he is working and his usage shows in agent.status. After it ends
    (a comment on a finished run) his status stays as it is."""
    agent = ctx.state.agents[BOBBY_ID]
    return AgentRunner.attach(ctx, BOBBY_ID, "large", track_status=agent.status == "working")


async def gate_call(ctx: RunContext, stage: str, digest: str) -> GateCall:
    """Bobby's raw call on one stage digest. gates.after_stage validates it. Raises Blocked, AgentOutputError
    or StopRequested like any agent call."""
    out = await runner(ctx).ask(GATE_SYSTEM, gate_user(digest), GateCall, max_tokens=GATE_MAX_TOKENS)
    return out  # type: ignore[return-value]


async def comment_call(ctx: RunContext, context: str) -> CommentCall:
    """Bobby's raw call on one comment. comments.on_comment validates it."""
    out = await runner(ctx).ask(COMMENT_SYSTEM, comment_user(context), CommentCall, max_tokens=COMMENT_MAX_TOKENS)
    return out  # type: ignore[return-value]


def clip(text: str, n: int = MAX_INSTRUCTIONS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Fakes (simulated)
# ---------------------------------------------------------------------------

_STAGE = re.compile(r"Stage gate: after (\w+)")
_COUNTS = re.compile(r"^(Data items|Report items|Brief):.*$", re.MULTILINE)
_GOON = re.compile(r"^Producing goon: ([\w.\-]+)", re.MULTILINE)
_COMMENT = re.compile(r"^Comment: (.+)$", re.MULTILINE)


def fake_gate(system: str, user: str, schema: dict) -> dict:
    """Simulated Bobby: always proceeds, and says what he saw."""
    stage = _STAGE.search(user)
    counts = _COUNTS.search(user)
    seen = f" {counts.group(0)}." if counts else ""
    return {"decision": "proceed", "reworks": [],
            "summary": f"Simulated (FAKE_MODELS): {stage.group(1) if stage else 'stage'} gate checked.{seen} Proceeding."}


def fake_comment(system: str, user: str, schema: dict) -> dict:
    """Simulated Bobby: sends the producing goon back with the comment as instructions, else answers."""
    goon = _GOON.search(user)
    text = _COMMENT.search(user)
    comment = text.group(1).strip() if text else "the analyst's comment"
    if goon and goon.group(1) != "none":
        return {"action": "rework", "instructions": comment,
                "answer": f"Simulated (FAKE_MODELS): sent {goon.group(1)} back with your comment as instructions."}
    return {"action": "answer", "instructions": "",
            "answer": "Simulated (FAKE_MODELS): noted. No goon produced this target, so nothing changes."}


def register_fakes() -> None:
    register_fake(tool_name(GateCall), fake_gate)
    register_fake(tool_name(CommentCall), fake_comment)


register_fakes()
