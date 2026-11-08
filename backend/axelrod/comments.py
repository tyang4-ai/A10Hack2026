"""Analyst comments go to Bobby (docs/BACKEND_V2.md step 5, CONTRACT.md 14).

POST /comments stores the comment, emits comment.created and schedules on_comment as a side task. Bobby
reads the comment with its target (an item or a brief sentence) and the target's evidence, then makes one
call (agents/bobby.py comment_call): rework the goon that produced the target, with the comment turned into
instructions (rework.start_rerun, recheck on, so a finished run reopens and completes again), or answer
without a change. Code decides who the producing goon is and whether it can be rerun, never Bobby. Either
way the comment ends with comment.resolved, his answer in `response` (and what changed), plus an
agent.decision with `comment_id`.

A stopped run rejects comments before they get here, and STOP rejects the open ones. With Bobby stopped by
the analyst, nobody acts on comments: they resolve as rejected with that reason.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from . import gates
from .agents.base import AgentOutputError
from .contract import m
from .gateway import Blocked
from .ids import now_iso
from .runtime import InvalidState, StopRequested

if TYPE_CHECKING:
    from .rework import Outcome
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

EVIDENCE_LIMIT = 12
OFF = ("Bobby was stopped by the analyst, so his calls are off and nobody acts on comments. "
       "Rerun the goon yourself to apply this one.")


# ---------------------------------------------------------------------------
# Who produced the target, and what Bobby reads
# ---------------------------------------------------------------------------


def _latest(state: RunState, role: str) -> str | None:
    """The newest agent of a role (agents are kept in spawn order)."""
    found = [a.agent_id for a in state.agents.values() if a.role == role]
    return found[-1] if found else None


def producing_goon(state: RunState, comment: m.Comment) -> tuple[str | None, list[str] | None, str]:
    """(goon id, focus, why none). Code picks the goon from the target, never Bobby."""
    tid = comment.target_id
    if comment.target_type == "summary_sentence":
        goon = _latest(state, "summarizer")
        return (goon, [tid] if tid else None, "" if goon else "no summarizer wrote the brief yet")
    if comment.target_type != "item" or tid not in state.items:
        return None, None, f"a comment on a {comment.target_type.replace('_', ' ')} has no goon behind it"
    item = state.items[tid]
    aid = getattr(item, "agent_id", None)
    if item.kind == "chart":            # charts are built by code from a drafter's refs
        refs = [r.split(":")[0] for r in item.refs]
        aid = next((getattr(state.items[r], "agent_id", None) for r in refs
                    if r in state.items and state.items[r].kind == "conclusion"), None)
    if not aid or aid not in state.agents:
        return None, None, f"no goon on record produced {tid}"
    return aid, None, ""


def _view(state: RunState, comment: m.Comment) -> tuple[str, list[dict]]:
    """The target as text and its evidence, short."""
    from .agents import summarizer
    from .stages import summarize, verify

    tid = comment.target_id
    if comment.target_type == "summary_sentence":
        s = summarize.find_sentence(state.summary, tid or "")
        if s is None:
            return f"Target: brief sentence {tid} (not in the brief any more)", []
        ev = summarizer.evidence_for(s.derived_from, state.items, state.holes, state.rollup, limit=EVIDENCE_LIMIT)
        return (f"Target: brief sentence {tid} ({s.mode}, {s.verification.status}): {s.text}\n"
                f"Cites: {', '.join(s.derived_from)}"), ev
    if comment.target_type == "item" and tid in state.items:
        item = state.items[tid]
        if item.kind == "data":
            return (f"Target: data item {tid} ({item.verification.status})", [verify.data_view(item)])
        head = (f"Target: {item.kind} {tid} ({item.verification.status}): "
                + json.dumps(verify.claim_view(item), ensure_ascii=False, default=str))
        return head, verify.evidence_view(state, item)[:EVIDENCE_LIMIT]
    return f"Target: the {comment.target_type.replace('_', ' ')}" + (f" {tid}" if tid else ""), []


def build_context(ctx: RunContext, comment: m.Comment, goon: str | None, why_none: str) -> str:
    st = ctx.state
    head, ev = _view(st, comment)
    _, _, left = gates.budget_left(ctx)
    agent = st.agents.get(goon) if goon else None
    who = (f"Producing goon: {goon} ({agent.role}, {agent.task})" if agent is not None
           else f"Producing goon: none ({why_none})")
    lines = [f"Comment: {' '.join(comment.text.split())}", head]
    if ev:
        lines.append("Evidence: " + json.dumps(ev, ensure_ascii=False, default=str))
    lines += [who, f"Budget left: ${left:.2f}."]
    return "\n".join(lines)


def can_rework(ctx: RunContext, goon: str | None, focus: list[str] | None) -> str | None:
    """None when code would send this goon back, else why not."""
    from . import rework

    if goon is None:
        return "there is no goon to send back"
    agent = ctx.state.agents[goon]
    if agent.status == "stopped":
        return f"{goon} was stopped by the analyst"
    try:
        rework.check_rerunnable(ctx.state, agent)
        rework.plan_for(ctx, agent, focus)
    except InvalidState as e:
        return str(e)
    _, _, left = gates.budget_left(ctx)
    cost = rework.RESERVE_USD.get(agent.role, 0.5)
    if cost > left:
        return f"the budget left (${left:.2f}) can't cover a {agent.role} rerun (${cost:.2f})"
    return None


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def resolve(ctx: RunContext, comment: m.Comment, status: str, response: str,
            changed: list[str] | None = None) -> None:
    """comment.resolved, once. A comment STOP already rejected stays rejected."""
    if comment.status in ("applied", "rejected"):
        return
    comment.status, comment.response, comment.resolved_at = status, response.strip(), now_iso()
    comment.changed_item_ids = list(changed or [])
    ctx.emit("comment.resolved", m.CommentEventData(comment=comment.model_copy(deep=True)),
             level="info" if status == "applied" else "warn",
             message=f"Comment {comment.comment_id} {status}: {comment.response[:160]}")


def _changes(state: RunState, out: Outcome) -> str:
    if out.note and not out.changed and not out.sentences:
        return f" {out.note[0].upper()}{out.note[1:]}."
    parts = [f"{i} is {state.items[i].verification.status}" for i in out.changed if i in state.items]
    parts += [f"{s} is in the brief" for s in out.sentences[:3] if not out.brief]
    if out.rechecked:
        parts.append(f"{len(out.rechecked)} item(s) citing it checked again")
    return (" Now " + ", ".join(parts[:6]) + ".") if parts else " Nothing changed."


async def on_comment(ctx: RunContext, comment: m.Comment) -> None:
    """Bobby acts on one stored comment, as a side task."""
    from . import rework
    from .agents import bobby as bobby_agent

    state = ctx.state
    if comment.status != "queued" or state.run.status == "stopped":
        return
    if gates.bobby(ctx) is None or gates.bobby_off(ctx):
        resolve(ctx, comment, "rejected", OFF)
        await ctx.store.save_async(state)
        return
    comment.status = "working"
    goon, focus, why_none = producing_goon(state, comment)
    blocked_why = can_rework(ctx, goon, focus)
    try:
        call = await bobby_agent.comment_call(ctx, build_context(ctx, comment, goon if not blocked_why else None,
                                                                 why_none or blocked_why or ""))
    except StopRequested:
        return                                   # STOP rejected the open comments
    except (Blocked, AgentOutputError) as e:
        resolve(ctx, comment, "rejected", OFF if gates.bobby_off(ctx) else f"Bobby could not answer: {rework.why(e)}.")
        await ctx.store.save_async(state)
        return
    if comment.status != "working":
        return
    answer = bobby_agent.clip(call.answer, 600) or "Noted."
    if call.action == "rework" and blocked_why is None:
        instructions = bobby_agent.clip(call.instructions or comment.text)

        async def done(out: Outcome) -> None:
            resolve(ctx, comment, "applied", answer + _changes(ctx.state, out), out.changed + out.sentences)

        try:
            agent, task = rework.start_rerun(ctx, goon, instructions, True, by="orchestrator", focus=focus,
                                             after=done)
        except InvalidState as e:
            blocked_why = str(e)
        else:
            gates.record(ctx, state.run.stage, "rework", f"{answer} Sent {goon} back as {agent.agent_id}.",
                         [m.ReworkOrder(agent_id=goon, instructions=instructions)], comment_id=comment.comment_id)
            try:
                await task
            except StopRequested:
                return
            except Exception as e:  # noqa: BLE001 - the comment still resolves, with what went wrong
                log.exception("rework for %s failed", comment.comment_id)
                resolve(ctx, comment, "applied", f"{answer} The rerun of {goon} failed: {rework.why(e)}.")
            await ctx.store.save_async(state)
            return
    if call.action == "rework":
        answer += f" Code sent nobody back: {blocked_why}."
    gates.record(ctx, state.run.stage, "proceed", answer, comment_id=comment.comment_id)
    resolve(ctx, comment, "applied", answer)
    await ctx.store.save_async(state)
