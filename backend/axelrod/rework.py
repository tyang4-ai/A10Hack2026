"""Rerun one goon in the same run, with instructions (CONTRACT.md 14, docs/BACKEND_V2.md step 3).

The analyst (POST /runs/{id}/agents/{agent_id}/rerun, by="analyst") and Bobby (at a gate or on a comment,
by="orchestrator") share this one machine. rerun_agent registers the new agent `<root>.<k>` (extractor-03.2,
then extractor-03.3) with the same role, task and targets, parent_agent_id, the instructions and rerun_by,
returns it at once and does the work in a side task (ctx.spawn), the way the mock answers 201 at once.

What each role redoes, through the gateway under the current policy:
- extractor: its page again (stages/extract.py). Code matches each new item to the old one it replaces by the
  evidence span, then period and number, then statement. A JSON reader has no model and can't be rerun.
- drafter: its criteria group again (stages/draft.py), with the stage's pre-check and one revision. A score
  keeps its id by criterion, a conclusion by the data it cites, a chart by its title. Failing new versions
  get the repair stage's two repairs, counted from the rework on.
- summarizer: the whole brief again (draft, then the verify_summary checks), or with `focus` one sentence (an
  analyst comment on it). A one-sentence version that fails its checks never replaces the old sentence.
- judge (verifier-<id>-k): a fresh panel of three judges through stages.verify.judge_claim (or the brief's
  faithfulness panel for an H- sentence). Judges never see instructions: a check is never steered, so the
  instructions stay on the rerun agent and in the attempt note.
- repairer: one more repair of its item (or brief sentence), weaken only, then the checks.
Fetchers and Bobby can't be rerun (409, the route says so first).

New versions keep their ids. The previous version stays in verification.attempts as an attempt with action
"rework" (item.updated reason "rework", then item.verified). Old outputs the rerun gave no new version of are
kept as they are and named in the log. With recheck, everything citing a changed item is checked again
(code checks, then judges where the stage uses them): nearest first, and further only past items whose status
moved. A final report is finalized again (rollup, holes, report.ready), brief sentences citing what changed are
checked again (summary.verified), and a finished run goes back to running and completes again.
Only code checks decide pass or fail.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .agents.base import AgentOutputError, AgentRunner
from .contract import m
from .gateway import Blocked
from .ids import now_iso
from .runtime import InvalidState, StopRequested
from .stages import draft, extract, finalize, repair, summarize, verify as verify_stage, verify_summary
from .store.state import ORCHESTRATOR_ROLE
from .verify import checks

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

PLAN_ID = "V3-rework"
REASON = "rework"
PASSING = ("pass", "fixed")
NOTE_CHARS = 200
JUDGES = 3
RERUNNABLE = ("extractor", "drafter", "summarizer", "verifier", "repairer")
# The most one rerun may cost, by role: its own calls (worst case) and the judges that check what it made.
# Bobby's reworks are held to the budget left with these. The gateway still meters and blocks every real call.
RESERVE_USD = {"extractor": 0.10, "drafter": 0.80, "summarizer": 0.80, "verifier": 0.05, "repairer": 0.15}

_OPEN: dict[str, int] = {}      # run_id -> reruns that reopened a finished run and are still going


# ---------------------------------------------------------------------------
# What a rerun does and what it changed
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one rerun changed."""
    updated: list[str] = field(default_factory=list)     # item ids with a new version
    created: list[str] = field(default_factory=list)     # new item ids
    kept: list[str] = field(default_factory=list)        # the old agent's items the rerun gave no new version of
    sentences: list[str] = field(default_factory=list)   # brief sentences with a new version (or re-judged)
    rechecked: list[str] = field(default_factory=list)   # items and sentences checked again after the change
    brief: bool = False                                  # the whole brief was written again
    note: str = ""                                       # one line on what happened, for logs and comments

    @property
    def changed(self) -> list[str]:
        return self.updated + self.created


@dataclass
class Plan:
    """One rerun's work, worked out before the new agent is registered (so a bad request is a 409)."""
    role: str
    size: Literal["large", "small"] | None
    run: Callable[[RunContext, AgentRunner, Plan], Awaitable[Outcome]]
    old: list[m.Item] = field(default_factory=list)      # the old outputs the rerun replaces
    job: Any = None                                      # extractor: the PageJob
    group: list[str] = field(default_factory=list)       # drafter: the criteria group
    target: str | None = None                            # judge, repairer, one-sentence summarizer: item or H id


def base_task(task: str) -> str:
    """An agent's own task, without the "Rerun of <id>: " prefixes reruns add."""
    while task.startswith("Rerun of ") and ": " in task:
        task = task.split(": ", 1)[1]
    return task


def root_of(agent_id: str) -> str:
    return agent_id.split(".", 1)[0]


def lineage(state: RunState, agent_id: str) -> set[str]:
    """The agent, the one it reran and every rerun of the same root."""
    root = root_of(agent_id)
    return {a for a in state.agents if a == root or a.startswith(root + ".")}


def new_agent_id(state: RunState, agent_id: str) -> str:
    """<root>.<k>, k counting reruns of the root from 2."""
    root = root_of(agent_id)
    ks = [int(a.split(".", 1)[1]) for a in lineage(state, agent_id) if "." in a and a.split(".", 1)[1].isdigit()]
    return f"{root}.{max(ks, default=1) + 1}"


def _size(ctx: RunContext, agent: m.Agent) -> Literal["large", "small"] | None:
    return {ctx.settings.model_large: "large", ctx.settings.model_small: "small"}.get(agent.model_id or "")


def _clip(text: str, n: int = NOTE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def why(e: BaseException) -> str:
    if isinstance(e, Blocked):
        return f"blocked by the gateway: {e.reason}"
    first = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return first or type(e).__name__


def _and(ids: list[str], limit: int = 6) -> str:
    shown = ", ".join(ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


# ---------------------------------------------------------------------------
# Planning a rerun (raises InvalidState, so the API answers 409)
# ---------------------------------------------------------------------------


def check_rerunnable(state: RunState, agent: m.Agent) -> None:
    """The states no rerun may start from. The route checks most of them first, Bobby's calls come here too."""
    if state.run.status == "stopped":
        raise InvalidState("Run is stopped. Resume it before rerunning an agent.")
    if agent.role == ORCHESTRATOR_ROLE:
        raise InvalidState("Bobby can't be rerun. Talk to him in the chat instead.")
    if agent.role == "fetcher":
        raise InvalidState("Fetchers can't be rerun: they only fetch the approved sources. "
                           "Rerun the extractor that read the page instead.")
    if agent.status in ("queued", "working"):
        raise InvalidState(f"{agent.agent_id} is still {agent.status}. Stop it first.")
    if agent.role not in RERUNNABLE:
        raise InvalidState(f"A {agent.role} can't be rerun in this build.")


def plan_for(ctx: RunContext, agent: m.Agent, focus: list[str] | None = None) -> Plan:
    """What the rerun of this agent redoes. Raises InvalidState when there is nothing it can redo."""
    planner = {"extractor": _plan_extractor, "drafter": _plan_drafter, "summarizer": _plan_summarizer,
               "verifier": _plan_judge, "repairer": _plan_repairer}[agent.role]
    return planner(ctx, agent, focus)


def _owned(state: RunState, agent_id: str) -> list[m.Item]:
    mine = lineage(state, agent_id)
    return [i for i in state.items.values() if getattr(i, "agent_id", None) in mine]


def _plan_extractor(ctx: RunContext, agent: m.Agent, focus: list[str] | None) -> Plan:
    if agent.model_id is None:
        raise InvalidState(f"{agent.agent_id} reads structured data by code, so there is no model to instruct. "
                           "Its items are exact JSON values.")
    where = extract.page_of_task(base_task(agent.task))
    if where is None:
        raise InvalidState(f"{agent.agent_id} has no page to read again (task: {agent.task!r}).")
    source_id, page = where
    old = [i for i in _owned(ctx.state, agent.agent_id) if i.kind == "data"]
    hint = next((c.doc_id for i in old for c in i.citations if c.role == "evidence"), None)
    try:
        job = extract.rerun_job(ctx.state, source_id, page, hint)
    except ValueError as e:
        raise InvalidState(str(e)) from None
    return Plan("extractor", "small", _rerun_extractor, old=old, job=job)


def _plan_drafter(ctx: RunContext, agent: m.Agent, focus: list[str] | None) -> Plan:
    group = draft.group_of_task(base_task(agent.task))
    if not group:
        raise InvalidState(f"{agent.agent_id} has no criteria group to draft again (task: {agent.task!r}).")
    state = ctx.state
    old = [i for i in _owned(state, agent.agent_id) if i.kind in ("conclusion", "extrapolation")]
    have = {i.criterion for i in old if i.kind == "extrapolation"}
    old += [e for e in state.extrapolations() if e.criterion in group and e.criterion not in have]
    concl = {i.id for i in old if i.kind == "conclusion"}
    shown = {d.id for d in draft.group_items(draft.usable_data(state), group)[0]}
    old += [g for g in state.charts() if {r.split(":")[0] for r in g.refs} & (concl | shown)]
    return Plan("drafter", "large", _rerun_drafter, old=old, group=group)


def _plan_summarizer(ctx: RunContext, agent: m.Agent, focus: list[str] | None) -> Plan:
    if ctx.state.summary is None:
        raise InvalidState("The brief isn't written yet, so there is nothing to rewrite.")
    target = next((f for f in focus or [] if f.startswith("H-")), None)
    if target is None:
        return Plan("summarizer", "large", _rerun_brief)
    if summarize.find_sentence(ctx.state.summary, target) is None:
        raise InvalidState(f"{target} is not in the brief.")
    return Plan("summarizer", "large", _rerun_sentence, target=target)


def _target(agent: m.Agent) -> str | None:
    if agent.target_ids:
        return agent.target_ids[0]
    rest = root_of(agent.agent_id).removeprefix(f"{agent.role}-")
    return rest.rsplit("-", 1)[0] if "-" in rest else None


def _claim_target(ctx: RunContext, agent: m.Agent, what: str) -> str:
    """The item or brief sentence a judge or repairer worked on, if it can be redone."""
    target = _target(agent)
    if target is None:
        raise InvalidState(f"{agent.agent_id} has no target to {what} again.")
    if target.startswith("H-"):
        if summarize.find_sentence(ctx.state.summary, target) is None:
            raise InvalidState(f"{target} is no longer in the brief.")
        return target
    item = ctx.state.items.get(target)
    if item is None:
        raise InvalidState(f"{target} is not in the report.")
    if item.kind not in ("conclusion", "extrapolation"):
        raise InvalidState(f"{target} is a {item.kind} item: code checks it, no judge or repairer works on it.")
    if item.verification.status == "pending":
        raise InvalidState(f"{target} has not been checked yet.")
    return target


def _plan_judge(ctx: RunContext, agent: m.Agent, focus: list[str] | None) -> Plan:
    return Plan("verifier", None, _rerun_judge, target=_claim_target(ctx, agent, "judge"))


def _plan_repairer(ctx: RunContext, agent: m.Agent, focus: list[str] | None) -> Plan:
    target = _claim_target(ctx, agent, "repair")
    return Plan("repairer", _size(ctx, agent) or ("small" if target.startswith("H-") else "large"),
                _rerun_repairer, target=target)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def rerun_agent(ctx: RunContext, agent_id: str, instructions: str, recheck: bool, *,
                      by: Literal["analyst", "orchestrator"] = "analyst", focus: list[str] | None = None) -> m.Agent:
    """Rerun one goon with instructions and return the new agent at once. The work runs in a side task.
    focus (keyword, optional) narrows a summarizer rerun to brief sentence ids (an analyst comment on one)."""
    agent, _ = start_rerun(ctx, agent_id, instructions, recheck, by=by, focus=focus)
    return agent


def start_rerun(ctx: RunContext, agent_id: str, instructions: str, recheck: bool, *,
                by: Literal["analyst", "orchestrator"] = "analyst", focus: list[str] | None = None,
                after: Callable[[Outcome], Awaitable[None]] | None = None) -> tuple[m.Agent, asyncio.Task]:
    """rerun_agent, plus the side task, so a gate or a comment can wait for it. `after` runs when the work and
    the re-checks are done, before a reopened run completes again. Raises InvalidState."""
    state = ctx.state
    old = state.agents.get(agent_id)
    if old is None:
        raise InvalidState(f"No agent {agent_id} in {state.run.run_id}")
    check_rerunnable(state, old)
    text = (instructions or "").strip()
    if not text:
        raise InvalidState("A rerun needs instructions.")
    plan = plan_for(ctx, old, focus)
    reopen = recheck and state.run.status == "done"
    if reopen:
        _reopen(ctx, agent_id)
    runner = AgentRunner(ctx, old.role, f"Rerun of {agent_id}: {base_task(old.task)}", list(old.target_ids),
                         plan.size, agent_id=new_agent_id(state, agent_id), parent_agent_id=agent_id,
                         instructions=text, rerun_by=by)
    task = ctx.spawn(_work(ctx, runner, plan, recheck, reopen, after), name=f"rerun-{runner.agent_id}")
    return runner.agent, task


async def _work(ctx: RunContext, runner: AgentRunner, plan: Plan, recheck: bool, reopen: bool,
                after: Callable[[Outcome], Awaitable[None]] | None) -> Outcome:
    aid = runner.agent_id
    out = Outcome()
    try:
        try:
            with runner:
                out = await plan.run(ctx, runner, plan)
        except (Blocked, AgentOutputError, RuntimeError, ValueError, InvalidState) as e:
            out = Outcome(note=f"{aid} gave no new version ({why(e)})")
            _log(ctx, aid, f"Rerun gave no new version: {why(e)}", level="warn")
        else:
            if recheck:
                await recheck_after(ctx, out, aid)
            out.note = out.note or describe(ctx.state, out)
            _log(ctx, aid, f"Rerun done: {out.note}")
        if after is not None:
            await after(out)
        return out
    finally:
        if reopen:
            await _close(ctx)
        try:
            await ctx.store.save_async(ctx.state)
        except Exception:  # noqa: BLE001 - a save failure is logged, the rerun's events are already out
            log.exception("save after rerun %s failed", aid)


def describe(state: RunState, out: Outcome) -> str:
    """One plain line on what a rerun changed."""
    parts = []
    if out.updated:
        parts.append(f"new version of {_and(out.updated)}")
    if out.created:
        parts.append(f"new {_and(out.created)}")
    if out.brief:
        parts.append(f"the brief written again ({len(out.sentences)} sentences)")
    elif out.sentences:
        parts.append(f"{_and(out.sentences)} in the brief")
    if out.kept:
        parts.append(f"kept {_and(out.kept)} as they were (no new version)")
    if out.rechecked:
        parts.append(f"checked {_and(out.rechecked)} again")
    statuses = [f"{i} {state.items[i].verification.status}" for i in out.changed if i in state.items]
    line = "; ".join(parts) or "nothing changed"
    return line + (f". Now: {_and(statuses)}" if statuses else "")


def _log(ctx: RunContext, agent_id: str, text: str, level: str = "info") -> None:
    agent = ctx.state.agents.get(agent_id)
    ctx.emit("agent.log", m.AgentLogData(text=text, transcript_path=agent.transcript_path if agent else None),
             message=f"{agent_id}: {text}", agent_id=agent_id, level=level)


# ---------------------------------------------------------------------------
# Reopen a finished run, complete it again
# ---------------------------------------------------------------------------


def _reopen(ctx: RunContext, agent_id: str) -> None:
    run_id = ctx.run_id
    _OPEN[run_id] = _OPEN.get(run_id, 0) + 1
    if _OPEN[run_id] == 1:
        ctx.emit("run.status", {"status": "running", "stage": ctx.state.run.stage}, stage=ctx.state.run.stage,
                 message=f"Rerunning {agent_id}: run reopened")


async def _close(ctx: RunContext) -> None:
    """The last rerun of a reopened run writes the report files again and completes the run again."""
    run_id = ctx.run_id
    _OPEN[run_id] = max(0, _OPEN.get(run_id, 1) - 1)
    if _OPEN[run_id]:
        return
    _OPEN.pop(run_id, None)
    if ctx.stopped() or ctx.state.run.status != "running":
        return

    try:
        await finalize.run(ctx)
    except Exception as e:  # noqa: BLE001 - the files are a record, the run still completes
        log.exception("complete-again files failed for %s", run_id)
        ctx.emit("agent.log", m.AgentLogData(text=f"Report files not written again: {why(e)}"), level="warn",
                 message=f"Report files not written again after the rerun: {why(e)}")
    ctx.emit("run.status", {"status": "done", "stage": "complete"}, stage="complete", message="Run complete again")
    ctx.emit("run.completed", {}, stage="complete", message=f"Run complete again for {ctx.state.run.company}")


# ---------------------------------------------------------------------------
# New versions
# ---------------------------------------------------------------------------


def _note(runner: AgentRunner) -> str:
    a = runner.agent
    who = "Bobby" if a.rerun_by == "orchestrator" else "the analyst"
    return f"Rework by {a.agent_id} (rerun of {a.parent_agent_id} by {who}): {_clip(a.instructions or '')}"


def _text(item: m.Item) -> str:
    return verify_stage.claim_text(item)


def new_version(ctx: RunContext, old: m.Item, new: m.Item, runner: AgentRunner) -> m.Item:
    """Store `new` as the next version of `old` (same id): the old version becomes a rework attempt, the new
    one is pending. Emits item.updated with reason rework."""
    v = old.verification
    attempt = m.Attempt(attempt=len(v.attempts) + 1, status=v.status, checks=list(v.checks), action=REASON,
                        before=_text(old), after=_text(new), note=_note(runner), at=now_iso())
    out = new.model_copy(update={"id": old.id, "verification": m.Verification(status="pending",
                                                                               attempts=[*v.attempts, attempt]),
                                 "certainty": None})
    ctx.state.items[out.id] = out
    ctx.emit("item.updated", m.ItemUpdatedData(item=out, reason=REASON), agent_id=runner.agent_id,
             message=f"{out.id} new version from {runner.agent_id}")
    return out


def settle(status: str, history: list[m.Attempt]) -> str:
    """A pass after a failure in the item's history is `fixed` (failed, then passed after a repair or rework)."""
    return "fixed" if status == "pass" and any(a.status == "fail" for a in history) else status


def with_history(v: m.Verification, history: list[m.Attempt]) -> m.Verification:
    """The fresh verification of a new version, after the item's history (attempts renumbered)."""
    fresh = [a.model_copy(update={"attempt": len(history) + i + 1}) for i, a in enumerate(v.attempts)]
    return m.Verification(status=settle(v.status, history), checks=list(v.checks), attempts=[*history, *fresh],
                          verified_at=v.verified_at or now_iso())


async def _check_claim(ctx: RunContext, iid: str, book: Any, *, message: str | None = None) -> str:
    """Code checks, then judges (stages.verify.run_checks), stored with the item's history. Returns the status."""
    item = ctx.state.items[iid]
    found = await verify_stage.run_checks(ctx, item)
    item = ctx.state.items[iid]
    history = list(item.verification.attempts)
    item.verification = m.Verification(status=settle(checks.status_from(found), history), checks=found,
                                       attempts=history, verified_at=now_iso())
    finalize.emit_verified(ctx, item, book, message=message)
    return item.verification.status


# ---------------------------------------------------------------------------
# Role by role
# ---------------------------------------------------------------------------


async def _rerun_extractor(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    pairs = await extract.reextract(ctx, runner, plan.job, plan.old)
    old = {i.id: i for i in plan.old}
    out = Outcome()
    # B-replay: new D- ids are taken at once, in the order the page's items came back, before any await,
    # so the checks below (which yield) can't let another task take an id in between.
    pairs = [(item if old_id is not None else item.model_copy(update={"id": ctx.next_id("D")}), old_id)
             for item, old_id in pairs]
    for item, old_id in pairs:
        if old_id is not None:
            pending = new_version(ctx, old[old_id], item, runner)
            checked = await extract.check_reworked(ctx, pending)
            checked = checked.model_copy(update={"verification": with_history(checked.verification,
                                                                              pending.verification.attempts)})
            extract.record_verified(ctx, checked)
            out.updated.append(old_id)
        else:
            fresh = item
            extract.register(ctx, fresh)
            extract.record_verified(ctx, await extract.check_reworked(ctx, fresh))
            out.created.append(fresh.id)
    out.kept = [i for i in old if i not in out.updated]
    return out


async def _rerun_drafter(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    built = await draft.redraft_group(ctx, runner, plan.group, plan.old)
    old = {i.id: i for i in plan.old}
    out = Outcome()
    for item in built:
        if item.id in old:
            new_version(ctx, old[item.id], item, runner)
            out.updated.append(item.id)
        else:
            ctx.state.items[item.id] = item
            ctx.emit("item.created", m.ItemCreatedData(item=item), agent_id=runner.agent_id,
                     message=f"{item.id} drafted ({item.kind}) by {runner.agent_id}")
            out.created.append(item.id)
    out.kept = [i for i in old if i not in out.updated]
    book = finalize.CertaintyBook(ctx.state)
    for kinds in (("conclusion",), ("extrapolation", "chart")):       # the pipeline's order: C, then E and G
        ids = [i for i in out.changed if ctx.state.items[i].kind in kinds]
        statuses = await asyncio.gather(*(_check_claim(ctx, i, book) for i in ids))
        failed = [i for i, s in zip(ids, statuses) if s == "fail"]
        await asyncio.gather(*(repair.repair_item(ctx, i, book) for i in failed))
    return out


async def _rerun_brief(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    old = [s.id for _, s in summarize.all_sentences(ctx.state.summary.sections)] if ctx.state.summary else []
    await summarize.draft_brief(ctx, runner)
    _log(ctx, runner.agent_id, f"Brief written again: it replaces {len(old)} sentence(s) of the previous brief")
    await verify_summary.run(ctx)
    sids = [s.id for _, s in summarize.all_sentences(ctx.state.summary.sections)]
    return Outcome(sentences=sids, brief=True)


async def _rerun_sentence(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    new = await summarize.revise_sentence(ctx, runner, plan.target)
    return await _offer_sentence(ctx, runner, new)


async def _rerun_judge(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    _log(ctx, runner.agent_id, "Judges never see instructions, since a check is never steered. A fresh panel "
                               f"of {JUDGES} judges checks {plan.target} again. The instructions stay on record here.")
    if plan.target.startswith("H-"):
        s = summarize.find_sentence(ctx.state.summary, plan.target)
        await _recheck_sentence(ctx, s, runner.agent_id, note=_note(runner), action=REASON)
        return Outcome(sentences=[s.id])
    old = ctx.state.items[plan.target]
    new_version(ctx, old, old.model_copy(deep=True), runner)
    await _check_claim(ctx, plan.target, finalize.CertaintyBook(ctx.state),
                       message=f"{plan.target} judged again after {runner.agent_id}")
    return Outcome(updated=[plan.target])


async def _rerun_repairer(ctx: RunContext, runner: AgentRunner, plan: Plan) -> Outcome:
    if plan.target.startswith("H-"):
        return await _repair_sentence(ctx, runner, plan.target)
    old = ctx.state.items[plan.target]
    prop = await repair.agent_repair(ctx, old, runner=runner)
    book = finalize.CertaintyBook(ctx.state)
    if prop.item is None:                       # the repairer found nothing to keep: the claim fails, as in repair
        v = old.verification
        attempt = m.Attempt(attempt=len(v.attempts) + 1, status=v.status, checks=list(v.checks), action=REASON,
                            before=_text(old), after=None, note=f"{_note(runner)}. {prop.action}: {prop.note}",
                            at=now_iso())
        old.verification = m.Verification(status="fail", checks=list(v.checks), attempts=[*v.attempts, attempt],
                                          verified_at=now_iso())
        finalize.emit_verified(ctx, old, book, message=f"{old.id} {prop.action} by {runner.agent_id}: {prop.note}")
        return Outcome(updated=[old.id], note=f"{runner.agent_id} found nothing supported to keep in {old.id}")
    new_version(ctx, old, prop.item, runner)
    await _check_claim(ctx, old.id, book)
    return Outcome(updated=[old.id])


async def _repair_sentence(ctx: RunContext, runner: AgentRunner, sid: str) -> Outcome:
    s = summarize.find_sentence(ctx.state.summary, sid)
    fixer = verify_summary.Fixer(ctx, sid)
    fixer.runner = runner
    failed = [c for c in s.verification.checks if c.result == "fail"]
    rep = await verify_summary.rewrite(verify_summary.make_env(ctx.state), s, failed, fixer)
    if rep.sentence is None:
        return Outcome(note=f"{runner.agent_id} found nothing to keep in {sid} ({rep.note}). The brief keeps it.")
    new = rep.sentence.model_copy(update={"verification": m.Verification(
        status="pending", attempts=list(s.verification.attempts))})
    return await _offer_sentence(ctx, runner, new)


# ---------------------------------------------------------------------------
# Brief sentences
# ---------------------------------------------------------------------------


class FreshPanel(verify_summary.JudgePanel):
    """The brief's faithfulness panel, with judge ids that keep counting (verifier-H-012-4 to -6), so the
    stage's own judges keep their records."""

    def _ensure(self) -> None:
        if not self.runners:
            k0 = verify_stage.next_judge_k(self.ctx.state, self.sid)
            self.runners = [AgentRunner(self.ctx, "verifier", f"Judge whether {self.sid} is faithful to its items",
                                        [self.sid], "small", agent_id=f"verifier-{self.sid}-{k0 + j}")
                            for j in range(JUDGES)]


async def _sentence_checks(ctx: RunContext, s: m.SummarySentence) -> tuple[list[m.CheckResult], Any]:
    """Code checks, then the faithfulness panel for a derived sentence. Returns the results and the env."""
    env = verify_summary.make_env(ctx.state)
    panel = FreshPanel(ctx, env, s.id)
    try:
        res = await verify_summary.check_round(env, s, panel)
    except BaseException as e:
        panel.finish("stopped" if isinstance(e, StopRequested) else "failed")
        raise
    panel.finish("done")
    return res, env


def _publish_sentence(ctx: RunContext, s: m.SummarySentence, env: Any, message: str) -> None:
    s.certainty = verify_summary.sentence_certainty(env, s)
    ctx.emit("summary.verified", m.SummaryVerifiedData(sentence_id=s.id, verification=s.verification,
                                                       certainty=s.certainty),
             message=message, level="info" if s.verification.status in PASSING else "warn")


async def _offer_sentence(ctx: RunContext, runner: AgentRunner, new: m.SummarySentence) -> Outcome:
    """Check a new version of a brief sentence. It replaces the old one only when it passes: the brief never
    takes a sentence that failed. Either way the attempt is on record."""

    st = ctx.state
    old = summarize.find_sentence(st.summary, new.id)
    v = old.verification
    res, env = await _sentence_checks(ctx, new)
    status = checks.status_from(res)
    attempt = m.Attempt(attempt=len(v.attempts) + 1, status=v.status, checks=list(v.checks), action=REASON,
                        before=old.text, after=new.text, note=_note(runner), at=now_iso())
    if status in PASSING:
        history = [*v.attempts, attempt]
        new = new.model_copy(update={"verification": m.Verification(status=settle(status, history), checks=res,
                                                                     attempts=history, verified_at=now_iso())})
        summarize.replace_sentence(st.summary, new)
        _publish_sentence(ctx, new, env, f"{new.id} new version from {runner.agent_id}: {new.verification.status}")
        _refresh_brief(st)
        return Outcome(sentences=[new.id])
    bad = next((c for c in res if c.result != "pass"), None)
    detail = f"{bad.check}: {_clip(bad.detail, 160)}" if bad else status
    attempt = attempt.model_copy(update={"status": status, "checks": res,
                                         "note": f"{_note(runner)}. The new version failed its checks ({detail}), "
                                                 "so the brief keeps the previous one."})
    old.verification = v.model_copy(update={"attempts": [*v.attempts, attempt]})
    _publish_sentence(ctx, old, env, f"{old.id} kept: the new version from {runner.agent_id} failed ({detail})")
    return Outcome(note=f"the new version of {old.id} failed its checks ({detail}), so the brief keeps the old one")


async def _recheck_sentence(ctx: RunContext, s: m.SummarySentence, agent_id: str, *, note: str | None = None,
                            action: str | None = None) -> bool:
    """Check one brief sentence again in place (no repair: what fails shows as failed). True when it moved."""
    v = s.verification
    res, env = await _sentence_checks(ctx, s)
    status = checks.status_from(res)
    status = "fixed" if status == "pass" and v.status == "fixed" else status
    attempts = list(v.attempts)
    if action is not None or status != v.status:
        attempts.append(m.Attempt(attempt=len(attempts) + 1, status=v.status, checks=list(v.checks), action=action,
                                  note=note or f"Checked again after {agent_id} changed what it cites", at=now_iso()))
    s.verification = m.Verification(status=status, checks=res, attempts=attempts, verified_at=now_iso())
    _publish_sentence(ctx, s, env, f"{s.id} {status}: checked again after {agent_id}")
    _refresh_brief(ctx.state)
    return status != v.status


def _refresh_brief(state: RunState) -> None:
    """Stats, holes and the brief's certainty after sentences changed."""
    brief = state.summary
    if brief is None:
        return
    sents = [s for _, s in summarize.all_sentences(brief.sections)]
    brief.stats.passed = sum(s.verification.status == "pass" for s in sents)
    brief.stats.fixed = sum(s.verification.status == "fixed" for s in sents)
    brief.holes = list(state.holes)
    if brief.status == "final":
        brief.certainty = verify_summary.brief_certainty(state, brief.sections)


# ---------------------------------------------------------------------------
# Recheck: everything citing what changed
# ---------------------------------------------------------------------------


def refs_of(item: m.Item) -> set[str]:
    if item.kind == "conclusion":
        return set(item.data_ids)
    if item.kind == "extrapolation":
        return set(item.conclusion_ids)
    if item.kind == "chart":
        return {r.split(":")[0] for r in item.refs}
    return set()


async def recheck_after(ctx: RunContext, out: Outcome, agent_id: str) -> None:
    """Check again everything citing the changed items: the nearest level first, further only past items whose
    status moved. Then finalize a final report again, then the brief sentences citing anything that changed."""

    st = ctx.state
    changed = set(out.changed)
    moved = set(changed)
    seen = set(changed)
    level = set(changed)
    book = finalize.CertaintyBook(st)
    while level:
        deps = sorted(i for i, it in st.items.items()
                      if i not in seen and refs_of(it) & level and it.verification.status != "pending")
        seen.update(deps)
        before = {i: st.items[i].verification.status for i in deps}
        await asyncio.gather(*(_recheck_item(ctx, i, agent_id, book) for i in deps))
        out.rechecked += deps
        level = {i for i in deps if st.items[i].verification.status != before[i]}
        moved |= level
    if st.report_ready and (changed or moved):
        finalize.finalize_report(ctx)
        _refresh_brief(st)
    if st.summary is None or out.brief:
        return
    touched = set(out.rechecked) | changed
    scores_moved = any(st.items[i].kind == "extrapolation" for i in touched if i in st.items)
    targets = [s for _, s in _all_sentences(st)
               if s.id not in out.sentences and s.verification.status != "pending"
               and ({r.split(":")[0] for r in s.derived_from} & touched or ("rollup" in s.derived_from and scores_moved))]
    await asyncio.gather(*(_recheck_sentence(ctx, s, agent_id) for s in targets))
    out.rechecked += [s.id for s in targets]


def _all_sentences(state: RunState) -> list[tuple[str, m.SummarySentence]]:
    return summarize.all_sentences(state.summary.sections) if state.summary else []


async def _recheck_item(ctx: RunContext, iid: str, agent_id: str, book: Any) -> None:
    item = ctx.state.items[iid]
    v = item.verification
    found = await verify_stage.run_checks(ctx, item)
    item = ctx.state.items[iid]
    status = checks.status_from(found)
    status = "fixed" if status == "pass" and v.status == "fixed" else status
    attempts = list(v.attempts)
    if status != v.status:
        cited = sorted(refs_of(item))
        attempts.append(m.Attempt(attempt=len(attempts) + 1, status=v.status, checks=list(v.checks),
                                  note=f"Checked again after {agent_id} reworked what it cites ({_and(cited)})",
                                  at=now_iso()))
    item.verification = m.Verification(status=status, checks=found, attempts=attempts, verified_at=now_iso())
    finalize.emit_verified(ctx, item, book, message=f"{iid} {status}: checked again after {agent_id}")
