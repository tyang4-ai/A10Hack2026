"""Stage gates: Bobby, the orchestrator, spawns first and decides after plan, extract, repair and verify_summary.

At each gate code builds a short digest of the stage (counts, failures by check, holes, budget left, and the
goons he may send back), Bobby answers with one forced tool call (agents/bobby.py: proceed, rework at most 3
goons once each with instructions, or escalate with a note for the analyst), and code validates the call:
the goons exist, they are goons a rerun can redo (not Bobby, not a fetcher or a JSON reader, not working,
not stopped by the analyst), each is sent back once, at most 3 per gate, and the budget left covers them
plus the rest of the run. Then agent.decision records what actually happens, with only the reworks code
accepted, and the reworks run through rework.start_rerun(by="orchestrator", recheck on). The gate waits for
them, so the next stage sees the new versions.

Code keeps him in bounds (CONTRACT.md 14): he never touches sources, policy or check results, every call goes
through the gateway, and once he is stopped (his agent status, or a R-stop-bobby rule) every later gate is
proceed and the log says his calls are off. A gate error is the orchestrator's: logged, and the run proceeds.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import TYPE_CHECKING

from .contract import m
from .gateway import STOP_ORIGIN, Blocked, stop_rule_id
from .ids import now_iso
from .runtime import InvalidState, StopRequested
from .store.state import BOBBY_ID, ORCHESTRATOR_ROLE

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

GATE_STAGES = ("plan", "extract", "repair", "verify_summary")
BOBBY_TASK = "Orchestrate the run: review each stage gate, send goons back to rework, answer comments"
TERMINAL = ("done", "failed", "stopped")
MAX_REWORKS = 3
MAX_GOONS_SHOWN = 6
PASSING = ("pass", "fixed")
# Money the run still needs after each gate (live runs cost about $2 in all). A rework may only use the rest.
REST_OF_RUN_USD = {"plan": 2.0, "extract": 1.2, "repair": 0.6, "verify_summary": 0.0}
OFF = "Bobby is stopped, so his calls are off. Code proceeds."


def bobby(ctx: RunContext) -> m.Agent | None:
    """The run's orchestrator agent, if he was spawned."""
    return ctx.state.agents.get(BOBBY_ID)


def bobby_off(ctx: RunContext) -> bool:
    """True when the analyst stopped Bobby: a stop rule on him in the policy (R-stop-bobby), or his last
    agent.status says stopped with the analyst's reason. A whole-run STOP also ends him, but that is not
    turning his calls off (a resumed run gets him back)."""
    agent = bobby(ctx)
    if agent is None:
        return True
    policy = ctx.state.policy
    if policy is not None and any(r.rule_id == f"R-stop-{BOBBY_ID}" for r in policy.rules):
        return True
    if agent.status != "stopped":
        return False
    last = next((e for e in reversed(ctx.state.events) if e.type == "agent.status" and e.agent_id == BOBBY_ID), None)
    return last is not None and bool(getattr(last.data, "reason", None))


def spawn_bobby(ctx: RunContext) -> m.Agent:
    """Register Bobby (role orchestrator, the large model, status working) and emit agent.spawned.
    Idempotent: a run that already has him keeps him as he is."""
    existing = bobby(ctx)
    if existing is not None:
        return existing
    agent = m.Agent(agent_id=BOBBY_ID, role=ORCHESTRATOR_ROLE, model_id=ctx.settings.model_large,
                    status="working", task=BOBBY_TASK, target_ids=[], started_at=now_iso())
    ctx.state.register_agent(agent)
    ctx.emit("agent.spawned", m.AgentSpawnedData(agent=agent.model_copy(deep=True)), agent_id=BOBBY_ID,
             message="Bobby spawned: the orchestrator makes the big calls, his goons do the work")
    return agent


def finish_bobby(ctx: RunContext, status: str = "done") -> None:
    """Bobby works until the run ends: mark him done (or failed, stopped) once, with agent.status."""
    agent = bobby(ctx)
    if agent is None or agent.status in TERMINAL:
        return
    agent.status, agent.finished_at = status, now_iso()
    ctx.emit("agent.status", m.AgentStatusData(agent_id=BOBBY_ID, status=status, task=agent.task,
                                               usage=agent.usage.model_copy()),
             agent_id=BOBBY_ID, message=f"Bobby {status}: the run is over")


# ---------------------------------------------------------------------------
# The digest: what Bobby reads at a gate, built by code
# ---------------------------------------------------------------------------


def budget_left(ctx: RunContext) -> tuple[float, float, float]:
    """(spent, budget, left) in USD."""
    budget = ctx.gateway.budget_usd() if hasattr(ctx.gateway, "budget_usd") else ctx.settings.budget_usd
    budget = float(budget if budget is not None else ctx.settings.budget_usd)
    spent = float(ctx.state.run.usage.cost_usd)
    return spent, budget, max(0.0, budget - spent)


def _counts(statuses: list[str]) -> str:
    c = Counter(statuses)
    return ", ".join(f"{k} {c[k]}" for k in ("pass", "fixed", "fail", "blocked", "pending") if c[k]) or "none"


def _fails_by_check(items: list) -> str:
    c = Counter(r.check for it in items for r in it.verification.checks if r.result != "pass")
    return ", ".join(f"{k} {n}" for k, n in c.most_common(6)) or "none"


def _short(text: str | None, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _last_warning(state: RunState, agent_id: str) -> str | None:
    for e in reversed(state.events):
        if e.type == "agent.log" and e.agent_id == agent_id and e.level in ("warn", "error"):
            return e.data.text
    return None


def _goon_lines(state: RunState, kinds: tuple[str, ...], roles: tuple[str, ...]) -> list[str]:
    """Goons whose work failed somewhere: by how many of their items fail, then failed agents."""
    by_agent: dict[str, list] = {}
    for it in state.items.values():
        aid = getattr(it, "agent_id", None)
        if it.kind in kinds and aid in state.agents:
            by_agent.setdefault(aid, []).append(it)
    rows = []
    for aid, items in by_agent.items():
        a = state.agents[aid]
        bad = [i for i in items if i.verification.status not in PASSING]
        if a.role in roles and bad and a.model_id and a.status not in ("queued", "working", "stopped"):
            rows.append((len(bad), f"- {aid}, {a.task}: {len(items)} items, {len(bad)} not passing "
                                   f"({_fails_by_check(bad)})"))
    for a in state.agents.values():
        if a.role in roles and a.status == "failed" and a.agent_id not in by_agent and a.model_id:
            rows.append((0, f"- {a.agent_id}, {a.task}: failed, no items ({_short(_last_warning(state, a.agent_id))})"))
    rows.sort(key=lambda r: -r[0])
    return [r[1] for r in rows[:MAX_GOONS_SHOWN]]


def _sources_line(state: RunState) -> str:
    approved = [s for s in state.sources if s.approval in ("approved", "auto_approved")]
    c = Counter(s.status for s in approved)
    odd = {k: [s.source_id for s in approved if s.status == k] for k in ("revoked", "quarantined", "failed", "blocked")}
    tail = "".join(f", {k} {len(v)} ({', '.join(v)})" for k, v in odd.items() if v)
    return f"Sources: {len(approved)} approved, {c['extracted']} extracted{tail}."


def _ids(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none"


def plan_links(state: RunState) -> tuple[list[str], list[str]]:
    """(in-scope criteria no need names, needs no source names), computed by code from the plan, so Bobby never
    has to take the planner's word that every need is mapped (live Bobby once claimed it without seeing the map).
    A rejected source maps nothing."""
    from .stages.finalize import in_scope       # stages import the runtime, so import late

    named = {c for n in state.needs for c in n.criteria}
    sourced = {nid for s in state.sources if s.approval != "rejected" for nid in s.need_ids}
    return ([c for c in in_scope(state.run.depth) if c not in named],
            [n.need_id for n in state.needs if n.need_id not in sourced])


def stopped_by_analyst(state: RunState, agent_id: str) -> bool:
    """True only when the analyst stopped this one agent: its R-stop-<agent_id> rule (origin user_agent_stop)
    is in the policy. An agent a whole-run STOP ended also shows status stopped, and after a resume it is
    just a finished agent, so its status alone never counts."""
    policy = state.policy
    return policy is not None and any(r.rule_id == stop_rule_id(agent_id) and r.origin == STOP_ORIGIN
                                      for r in policy.rules)


def _stopped_line(state: RunState) -> str | None:
    stopped = [a.agent_id for a in state.agents.values()
               if a.role != ORCHESTRATOR_ROLE and stopped_by_analyst(state, a.agent_id)]
    return f"Stopped by the analyst: {', '.join(stopped[:8])}." if stopped else None


def build_digest(ctx: RunContext, stage: str) -> str:
    """A short, code-built digest of one stage (a few hundred tokens), so a gate costs cents."""
    st = ctx.state
    run = st.run
    spent, budget, left = budget_left(ctx)
    free = max(0.0, left - REST_OF_RUN_USD.get(stage, 0.0))
    if getattr(ctx.settings, "cache_models", "off") != "off":
        # B-replay: spend differs between a recorded run and its replay ($0 hits), so it stays out of Bobby's
        # prompt or his gate call would never be found. Code still checks every rework against the budget.
        money = f"Budget: ${budget:.2f}. Code checks every rework against what is left."
    else:
        money = (f"Budget: ${spent:.2f} spent of ${budget:.2f}, ${left:.2f} left, "
                 f"${free:.2f} of it free for reworks after the rest of the run.")
    lines = [f"Stage gate: after {stage}. {run.company}" + (f" ({run.ticker})" if run.ticker else "")
             + f", {run.depth} depth.", money]
    goons: list[str] = []
    if stage == "plan":
        tiers = Counter(s.tier for s in st.sources)
        lines.append(f"Plan: {len(st.needs)} needs, {len(st.sources)} sources (priority {tiers['priority']}, "
                     f"tier 2 {tiers['tier_2']}, tier 3 {tiers['tier_3']}). Nothing is fetched until the analyst "
                     "approves the priority sources.")
        no_need, no_source = plan_links(st)
        lines += [f"Criteria with no need: {_ids(no_need)}.", f"Needs with no source: {_ids(no_source)}."]
    elif stage == "extract":
        data = st.data_items()
        lines += [f"Data items: {len(data)} ({_counts([d.verification.status for d in data])}).",
                  f"Failures by check: {_fails_by_check(data)}.", _sources_line(st)]
        goons = _goon_lines(st, ("data",), ("extractor",))
    elif stage == "repair":
        items = list(st.items.values())
        kinds = Counter(i.kind for i in items)
        reasoning = [i for i in items if i.kind != "data"]
        lines += [f"Report items: {len(items)} (data {kinds['data']}, conclusions {kinds['conclusion']}, "
                  f"scores {kinds['extrapolation']}, charts {kinds['chart']}; {_counts([i.verification.status for i in items])}).",
                  f"Failures by check, conclusions and scores: {_fails_by_check(reasoning)}."]
        if st.rollup is not None:
            lines.append(f"Criteria scored: {st.rollup.scored} of {st.rollup.in_scope}.")
        for e in sorted(st.extrapolations(), key=lambda e: e.criterion):
            ok = e.verification.status in PASSING
            score = ("N/A" if e.score is None else f"{e.score:+d}" if e.score else "0") + f" ({e.confidence})"
            why = f": {_short(e.na_reason, 80)}" if e.score is None and e.na_reason else ""
            lines.append(f"- {e.criterion} {score}, {e.verification.status}{why}" + ("" if ok else " (not counted)"))
        lines.append(f"Holes declared: {len(st.holes)}.")
        goons = _goon_lines(st, ("conclusion", "extrapolation"), ("drafter",)) \
            + _goon_lines(st, ("data",), ("extractor",))[:2]
    elif stage == "verify_summary":
        brief = st.summary
        if brief is None:
            lines.append("Brief: none was written.")
        else:
            s = brief.stats
            lines.append(f"Brief: {s.sentences} sentences (pass {s.passed}, fixed {s.fixed}), {s.failed} removed by "
                         f"repair, {s.words} words, about {s.est_pages:g} pages.")
            summ = [a for a in st.agents.values() if a.role == "summarizer" and a.status in ("done", "failed")]
            if summ:
                goons = [f"- {summ[-1].agent_id}, {summ[-1].task}: rewrites the whole brief (costly)"]
    stopped = _stopped_line(st)
    if stopped:
        lines.append(stopped)
    lines.append("Goons you may send back (at most 3, each once):" if goons else "Goons you may send back: none.")
    lines += goons
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code validates Bobby's call
# ---------------------------------------------------------------------------


def validate_reworks(ctx: RunContext, stage: str, orders: list) -> tuple[list[m.ReworkOrder], list[str]]:
    """The rework orders code accepts, and one reason per order it rejects."""
    from . import rework
    from .agents.bobby import clip

    st = ctx.state
    accepted: list[m.ReworkOrder] = []
    rejected: list[str] = []
    _, _, left = budget_left(ctx)
    left -= REST_OF_RUN_USD.get(stage, 0.0)
    for order in orders:
        aid = (order.agent_id or "").strip()
        text = clip(order.instructions)
        agent = st.agents.get(aid)
        reason = None
        if len(accepted) >= MAX_REWORKS:
            reason = f"at most {MAX_REWORKS} goons per gate"
        elif any(o.agent_id == aid for o in accepted):
            reason = "already sent back at this gate"
        elif agent is None:
            reason = "no such agent"
        elif stopped_by_analyst(st, aid):
            reason = "stopped by the analyst, and Bobby never undoes that"
        elif not text:
            reason = "no instructions"
        if reason is None:
            try:
                rework.check_rerunnable(st, agent)
                rework.plan_for(ctx, agent)
            except InvalidState as e:
                reason = str(e)
        if reason is None:
            cost = rework.RESERVE_USD.get(agent.role, 0.5)
            if cost > left:
                reason = f"the budget left (${max(left, 0):.2f} after the rest of the run) can't cover a {agent.role} rerun (${cost:.2f})"
            else:
                left -= cost
        if reason is None:
            accepted.append(m.ReworkOrder(agent_id=aid, instructions=text))
        else:
            rejected.append(f"{aid or '(no id)'}: {reason}")
    return accepted, rejected


def record(ctx: RunContext, stage: str, decision: str, summary: str, reworks: list[m.ReworkOrder] | None = None,
           comment_id: str | None = None) -> m.AgentDecisionData:
    """Store one of Bobby's calls and emit agent.decision."""
    data = m.AgentDecisionData(agent_id=BOBBY_ID, stage=stage, decision=decision, summary=summary,
                               reworks=list(reworks or []), comment_id=comment_id)
    ctx.state.decisions.append(data)
    what = {"proceed": "proceed", "rework": f"rework {', '.join(r.agent_id for r in data.reworks)}",
            "escalate": "escalate to the analyst"}[decision]
    where = f"comment {comment_id}" if comment_id else f"after {stage}"
    ctx.emit("agent.decision", data, agent_id=BOBBY_ID, stage=stage,
             level="warn" if decision == "escalate" else "info", message=f"Bobby, {where}: {what}. {summary}")
    return data


def _log(ctx: RunContext, text: str, level: str = "info") -> None:
    agent = bobby(ctx)
    ctx.emit("agent.log", m.AgentLogData(text=text, transcript_path=agent.transcript_path if agent else None),
             agent_id=BOBBY_ID, level=level, message=f"bobby: {text}")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def after_stage(ctx: RunContext, stage: str) -> None:
    """Bobby's call after one of GATE_STAGES, validated by code, then the reworks it accepted (awaited).
    Raises StopRequested on STOP. Any other error goes to the orchestrator, which logs it and proceeds."""
    if stage not in GATE_STAGES or bobby(ctx) is None:
        return
    if bobby_off(ctx):
        _log(ctx, f"Gate after {stage}: {OFF}", level="warn")
        record(ctx, stage, "proceed", OFF)
        return
    from .agents import bobby as bobby_agent

    try:
        call = await bobby_agent.gate_call(ctx, stage, build_digest(ctx, stage))
    except Blocked:
        if not bobby_off(ctx):
            raise                         # budget or a gateway error: the orchestrator logs it and proceeds
        call = None                       # stopped while he was thinking: the gateway discarded his answer
    if call is None or bobby_off(ctx):    # stopped while he was thinking: his call is discarded
        _log(ctx, f"Gate after {stage}: Bobby was stopped before his call landed. {OFF}", level="warn")
        record(ctx, stage, "proceed", OFF)
        return
    summary = bobby_agent.clip(call.summary, 600) or f"{call.decision} after {stage}"
    accepted: list[m.ReworkOrder] = []
    if call.decision == "rework":
        accepted, rejected = validate_reworks(ctx, stage, call.reworks)
        if rejected:
            summary += " Code rejected: " + "; ".join(rejected) + "."
        decision = "rework" if accepted else "proceed"
        if not accepted:
            summary += " Nothing to rework, so the run proceeds."
    else:
        decision = call.decision
        if call.reworks:
            summary += f" Code ignored the {len(call.reworks)} rework order(s): the call was {decision}."
    record(ctx, stage, decision, summary, accepted)
    if accepted:
        await run_reworks(ctx, accepted)


async def run_reworks(ctx: RunContext, orders: list[m.ReworkOrder]) -> None:
    """Start every accepted rework (recheck on, by the orchestrator) and wait for them all. STOP wins."""
    from . import cache, rework

    # With the model cache on (record, replay, replay_only), reworks run one at a time in Bobby's order, so a
    # recording and its replay hand out the same new D- ids and later prompts match. Live, they run in parallel.
    serial = ctx.settings.cache_models in cache.MODEL_CACHE_MODES
    tasks, results = [], []
    for o in orders:
        try:
            _, task = rework.start_rerun(ctx, o.agent_id, o.instructions, True, by="orchestrator")
        except InvalidState as e:          # the state moved since validation
            _log(ctx, f"Rework of {o.agent_id} not started: {e}", level="warn")
            continue
        tasks.append(task)
        if serial:
            results += await asyncio.gather(task, return_exceptions=True)
    if not serial:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    stop = next((r for r in results if isinstance(r, StopRequested)), None)
    if stop is not None:
        raise stop
    for o, r in zip(orders, results):
        if isinstance(r, BaseException):
            _log(ctx, f"Rework of {o.agent_id} failed: {type(r).__name__}: {r}", level="warn")
    ctx.check_stop()
