"""Resume a stopped run (CONTRACT.md 13, docs/BACKEND_V2.md step 9). Built by V6-resume.

The route (POST /runs/{id}/resume) has already checked that the run is stopped. resume_run then:
1. waits for the old pipeline task (and side tasks) to end while the kill switch is still on, so a result
   still in flight can never land in the resumed run;
2. turns the kill switch off with a policy version bump and policy.updated. Every rule stays, so the
   analyst's R-stop-<agent_id> rules (origin user_agent_stop) keep those agents stopped;
3. emits run.resumed {reason, stage} and run.status running, at the stage STOP cut into;
4. brings Bobby back to working (a whole-run STOP stops him first), unless the analyst stopped him on his
   own (R-stop-bobby): then he stays stopped and every gate proceeds without him (gates.bobby_off);
5. starts a new pipeline task with orchestrator.start_from at that stage, which skips stages whose result is
   already in the state. Inside a stage finished work is kept: extract reads only the pages no extractor
   finished (resume_extract), verify checks only pending items, collect never fetches a stored source.

Work an analyst-stopped agent owned is never handed to a new agent: its page or sources stay undone and
finalize declares them as a hole ("agent stopped by the analyst").
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from . import orchestrator
from .agents.base import AgentRunner, revive_orchestrator
from .agentstop import is_stopped
from .contract import m
from .gateway import STOP_ORIGIN
from .ids import now_iso
from .runtime import InvalidState, StopRequested
from .store.state import BOBBY_ID

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

PLAN_ID = "V6-resume"
DEFAULT_REASON = "Resumed by the analyst"
CHANGE = "Kill switch off (resumed)"
WIND_DOWN_S = 15.0                   # how long resume waits for the stopped pipeline task to end
FINISHED = ("done", "failed")        # a failed page is finished too: finalize declares it as a hole
# Extractor tasks, as stages/extract.py writes them: one page, or one JSON source read by code.
_PAGE_TASK = re.compile(r"^Extract (?P<sid>\S+) page (?P<page>\d+)$")
_JSON_TASK = re.compile(r"^Read (?P<sid>\S+) structured facts by code")


def not_stopped_message(status: str) -> str:
    """The 409 text, the same as the mock's."""
    return f"Only a stopped run can resume. This one is {status}."


async def resume_run(ctx: RunContext, reason: str | None = None) -> m.Run:
    """Resume the stopped run and return it (status running). InvalidState (409) for any other status, or
    when the stopped pipeline task has not ended yet."""
    state = ctx.state
    if state.run.status != "stopped":
        raise InvalidState(not_stopped_message(state.run.status))
    why = (reason or "").strip() or DEFAULT_REASON
    await wind_down(ctx)
    stage = cut_stage(state)
    kill_switch_off(ctx)
    ctx.emit("run.resumed", m.RunResumedData(reason=why, stage=stage), stage=stage,
             message=f"Run resumed at {stage}: {why}")
    ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message="Kill switch off")
    bring_back_bobby(ctx)
    if not orchestrator.approval_committed(state):
        ctx.approval.clear()                       # POST /stop set it to wake the approve stage
    await ctx.store.save_async(state)
    ctx.task = asyncio.create_task(orchestrator.start_from(ctx, stage, runners=RUNNERS),
                                   name=f"pipeline-{ctx.run_id}")
    return state.run.model_copy(deep=True)


async def wind_down(ctx: RunContext) -> None:
    """Cancel the stopped run's pipeline task and side tasks and wait for them, with the kill switch still on.
    STOP doesn't cancel them: they end at their next stop check or gateway call, which can be a long model
    call away. Raises InvalidState when they don't end in WIND_DOWN_S."""
    me = asyncio.current_task()
    tasks = [t for t in (ctx.task, *ctx.background) if t is not None and not t.done() and t is not me]
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    _, pending = await asyncio.wait(tasks, timeout=WIND_DOWN_S)
    if pending:
        raise InvalidState("The stopped run is still winding down. Try resume again in a moment.")


def cut_stage(state: RunState) -> str:
    """The stage STOP cut into: the stage on the last run.stopped event (the run's stage when STOP landed)."""
    names = [s for s, _ in orchestrator.STAGES]
    ev = next((e for e in reversed(state.events) if e.type == "run.stopped"), None)
    stage = (ev.stage if ev is not None else None) or state.run.stage
    return stage if stage in names else names[0]


def kill_switch_off(ctx: RunContext) -> m.Policy | None:
    """Kill switch off with a version bump and policy.updated. Every rule is kept as it is, the analyst's
    agent stop rules included (a fork drops them, a resume never does)."""
    state = ctx.state
    state.kill_switch = False
    state.run.stop_reason = None
    old = state.policy
    version = max(old.version if old else 0, state.run.policy_version) + 1
    kept = [r.rule_id for r in old.rules if r.origin == STOP_ORIGIN] if old else []
    if old is not None:
        state.policy = old.model_copy(deep=True, update={"version": version, "kill_switch": False,
                                                         "updated_at": now_iso()})
    state.run.policy_version = version
    tail = f", agent stop rules kept: {', '.join(kept)}" if kept else ""
    ev = ctx.emit("policy.updated", m.PolicyUpdatedData(version=version, change=CHANGE, rule_ids=[]),
                  message=f"Gateway policy v{version}: kill switch off{tail}")
    if state.policy is not None:
        state.policy.as_of_seq = ev.seq
    return state.policy


def bring_back_bobby(ctx: RunContext) -> None:
    """A whole-run STOP stops Bobby first, so the resumed run gets him back. If the analyst stopped him on his
    own, he stays stopped and the gates proceed without his calls."""
    state = ctx.state
    agent = state.agents.get(BOBBY_ID)
    if agent is None or agent.status != "stopped":
        return
    if is_stopped(state, BOBBY_ID):
        ctx.emit("agent.log", m.AgentLogData(text="Bobby stays stopped (the analyst stopped him): every later "
                                                  "stage gate proceeds without his call."),
                 message="bobby: stays stopped after resume, gates proceed without him", agent_id=BOBBY_ID,
                 level="warn")
        return
    revive_orchestrator(ctx, BOBBY_ID)


# ---------------------------------------------------------------------------
# Stage entries for a resumed run: the stage's own code, minus the work already finished
# ---------------------------------------------------------------------------


async def resume_plan(ctx: RunContext) -> None:
    """plan.run appends needs and sources as it streams them, so a plan STOP cut off is redone whole: drop
    the half that streamed (the frozen plan gives the same ids back) and keep the notes it already added."""
    from .stages import plan

    state = ctx.state
    notes = state.run.notes
    state.needs, state.sources = [], []
    await plan.run(ctx)
    if notes and plan.FROZEN_NOTE in notes:
        state.run.notes = notes


async def resume_collect(ctx: RunContext) -> None:
    """collect.run without what an analyst-stopped fetcher left undone: those sources stay skipped (a hole),
    never fetched by a new fetcher. Stored sources are never fetched twice."""
    from .stages import collect

    state = ctx.state
    for f in [a for a in state.agents.values() if a.role == "fetcher" and is_stopped(state, a.agent_id)]:
        collect.skip_rest(ctx, f, [s for s in state.sources if s.source_id in f.target_ids])
    todo = [s for s in collect.collect_order(state.sources)
            if s.status != "skipped" and not (s.doc_ids and s.status in collect.ALREADY_STORED)]
    if not todo:
        return
    agent = collect.spawn_fetcher(ctx, [s.source_id for s in todo])
    status = "failed"
    try:
        for i, source in enumerate(todo):
            ctx.check_stop()
            await collect.collect_source(ctx, agent, source)
            if is_stopped(state, agent.agent_id) and collect.stop_shown(ctx, agent):
                collect.skip_rest(ctx, agent, todo[i + 1:])
                break
        status = "done"
    except StopRequested:
        status = "stopped"
        raise
    finally:
        collect.finish_fetcher(ctx, agent, status)


def finished_extract_work(state: RunState) -> tuple[set[tuple[str, int]], set[str]]:
    """(source_id, page) pages and JSON source ids an extractor already covered: it finished (done or
    failed), its items are in, or the analyst stopped it (that stop stands, and finalize declares the page).
    An extractor the whole-run STOP cut off covers nothing, so its page is read again."""
    with_items = {getattr(it, "agent_id", None) for it in state.data_items()}
    pages: set[tuple[str, int]] = set()
    json_sources: set[str] = set()
    for a in state.agents.values():
        if a.role != "extractor":
            continue
        if not (a.status in FINISHED or a.agent_id in with_items or is_stopped(state, a.agent_id)):
            continue
        if (mm := _PAGE_TASK.match(a.task)) is not None:
            pages.add((mm["sid"], int(mm["page"])))
        elif (mm := _JSON_TASK.match(a.task)) is not None:
            json_sources.add(mm["sid"])
    return pages, json_sources


def remaining_extract_work(state: RunState) -> list:
    """extract.plan_work at page level: the pages and JSON documents no extractor covered yet."""
    from .stages import extract

    pages_done, json_done = finished_extract_work(state)
    docs = state.documents_by_id()
    work = []
    for source in state.sorted_sources():
        if source.approval not in extract.APPROVED or source.status in extract.SKIP_SOURCE:
            continue
        w = extract.SourceWork(source.source_id)
        for doc_id in source.doc_ids:
            doc = docs.get(doc_id)
            if doc is None or not state.agent_pages(doc_id):
                continue                            # quarantined or never stored: no agent reads it
            if doc.content_type == "json":
                if source.source_id not in json_done:
                    w.json_docs.append(extract.JsonJob(source.source_id, doc_id))
                continue
            for p in extract.pages_to_read(state, source, doc):
                if (source.source_id, p) not in pages_done:
                    w.pages.append(extract.PageJob(source.source_id, doc_id, p,
                                                   extract.needs_for_page(state, source, p)))
        if w.pages or w.json_docs:
            work.append(w)
    return work


async def resume_extract(ctx: RunContext) -> None:
    """extract.run on the remaining pages only: items already extracted stay, and no page is read twice."""
    from .stages import extract

    state = ctx.state
    work = remaining_extract_work(state)
    pages_by_doc = extract.readable_pages(state)
    for w in work:                                  # spawn every agent up front, as extract.run does
        for j in w.json_docs:
            j.runner = AgentRunner(ctx, "extractor", f"Read {j.source_id} structured facts by code (no model)",
                                   [j.source_id], None)
        for p in w.pages:
            p.runner = AgentRunner(ctx, "extractor", f"Extract {p.source_id} page {p.page}", [p.source_id], "small")
    sem = asyncio.Semaphore(max(1, ctx.settings.max_concurrency_small))
    results = await asyncio.gather(*(extract.extract_source(ctx, w, pages_by_doc, sem) for w in work),
                                   return_exceptions=True)
    stop = next((r for r in results if isinstance(r, StopRequested)), None)
    if stop is not None:
        raise stop
    crash = next((r for r in results if isinstance(r, BaseException)), None)
    if crash is not None:
        raise crash
    ctx.check_stop()
    changed = extract.apply_tier_rules(ctx)
    for source in state.sources:                    # STOP after its last page left it extracting
        if source.status == "extracting" or (source.source_id in changed and source.status == "extracted"):
            extract.set_source_status(ctx, source, "extracted")


RUNNERS = {"plan": resume_plan, "collect": resume_collect, "extract": resume_extract}
