"""Stage verify: code checks, then three isolated judges per conclusion and extrapolation (two of three pass).

Order matters because checks read the cited items' status: conclusions first, then extrapolations and
charts. Code checks run first. A claim that fails or is blocked by code is not sent to the judges (the
repair stage re-checks it after a fix). Each judge is its own agent (verifier-<item id>-<k>) with its own
model call and sees only the claim, the cited items and, for a score, the rubric entry. Votes are counted
by code and recorded as votes_for / votes_total, which feed the certainty score.

A score's judges see its value (+1, 0, -1 or N/A), its scoring type, the company, the rubric entry with how
its direction is read, and the text, and answer two questions: is the text supported, and does the score
follow from it under the rubric (B-fix-score). A judge that finds the direction wrong votes against, under
the same check name (support). Repair stays weaken-only (stages/repair.py): a direction failure can move a
judgment score toward 0 or N/A, never flip its sign, and a threshold score stays code's.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

from ..agents import judge
from ..agents.base import AgentOutputError, AgentRunner
from ..contract import RUBRIC, m
from ..gateway import Blocked
from ..ids import now_iso
from ..runtime import StopRequested
from ..verify import checks
from .finalize import CertaintyBook, emit_verified, label

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

JUDGES = 3
NEEDED = 2


# ---------------------------------------------------------------------------
# What a judge (and the repairer) sees
# ---------------------------------------------------------------------------


def plain(x: Decimal | float) -> str:
    """A number as a plain string, no exponent: 403000000, -0.21. Floats go through repr, so 0.1 stays 0.1."""
    d = x if isinstance(x, Decimal) else Decimal(repr(x))
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "0") else s


def data_view(d: m.DataItem) -> dict:
    """A data item as evidence: statement, value, period, tier and the exact evidence quote.

    expr_value sits next to the as-reported value: what the item's id stands for in a computation
    (checks.base_value: base units, the sign as reported, a percentage as a fraction). Judges misread signs
    from the words (G5b C-003, B-fix-yield C-064 and C-065: "EBIT less a −375 amortization" read as not
    adding it back), so code shows them the signed number the arithmetic used."""
    v = d.value
    out = {"id": d.id, "statement": d.statement, "source_tier": d.source_tier}
    if v is not None:
        out.update({"value": v.as_reported, "expr_value": plain(checks.base_value(d)), "unit": v.unit,
                    "scale": v.scale, "period": v.period})
    out["quote"] = " | ".join(c.quote for c in d.citations if c.role == "evidence")
    return out


def _computation_view(x: m.Computation, **extra) -> dict:
    """A computation with code's exact result, so a judge checks the words against the real arithmetic."""
    return {"name": x.name, "expr": x.expr, "result": plain(x.result), "display": x.display, **extra}


def conclusion_view(c: m.Conclusion) -> dict:
    return {"id": c.id, "text": c.text, "computations": [_computation_view(x) for x in c.computations]}


DIRECTION_JUDGMENT = ("Where an anchor is null: ahead of peers, or strong on the criterion, is +1; behind peers, "
                      "or weak, is -1; mixed or unclear is 0.")
DIRECTION_THRESHOLD = ("Code sets the score from the rule input against the bars: plus_one met is +1, minus_one met "
                       "is -1, neither is 0.")
SET_BY = {"threshold": "code, from rule_input against the rubric bar", "judgment": "the drafter, under the rubric"}


def rubric_view(criterion: str) -> dict:
    """The rubric entry a score is judged on, plus how its direction is read (B-fix-score): rotic_vs_peers has no
    anchors, so without the direction line a judge had nothing to hold a +1 against."""
    crit = RUBRIC["criteria"].get(criterion, {})
    out = {"criterion": criterion, "label": crit.get("label"), "scoring": crit.get("scoring"),
           "plus_one": crit.get("plus_one"), "minus_one": crit.get("minus_one"),
           "evidence_hint": crit.get("evidence_hint"), "scale": "-1, 0, +1, or null for N/A"}
    if crit.get("zero"):
        out["zero"] = crit["zero"]
    out["direction"] = DIRECTION_THRESHOLD if crit.get("scoring") == "threshold" else DIRECTION_JUDGMENT
    return out


def claim_view(item: m.Item) -> dict:
    """The claim as a judge (and the repairer) sees it. A score shows its value the way the rubric writes it
    (score_shown: +1, 0, -1 or N/A), its scoring type and who set it, next to its text, so its direction can be
    checked against the text. score stays the plain number for code and the repairer."""
    if item.kind == "conclusion":
        return {"kind": "conclusion", "text": item.text,
                "computations": [_computation_view(x, checked_by_code=True) for x in item.computations]}
    if item.kind == "extrapolation":
        return {"kind": "score", "criterion": item.criterion, "label": label(item.criterion),
                "scoring": item.rule.type, "score": item.score, "score_shown": judge.shown(item.score),
                "score_set_by": SET_BY.get(item.rule.type, SET_BY["judgment"]), "na_reason": item.na_reason,
                "confidence": item.confidence, "text": item.text, "rule_input": item.rule.input}
    return {"kind": item.kind, "text": getattr(item, "text", "")}


def judge_claim_view(state: RunState, item: m.Item) -> dict:
    """claim_view plus, for a score, the company it is about, so "ahead of peers" has a subject."""
    view = claim_view(item)
    if item.kind == "extrapolation":
        view["company"] = state.run.company
    return view


def evidence_view(state: RunState, item: m.Item) -> list[dict]:
    """Only the items the claim cites."""
    items = state.items
    if item.kind == "conclusion":
        return [data_view(items[i]) for i in item.data_ids if i in items and items[i].kind == "data"]
    if item.kind == "extrapolation":
        return [conclusion_view(items[i]) for i in item.conclusion_ids if i in items and items[i].kind == "conclusion"]
    return []


def claim_text(item: m.Item) -> str:
    if item.kind == "data":
        return item.statement
    return item.title if item.kind == "chart" else item.text


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------


def next_judge_k(state: RunState, item_id: str) -> int:
    """Judge numbers keep counting across re-checks, so every judge agent keeps its own record."""
    prefix = f"verifier-{item_id}-"
    ks = [int(a[len(prefix):]) for a in state.agents if a.startswith(prefix) and a[len(prefix):].isdigit()]
    return max(ks, default=0) + 1


async def _one_judge(ctx: RunContext, item_id: str, k: int, prompt: str) -> judge.JudgeVerdict | None:
    """One isolated vote. None when the judge could not answer (blocked or invalid twice)."""
    runner = AgentRunner(ctx, "verifier", f"Judge {item_id} (vote {k})", [item_id], "small",
                         agent_id=f"verifier-{item_id}-{k}")
    try:
        with runner:
            out = await runner.ask(judge.SYSTEM, prompt, judge.JudgeVerdict, max_tokens=judge.MAX_TOKENS)
    except StopRequested:
        raise
    except (Blocked, AgentOutputError):
        return None
    return out  # type: ignore[return-value]


def direction_wrong(item: m.Item, v: judge.JudgeVerdict) -> bool:
    """A judge found a score's direction wrong. Code counts that as a vote against, even if the verdict says
    SUPPORTED: the judge's own answer to the direction question wins over a slip in the verdict."""
    return item.kind == "extrapolation" and v.score_direction == "wrong"


def against(item: m.Item, v: judge.JudgeVerdict) -> bool:
    return v.verdict == "UNSUPPORTED" or direction_wrong(item, v)


def dissent_of(item: m.Item, v: judge.JudgeVerdict) -> str:
    """A dissenting judge's reason, prefixed when it is about the score's direction, so the detail names it."""
    reason = v.reason.strip()
    if direction_wrong(item, v):
        reason = f"Score direction wrong ({judge.shown(item.score)}): {reason}"
    return reason[:300]


def tally(item: m.Item, votes: list[judge.JudgeVerdict | None], model_id: str | None) -> m.CheckResult:
    """Two of three pass it. A missing vote never counts as support, and can make the check blocked. For a score,
    a judge that found the direction wrong votes against (B-fix-score), and its reason leads the detail."""
    text = claim_text(item)
    answered = [v for v in votes if v is not None]
    no = [v for v in answered if against(item, v)]
    yes = len(answered) - len(no)
    no.sort(key=lambda v: not direction_wrong(item, v))      # a direction dissent first, then the others
    spans: list[str] = []
    for v in no:
        for s in v.unsupported_spans:
            s = s.strip()
            if s and s in text and s not in spans:
                spans.append(s)
    dissent = dissent_of(item, no[0]) if no else ""
    if yes >= NEEDED:
        result = "pass"
        detail = (f"Supported by {yes} of {len(votes)} judges" if not no and len(answered) == len(votes)
                  else f"Supported by {yes} of {len(votes)} judges." + (f" Dissent: {dissent}" if dissent else ""))
    elif yes + (len(votes) - len(answered)) >= NEEDED:
        result = "blocked"
        detail = (f"Only {len(answered)} of {len(votes)} judges answered ({yes} supported), "
                  f"so the claim could not be judged")
    else:
        result = "fail"
        detail = f"Not supported: {yes} of {len(votes)} judges said supported." + (f" {dissent}" if dissent else "")
    return m.CheckResult(check="support", method="llm_judge", result=result, detail=detail, model_id=model_id,
                         agent_id=f"verifier-{item.id}", unsupported_spans=spans if result != "pass" else [],
                         votes_for=yes, votes_total=len(votes))


async def judge_claim(ctx: RunContext, item: m.Item) -> m.CheckResult:
    """Three isolated judges in parallel on one claim. A score's judges also answer whether its direction
    follows from the text and the cited conclusions under the rubric."""
    ctx.check_stop()
    rubric = rubric_view(item.criterion) if item.kind == "extrapolation" else None
    prompt = judge.judge_prompt(judge_claim_view(ctx.state, item), evidence_view(ctx.state, item), rubric)
    k0 = next_judge_k(ctx.state, item.id)
    votes = await asyncio.gather(*(_one_judge(ctx, item.id, k0 + j, prompt) for j in range(JUDGES)))
    return tally(item, list(votes), ctx.settings.model_small)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def code_checks(state: RunState, item: m.Item) -> list[m.CheckResult]:
    """Every code check for a reasoning item. Data items were checked in the extract stage."""
    items, sources = state.items, state.sources_by_id()
    if item.kind == "conclusion":
        return (checks.check_conclusion(item, items, sources, rubric=RUBRIC)
                + checks.check_tier_rule(item, items, sources))
    if item.kind == "extrapolation":
        return checks.check_extrapolation(item, items, RUBRIC, sources=sources)
    if item.kind == "chart":
        return checks.check_chart(item, items, sources=sources)  # a chart behind a revoked source is blocked
    return list(item.verification.checks)


def wants_judges(item: m.Item) -> bool:
    return item.kind == "conclusion" or (item.kind == "extrapolation" and item.score is not None)


async def run_checks(ctx: RunContext, item: m.Item) -> list[m.CheckResult]:
    """Code checks, then the judges when code passed and the claim is semantic."""
    found = code_checks(ctx.state, item)
    if checks.status_from(found) != "pass" or not wants_judges(item):
        return found
    return found + [await judge_claim(ctx, item)]


async def verify_item(ctx: RunContext, item_id: str, book: CertaintyBook) -> str:
    """Check one pending item, store its verification and emit item.verified. Returns the status."""
    item = ctx.state.items[item_id]
    found = await run_checks(ctx, item)
    item = ctx.state.items[item_id]
    status = checks.status_from(found)
    item.verification = m.Verification(status=status, checks=found, attempts=list(item.verification.attempts),
                                       verified_at=now_iso())
    emit_verified(ctx, item, book)
    return status


def pending(state: RunState, kinds: tuple[str, ...]) -> list[str]:
    return [i.id for i in state.items.values() if i.kind in kinds and i.verification.status == "pending"]


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    book = CertaintyBook(ctx.state)
    await asyncio.gather(*(verify_item(ctx, iid, book) for iid in pending(ctx.state, ("conclusion",))))
    ctx.check_stop()
    rest = pending(ctx.state, ("extrapolation", "chart"))
    await asyncio.gather(*(verify_item(ctx, iid, book) for iid in rest))
