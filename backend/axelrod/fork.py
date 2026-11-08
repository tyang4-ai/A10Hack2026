"""Rerun from a step: fork a finished, stopped or failed run (CONTRACT.md 13, docs/BACKEND_V2.md step 11).

A fork is a new run with its own id, its own policy and its own event log from seq 1 (its run.created carries
parent_run_id, rerun_from and instructions). It copies the parent's state up to from_stage, keeps every item
id so citations still resolve, and runs the rest through orchestrator.start_from. The instructions reach every
agent it spawns (AgentRunner reads Run.instructions; judges never get them, since a check is never steered),
and every check still applies to what they produce. The parent is never changed: everything kept is a copy.

What a fork keeps, by from_stage (the CONTRACT.md 13 table):

    plan            nothing (the plan stage runs again)
    approve         the needs and the sources as the plan proposed them (the approval gate waits again)
    collect         + the approvals and the policy
    extract         + the stored documents and their page texts
    draft           + the data items, the parent's latest versions (all checked by code)
    verify          + the draft: conclusions, scores and charts as the drafter wrote them, checks redone
    repair          + the draft as the verify stage left it, before any repair
    summarize       + the final agent report (rollup, certainty, holes)
    verify_summary  + the brief, its sentences checked again

The draft and its checks are rebuilt from the parent's own events (item.created, item.updated and item.verified
before the stage's first run.status), since the parent's items hold their latest, repaired versions. The agents
whose work is kept are copied with their names, and their transcripts and the kept documents are copied into
the fork's vault folder, so its vault drawer opens them.

Policy: the parent's rules from collect on (approvals, rejections, revocations), version 1, kill switch off.
The analyst's agent stop rules (user_agent_stop) are dropped, since a copied R-stop-extractor-03 would block
the fork's own extractor-03. The one exception is a rule for an agent the fork copies: that agent never runs
again, and its rule is what makes finalize declare the work it never finished as a hole.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, get_args

from . import orchestrator
from .cache import run_cache_mode
from .contract import m
from .gateway import STOP_ORIGIN, role_scopes
from .ids import IdAllocator, now_iso
from .runtime import InvalidState, RunContext
from .store import FAKE_MODELS_NOTE
from .store.events import lock_for
from .store.state import BOBBY_ID, ORCHESTRATOR_ROLE, TERMINAL_STATUSES, RunState

if TYPE_CHECKING:
    from .config import Settings
    from .store import Store
    from .store.vault import Vault

log = logging.getLogger(__name__)

PLAN_ID = "V8-fork"
STAGES: tuple[str, ...] = get_args(m.Stage)
DRAFT_KINDS = ("conclusion", "extrapolation", "chart")
PLAN_FILES = ("00-Meta/_plan.json", "00-Meta/_url-manifest.json")
INDEX_FILE = "00-Meta/_document-index.json"
REPORT_JSON = "09-Analysis/agent-report.json"
_ID = re.compile(r"([A-Za-z_]+)-(\d+)")
# The stage each role works in: where an agent with no agent.spawned event belongs.
ROLE_STAGE = {"planner": "plan", "fetcher": "collect", "extractor": "extract", "drafter": "draft",
              "verifier": "verify", "repairer": "repair", "summarizer": "summarize", "reviser": "verify_summary"}


def _at(stage: str) -> int:
    return STAGES.index(stage)


# ---------------------------------------------------------------------------
# Entry point (POST /runs/{id}/rerun)
# ---------------------------------------------------------------------------


def check_forkable(parent: RunState, from_stage: str) -> None:
    """The route's rules, with the mock's messages, for callers that come without the route."""
    status = parent.run.status
    if status not in TERMINAL_STATUSES:
        raise InvalidState(f"Rerun forks a finished, stopped or failed run. This one is {status}.")
    if from_stage == "complete":
        raise InvalidState("Nothing to rerun after complete. Pick an earlier step.")
    if _at(from_stage) > _at(parent.run.stage):
        raise InvalidState(f"{parent.run.run_id} never reached {from_stage}")


async def fork_run(store: Store, vault: Vault, settings: Settings, parent_state: RunState,
                   req: m.RerunRequest) -> RunContext:
    """Create the fork, emit its run.created (and its carried policy), and start its pipeline at from_stage.
    Returns its context at once: the vault copies and the stages run in the fork's own task."""
    check_forkable(parent_state, req.from_stage)
    parent_id = parent_state.run.run_id
    with lock_for(parent_id):            # a side task (chat) may still emit into a finished parent
        state = build_fork(parent_state, req, run_id=store.fresh_run_id(), settings=settings)
    store.put(state)                     # registers the fork's vault folder before paths move into it
    copies = rebase_paths(vault, parent_id, state)
    ctx = RunContext.create(state, store, vault, settings)
    stage = req.from_stage
    ctx.emit("run.created", {"run": state.run}, stage=stage,
             message=f"Run created for {state.run.company}: rerun of {parent_id} from {stage}")
    if state.policy is not None:
        _announce_policy(ctx, parent_id)
    elif _at(stage) > _at("approve"):
        ctx.gateway.build_policy()       # the parent kept no policy: build it from the copied approvals
    ctx.task = asyncio.create_task(_run_fork(ctx, parent_id, copies, stage), name=f"pipeline-{ctx.run_id}")
    return ctx


async def _run_fork(ctx: RunContext, parent_id: str, copies: list[str], stage: str) -> None:
    """Copy the reused vault files, then run the pipeline from the fork's stage."""
    if copies:
        try:
            copied, missing = await asyncio.to_thread(ctx.store.copy_vault_files, parent_id, ctx.run_id, copies)
            log.info("fork %s: %d vault file(s) copied from %s, %d not found", ctx.run_id, len(copied), parent_id,
                     len(missing))
        except asyncio.CancelledError:
            orchestrator._on_fail(ctx, orchestrator.SHUTDOWN)
            ctx.store.save(ctx.state)
            raise
        except Exception:  # a vault copy is a convenience: the stages read the state, never these files
            log.exception("fork %s: copying vault files from %s failed", ctx.run_id, parent_id)
    await orchestrator.start_from(ctx, stage)


def _announce_policy(ctx: RunContext, parent_id: str) -> None:
    policy = ctx.state.policy
    kept_stops = [r.rule_id for r in policy.rules if r.origin == STOP_ORIGIN]
    note = " Agent stop rules dropped" + (f", except {', '.join(kept_stops)} on copied agents." if kept_stops else ".")
    ev = ctx.emit("policy.updated", m.PolicyUpdatedData(
        version=policy.version, rule_ids=[r.rule_id for r in policy.rules],
        change=f"Carried over from {parent_id} (fork): approvals, rejections and revocations.{note}"),
        message=f"Gateway policy v{policy.version}: the parent's rules, kill switch off, default deny")
    policy.as_of_seq = ev.seq


# ---------------------------------------------------------------------------
# The fork's state (pure: reads the parent, copies what it keeps)
# ---------------------------------------------------------------------------


def build_fork(parent: RunState, req: m.RerunRequest, *, run_id: str, settings: Settings) -> RunState:
    """The fork's RunState: the parent's work before req.from_stage, deep copied, at stage from_stage."""
    stage = req.from_stage
    k = _at(stage)
    cut = cut_seq(parent, stage)
    state = RunState(run=fork_record(parent, req, run_id, settings))
    if k > _at("plan"):
        state.needs = [n.model_copy(deep=True) for n in parent.needs]
        state.extract_pages = {s: [list(r) for r in rs] for s, rs in parent.extract_pages.items()}
        state.need_pages = {n: {s: [list(r) for r in rs] for s, rs in per.items()}
                            for n, per in parent.need_pages.items()}
        state.as_of, state.fiscal_note, state.plan_ready = parent.as_of, parent.fiscal_note, True
    state.sources = fork_sources(parent, k)
    if k > _at("collect"):
        state.documents = [d.model_copy(deep=True) for d in parent.documents]
        state.pages = {doc_id: list(texts) for doc_id, texts in parent.pages.items()}
    state.items = fork_items(parent, k, cut)
    if k > _at("repair"):
        state.holes = [h.model_copy(deep=True) for h in parent.holes]
        state.rollup = parent.rollup.model_copy(deep=True) if parent.rollup else None
        state.report_certainty = parent.report_certainty.model_copy(deep=True) if parent.report_certainty else None
        state.sections = [s.model_copy(deep=True) for s in parent.sections]
        state.report_generated_at, state.report_markdown_path = parent.report_generated_at, parent.report_markdown_path
        state.report_ready = True
    if k > _at("summarize"):
        state.summary = fork_brief(parent, run_id)
    state.disclaimer = parent.disclaimer
    state.agents = fork_agents(parent, k, cut, state.items)
    if k > _at("approve"):
        state.approval_request = parent.approval_request.model_copy(deep=True) if parent.approval_request else None
        state.policy = fork_policy(parent, run_id, settings, state)
    state.ids = fork_ids(state)
    return state


def fork_record(parent: RunState, req: m.RerunRequest, run_id: str, settings: Settings) -> m.Run:
    """The fork's Run: same company and budget, at from_stage, with the parent fields and the instructions.
    A fork from plan starts from the parent's Run as it was created (the plan stage adds its notes again)."""
    now = now_iso()
    base = (_created_run(parent) or parent.run) if req.from_stage == "plan" else parent.run
    fork_note = (f"Rerun of {parent.run.run_id} from {req.from_stage} (a fork): the parent's work before "
                 f"{req.from_stage} is copied, the rest is redone with the analyst's instructions.")
    notes = (base.notes or "").strip()
    if settings.fake_models and FAKE_MODELS_NOTE not in notes:
        notes = f"{notes} | {FAKE_MODELS_NOTE}" if notes else FAKE_MODELS_NOTE
    notes = f"{notes} | {fork_note}" if notes else fork_note
    budget = parent.run.usage.budget_usd if parent.run.usage.budget_usd is not None else settings.budget_usd
    return m.Run(run_id=run_id, company=parent.run.company, ticker=parent.run.ticker, depth=base.depth,
                 status="running", stage=req.from_stage, created_at=now, updated_at=now,
                 created_via=parent.run.created_via, notes=notes, policy_version=0, counts=m.RunCounts(),
                 usage=m.Usage(budget_usd=budget), last_seq=0, mode="live", parent_run_id=parent.run.run_id,
                 rerun_from=req.from_stage, instructions=req.instructions.strip() or req.instructions,
                 cache_mode=run_cache_mode(settings))


def _created_run(parent: RunState) -> m.Run | None:
    ev = next((e for e in parent.events if e.type == "run.created"), None)
    return ev.data.run if ev is not None else None


def cut_seq(parent: RunState, stage: str) -> int | None:
    """The seq where the parent entered `stage` (its first run.status running for it). Everything before it is
    the parent's state up to that stage. None when the log has no such event (a hand-built state)."""
    return next((e.seq for e in parent.events
                 if e.type == "run.status" and e.data.status == "running" and e.data.stage == stage), None)


def fork_sources(parent: RunState, k: int) -> list[m.Source]:
    """The sources as they stood when the parent entered from_stage."""
    if k <= _at("plan"):
        return []
    if k <= _at("approve"):
        return proposed_sources(parent)
    out = []
    for s in parent.sources:
        s = s.model_copy(deep=True)
        if k <= _at("collect"):                       # approved, nothing fetched yet
            if s.status != "revoked":
                s.status = "queued" if s.approval in ("approved", "auto_approved") else "skipped"
                s.error = None
            s.doc_ids, s.stats = [], m.SourceStats()
        elif k <= _at("extract"):                     # fetched, nothing extracted yet
            if s.status in ("extracting", "extracted"):
                s.status = "fetched"
            s.stats = s.stats.model_copy(update={"data_items": 0, "data_items_verified": 0})
        out.append(s)
    return out


def proposed_sources(parent: RunState) -> list[m.Source]:
    """Each source as its last plan.source event showed it (the plan's proposal, or the analyst's addition),
    before any approval moved its tier or rank. Without events: the current sources, reset to proposed."""
    latest: dict[str, m.Source] = {}
    for e in parent.events:
        if e.type == "plan.source":
            latest[e.data.source.source_id] = e.data.source
    if latest:
        return [s.model_copy(deep=True) for s in latest.values()]
    return [s.model_copy(deep=True, update={
        "approval": "approved" if s.proposed_by == "user" else "pending", "status": "proposed", "doc_ids": [],
        "stats": m.SourceStats(), "error": None}) for s in parent.sources]


def _pending(item: m.Item) -> m.Item:
    return item.model_copy(deep=True, update={"verification": m.Verification(status="pending"), "certainty": None})


def items_before(parent: RunState, cut: int, kinds: tuple[str, ...]) -> dict[str, m.Item]:
    """Items of these kinds as the parent's events showed them just before seq `cut`: item.created and
    item.updated give the whole item, item.verified its verification and certainty."""
    out: dict[str, m.Item] = {}
    for e in parent.events:
        if e.seq >= cut:
            break
        if e.type in ("item.created", "item.updated") and e.data.item.kind in kinds:
            out[e.data.item.id] = e.data.item
        elif e.type == "item.verified" and e.data.item_id in out:
            out[e.data.item_id] = out[e.data.item_id].model_copy(update={
                "verification": e.data.verification, "certainty": e.data.certainty})
    return {iid: it.model_copy(deep=True) for iid, it in out.items()}


def fork_items(parent: RunState, k: int, cut: int | None) -> dict[str, m.Item]:
    """Kept items under their own ids, in the parent's order."""
    if k <= _at("extract"):
        return {}
    if k > _at("repair"):                             # the final report, as it stands
        return {iid: it.model_copy(deep=True) for iid, it in parent.items.items()}
    data = {iid: it.model_copy(deep=True) for iid, it in parent.items.items() if it.kind == "data"}
    if k <= _at("draft"):
        return data
    if cut is not None:
        draft = items_before(parent, cut, DRAFT_KINDS)
    else:                                             # no log to rebuild from: the latest versions
        draft = {iid: it.model_copy(deep=True) for iid, it in parent.items.items() if it.kind in DRAFT_KINDS}
    if k == _at("verify"):                            # the checks are redone, so nothing counts as checked
        draft = {iid: _pending(it) for iid, it in draft.items()}
    order = list(parent.items) + [iid for iid in draft if iid not in parent.items]
    return {iid: data.get(iid) or draft[iid] for iid in order if iid in data or iid in draft}


def fork_brief(parent: RunState, run_id: str) -> m.HumanReport | None:
    """The parent's brief with every sentence back to pending, for verify_summary to check again."""
    if parent.summary is None:
        return None
    brief = parent.summary.model_copy(deep=True, update={"run_id": run_id, "status": "drafting", "certainty": None,
                                                         "markdown_path": None})
    for section in brief.sections:
        for block in section.blocks:
            sentences = list(getattr(block, "sentences", None) or [])
            caption = getattr(block, "caption", None)
            for s in sentences + ([caption] if caption is not None else []):
                s.verification, s.certainty = m.Verification(status="pending"), None
    return brief


def fork_agents(parent: RunState, k: int, cut: int | None, items: dict[str, m.Item]) -> dict[str, m.Agent]:
    """The goons whose work the fork keeps: spawned before the parent entered from_stage, or named by a kept
    item. Bobby is never copied: the fork gets its own, spawned first by the pipeline."""
    spawned = {e.data.agent.agent_id: e.seq for e in parent.events if e.type == "agent.spawned"}
    named = {getattr(it, "agent_id", None) for it in items.values()} - {None}
    out = {}
    for aid, agent in parent.agents.items():
        if aid == BOBBY_ID or agent.role == ORCHESTRATOR_ROLE:
            continue
        seq = spawned.get(aid)
        before = seq < cut if seq is not None and cut is not None else _at(ROLE_STAGE.get(agent.role, "complete")) < k
        if before or aid in named:
            out[aid] = agent.model_copy(deep=True)
    return out


def fork_policy(parent: RunState, run_id: str, settings: Settings, state: RunState) -> m.Policy | None:
    """The parent's rules as the fork's version 1, kill switch off. Stop rules survive only for copied agents.
    None when the parent never had a policy (fork_run then builds one from the approvals)."""
    if parent.policy is None:
        return None
    rules = [r.model_copy(deep=True) for r in parent.policy.rules
             if r.origin != STOP_ORIGIN or r.agent_id in state.agents]
    state.run.policy_version = 1
    return m.Policy(run_id=run_id, version=1, kill_switch=False, rules=rules, role_scopes=role_scopes(settings),
                    budget_usd=state.run.usage.budget_usd, updated_at=now_iso(), as_of_seq=0)


def fork_ids(state: RunState) -> IdAllocator:
    """Counters that continue after every id the fork kept, and start over for everything it redoes
    (requests, comments, chat and dropped items count from 1 again in the fork's own log)."""
    top: dict[str, int] = {}
    ids = [n.need_id for n in state.needs] + [s.source_id for s in state.sources]
    ids += [d.doc_id for d in state.documents] + list(state.items) + [h.hole_id for h in state.holes]
    ids += list(state.agents)
    if state.summary is not None:
        ids += [s.id for sec in state.summary.sections for b in sec.blocks
                for s in list(getattr(b, "sentences", None) or []) + [getattr(b, "caption", None)] if s is not None]
    for i in ids:
        hit = _ID.fullmatch(i.split(".", 1)[0])      # extractor-03.2 counts as extractor-03
        if hit:
            top[hit.group(1)] = max(top.get(hit.group(1), 0), int(hit.group(2)))
    return IdAllocator(counters=top)


# ---------------------------------------------------------------------------
# Vault paths
# ---------------------------------------------------------------------------


def rebase_paths(vault: Vault, parent_id: str, state: RunState) -> list[str]:
    """Point the kept documents, transcripts and report at the fork's vault folder. Returns the relpaths
    to copy there from the parent's folder. A path in a third run's folder is left as it is."""
    src, dst = vault.root(parent_id), vault.root(state.run.run_id)
    rels: list[str] = []

    def move(path: str | None) -> str | None:
        if not path:
            return path
        p = path.replace("\\", "/")
        if p.startswith(src + "/"):
            rels.append(p[len(src) + 1:])
            return f"{dst}/{p[len(src) + 1:]}"
        if not p.startswith("runs/"):
            rels.append(p.lstrip("/"))
        return path

    k = _at(state.run.stage)
    if k > _at("plan"):
        rels.extend(PLAN_FILES)
    if k > _at("collect"):
        rels.append(INDEX_FILE)
    for doc in state.documents:
        vp = doc.vault_paths
        doc.vault_paths = m.DocVaultPaths(raw=move(vp.raw), text=move(vp.text), meta=move(vp.meta))
    for agent in state.agents.values():
        agent.transcript_path = move(agent.transcript_path)
    if state.report_markdown_path:
        state.report_markdown_path = move(state.report_markdown_path)
        rels.append(REPORT_JSON)
    return list(dict.fromkeys(rels))
