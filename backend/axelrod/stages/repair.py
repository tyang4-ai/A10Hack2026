"""Stage repair: weaken failed items toward the evidence, at most two attempts, then holes and finalize_report.

Actions: recompute (code redoes a computation or a threshold score), downgrade (drop refs that failed, or a
repairer rewrite toward the evidence), delete (drop a claim) and not_determinable (a score becomes N/A).
Code makes every repair it can by itself, and the repairer agent only rewrites wording.

Models can only weaken claims. Code refuses a model repair that strengthens one: a stronger judgment score,
a higher data coverage, a new citation or an unchanged text. Code itself is not bound that way for threshold
criteria: it recomputes the score from the rubric's bar and the unrounded rule input, and its score wins,
which can move a score either way (up as well as down). Every repair is checked again by code and by three
fresh judges.

Scores, by rubric kind:
- Numeric threshold (rotic_level "> 15%" / "< 10%", liquidity "> 2x" / "< 1x"): the score is the one the
  threshold_rule check accepts for the unrounded rule input, and nothing else. The repairer may narrow the
  text, lower the data coverage or declare N/A. Any other score it proposes is refused and code's score and
  code's own sentence stand (the refusal is in the attempt note). A display that rounds onto the bar
  (9.996% shown as 10.0% against "< 10%") is spelled out in that sentence, so judges see why it is -1.
- Judgment (no numeric bar): the score may stay or move to 0, never away from 0, and N/A never becomes a
  score. A stronger proposal is refused and the score becomes N/A with the refusal as its note.

Order: conclusions first, then the scores and charts that cite them are checked again, then those still
failing are repaired. Blocked items (a revoked, quarantined or rejected source behind them) are left alone,
and finalize declares them as holes. An item still failing after two repairs stays `fail` and becomes a hole.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ..agents import repairer
from ..agents.base import AgentOutputError, AgentRunner
from ..contract import RUBRIC, m
from ..gateway import Blocked
from ..ids import now_iso
from ..runtime import StopRequested
from ..verify import checks, compute, text as vtext
from ..verify.numbers import WORD_FACTOR, half_up, scan_numbers
from .draft import point_value
from .finalize import PASSING, CertaintyBook, emit_verified, finalize_report, label
from .verify import claim_text, claim_view, evidence_view, rubric_view, run_checks

if TYPE_CHECKING:
    from ..runtime import RunContext

REPAIRS = ("recite", "recompute", "downgrade", "delete", "not_determinable", "reword")
MAX_REPAIRS = 2
GIVE_UP_NOTE = "Still failing after two repairs. Kept as fail and declared as a hole, excluded from scoring."
_CMP = re.compile(r"(>=|<=|≥|≤|>|<)\s*(\d+(?:\.\d+)?)\s*(%|x|y)?")
_ID = re.compile(r"\b[A-Z]{1,4}-\d{1,4}\b")
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Proposal:
    """One repair: the action, a readable note, and the repaired item (None: the item stays fail)."""
    action: str
    note: str
    item: m.Item | None = None


def _failing(v: m.Verification) -> set[str]:
    return {c.check for c in v.checks if c.result == "fail"}


def _passing(items: dict, iid: str, kind: str) -> bool:
    it = items.get(iid)
    return it is not None and it.kind == kind and it.verification.status in PASSING


def _and(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


# ---------------------------------------------------------------------------
# Conclusions (code repairs)
# ---------------------------------------------------------------------------


def keep_computations(comps: list[m.Computation], ids: set[str]) -> list[m.Computation]:
    """Computations that only read the kept ids and earlier kept computations."""
    out: list[m.Computation] = []
    names: set[str] = set()
    for comp in comps:
        used_ids = set(compute.ids_in(comp.expr))
        used_names = set(re.findall(r"\b[A-Za-z_]\w*\b", _ID.sub(" ", comp.expr)))
        if used_ids <= ids and used_names <= names:
            out.append(comp)
            names.add(comp.name)
    return out


def _drop_data_refs(c: m.Conclusion, items: dict) -> Proposal | None:
    good = [i for i in c.data_ids if _passing(items, i, "data")]
    bad = [i for i in c.data_ids if i not in good]
    if not bad:
        return None
    if not good:
        return Proposal("delete", f"No cited data item is still verified ({_and(bad)}). The claim is dropped.")
    new = c.model_copy(update={"data_ids": good, "computations": keep_computations(c.computations, set(good))})
    return Proposal("downgrade", f"Dropped {_and(bad)}, which did not pass. The claim now rests on {_and(good)}.", new)


def redisplay(value: Decimal, old: str) -> str | None:
    """The display for value in the old display's format (percent, scale word, decimals, separators)."""
    s = vtext.normalize(old)
    toks = scan_numbers(s, include_labels=True)
    if not toks:
        return None
    t = toks[0]
    x = value * 100 if t.pct else value / WORD_FACTOR[t.word] if t.word else value
    shown = Decimal(half_up(x, t.places))
    if (shown < 0) != (t.number < 0) and shown != 0:
        return None  # the sign flipped: no faithful way to keep the old format
    mag = abs(shown)
    core = f"{mag:,}" if "," in t.core else format(mag, "f")
    return s[:t.core_start] + core + s[t.core_end:]


def replace_number(text: str, old: str, new: str) -> str:
    """Replace a display in text, never inside a longer number ("2.5" stays put in "12.5%")."""
    return re.sub(rf"(?<![\d.,]){re.escape(old)}(?![\d])", lambda _: new, text)


def recompute_conclusion(c: m.Conclusion, items: dict) -> Proposal | None:
    """Redo every computation by code. A wrong display is rewritten in the text as well."""
    env = {i: checks.base_value(items[i]) for i in c.data_ids
           if i in items and items[i].kind == "data" and items[i].value is not None}
    text, comps, changes = c.text, [], []
    for comp in c.computations:
        try:
            val = compute.safe_eval(comp.expr, env)
        except compute.ComputeError as e:
            changes.append(f"dropped {comp.name} ({e})")
            continue
        env[comp.name] = val
        display = comp.display
        if not checks.display_of(val, comp.display)[0]:
            display = redisplay(val, comp.display)
            if display is None:
                changes.append(f"dropped {comp.name} (its sign changed)")
                continue
            text = replace_number(text, comp.display, display)
            changes.append(f"{comp.name} display {comp.display} corrected to {display}")
        result = float(val)
        if abs(Decimal(repr(result)) - Decimal(repr(comp.result))) > abs(val) * Decimal("1e-9") + Decimal("1e-12"):
            changes.append(f"{comp.name} result corrected to {half_up(val, 6)}")
        comps.append(m.Computation(name=comp.name, expr=comp.expr, result=result, display=display))
    if not changes:
        return None
    new = c.model_copy(update={"text": text, "computations": comps})
    return Proposal("recompute", "Recomputed by code: " + "; ".join(changes) + ".", new)


# ---------------------------------------------------------------------------
# Extrapolations (code repairs)
# ---------------------------------------------------------------------------


def not_determinable(e: m.Extrapolation, items: dict, reason: str, note: str) -> Proposal:
    """The score becomes N/A with a stated reason. Cited conclusions that still pass stay as context."""
    keep = [i for i in e.conclusion_ids if _passing(items, i, "conclusion")]
    new = e.model_copy(update={"score": None, "na_reason": reason, "text": f"{label(e.criterion)} is not scored.",
                               "conclusion_ids": keep, "rule": e.rule.model_copy(update={"input": None}),
                               "confidence": "low"})
    return Proposal("not_determinable", note, new)


def _bar(s: str | None) -> tuple[str, Decimal, str] | None:
    found = _CMP.findall(s or "")
    if len(found) != 1:
        return None
    op, num, unit = found[0]
    return op, Decimal(num), unit


def _meets(x: Decimal, bar: tuple[str, Decimal, str]) -> bool:
    op, num, unit = bar
    t = num / 100 if unit == "%" else num
    return {">": x > t, "<": x < t, ">=": x >= t, "≥": x >= t, "<=": x <= t, "≤": x <= t}[op]


def _bar_text(bar: tuple[str, Decimal, str]) -> str:
    return f"{bar[0]} {bar[1]}{bar[2]}"


def _signed(score: int | None) -> str:
    if score is None:
        return "N/A"
    return f"{score:+d}" if score else "0"


# ---------------------------------------------------------------------------
# Threshold scores: code's recomputation is the score
# ---------------------------------------------------------------------------


@dataclass
class CodeScore:
    """The score code gets from a numeric rubric bar, and the sentence code writes for it."""
    score: int
    rule_input: str
    display: str
    shown_as: str    # the display, plus the side of the bar before rounding when the display rounds onto it
    why: str
    text: str


def numeric_bars(criterion: str) -> tuple[tuple[str, Decimal, str], tuple[str, Decimal, str]] | None:
    """(+1 bar, -1 bar) when the rubric scores the criterion by numeric thresholds. Judgment criteria and
    worded bars ("Followed closely for more than a year") give None: code cannot recompute those."""
    crit = RUBRIC["criteria"].get(criterion) or {}
    if crit.get("scoring") != "threshold":
        return None
    plus, minus = _bar(crit.get("plus_one")), _bar(crit.get("minus_one"))
    return (plus, minus) if plus is not None and minus is not None else None


def _rule_input(e: m.Extrapolation, items: dict) -> tuple[m.Conclusion, m.Computation] | None:
    cid, _, name = (e.rule.input or "").partition(":")
    c = items.get(cid)
    if c is None or c.kind != "conclusion" or cid not in e.conclusion_ids:
        return None
    comp = next((x for x in c.computations if x.name == name), None)
    return (c, comp) if comp is not None else None


def checked_score(e: m.Extrapolation, items: dict) -> int | None:
    """The one score the threshold_rule check passes for this rule input, or None when none does. Asking the
    check itself keeps repair and verify on one rule (it compares the unrounded value, not the display)."""
    for s in (-1, 0, 1):
        found = checks.check_extrapolation(e.model_copy(update={"score": s}), items, RUBRIC)
        r = next((x for x in found if x.check == "threshold_rule"), None)
        if r is None:
            return None
        if r.result == "pass":
            return s
    return None


def _bar_value(bar: tuple[str, Decimal, str]) -> Decimal:
    return bar[1] / 100 if bar[2] == "%" else bar[1]


def _shown_score(display: str, plus: tuple, minus: tuple) -> int | None:
    """The score the rounded display alone would suggest ("10.0%" against "< 10%" suggests 0)."""
    toks = scan_numbers(vtext.normalize(display), include_labels=True)
    if not toks:
        return None
    t = toks[0]
    n = t.number * WORD_FACTOR[t.word] if t.word else t.number
    x = n / 100 if t.pct else n
    return 1 if _meets(x, plus) else -1 if _meets(x, minus) else 0


def code_score(e: m.Extrapolation, items: dict) -> CodeScore | None:
    """Code's score for a numeric-threshold criterion and its sentence, or None (judgment criterion, worded
    bar, or a rule input that does not recompute). When the display rounds onto the bar, the sentence says
    which side of the bar the unrounded value is on, using only numbers the checks can trace."""
    bars = numeric_bars(e.criterion)
    found = _rule_input(e, items) if bars is not None else None
    if bars is None or found is None:
        return None
    score = checked_score(e, items)
    if score is None:
        return None
    (plus, minus), (c, comp) = bars, found
    try:
        x = checks.conclusion_env(c, items)[comp.name]
    except (compute.ComputeError, KeyError):
        return None
    why = {1: f"meets the +1 bar ({_bar_text(plus)})", -1: f"meets the -1 bar ({_bar_text(minus)})"}.get(
        score, f"sits between the bars ({_bar_text(minus)} and {_bar_text(plus)})")
    shown = _shown_score(comp.display, plus, minus)
    if shown is not None and shown != score:
        bar = minus if score == -1 or (score == 0 and shown == -1) else plus
        side = f"{'below' if x < _bar_value(bar) else 'above'} {bar[1]}{bar[2]}"
        shown_as = f"{comp.display} after rounding ({side} before rounding)"
        value = (f"the recomputed value shows as {comp.display} after rounding, but before rounding it is {side}, "
                 f"so it {why}")
    else:
        shown_as = comp.display
        value = f"the recomputed value {comp.display} {why}"
    text = f"{label(e.criterion)} scores {_signed(score)} under the framework: {value}."
    return CodeScore(score=score, rule_input=e.rule.input or "", display=comp.display, shown_as=shown_as, why=why,
                     text=text)


def recompute_threshold(e: m.Extrapolation, items: dict) -> Proposal:
    """The score the threshold_rule check accepts for the recomputed input. The text is rebuilt by code."""
    found = _rule_input(e, items)
    if numeric_bars(e.criterion) is None or found is None:
        return not_determinable(e, items, "The threshold input could not be recomputed from a verified conclusion.",
                                "Code: the rule input names no cited computation, so no score can be recomputed.")
    try:
        checks.conclusion_env(found[0], items)
    except compute.ComputeError as err:
        return not_determinable(e, items, f"The threshold input could not be recomputed ({err}).",
                                f"Code: {e.rule.input} does not recompute.")
    cs = code_score(e, items)
    if cs is None:
        return not_determinable(e, items, "The threshold input could not be recomputed from a verified conclusion.",
                                f"Code: no score passes the threshold check for {e.rule.input}.")
    if cs.score == e.score:
        # The failure came from a stale check (a cited conclusion changed since). The score already matches
        # code, so code's sentence is checked again rather than giving the score up.
        return Proposal("recompute", f"Code recomputed {e.rule.input} = {cs.shown_as}: the score "
                                     f"{_signed(cs.score)} stands, checked again with code's sentence.",
                        e.model_copy(update={"text": cs.text}))
    new = e.model_copy(update={"score": cs.score, "text": cs.text})
    return Proposal("recompute", f"Code recomputed {e.rule.input} = {cs.shown_as}, which {cs.why}: score "
                                 f"{_signed(e.score)} became {_signed(cs.score)}.", new)


def repair_extrapolation_by_code(e: m.Extrapolation, items: dict) -> Proposal | None:
    failing = _failing(e.verification)
    if "threshold_rule" in failing:
        return recompute_threshold(e, items)
    if "citations_resolve" in failing:
        if e.score is None and not (e.na_reason or "").strip():
            return not_determinable(e, items, "Not determinable from the verified evidence.",
                                    "Code: an N/A needs a stated reason.")
        good = [i for i in e.conclusion_ids if _passing(items, i, "conclusion")]
        bad = [i for i in e.conclusion_ids if i not in good]
        if e.score is not None and not good:
            why = f"{_and(bad)} did not pass" if bad else "it cites no conclusion"
            return not_determinable(e, items, f"No verified conclusion supports a score ({why}).",
                                    f"Code: {why}, so the score is not determinable.")
        if bad:
            if e.score is not None and e.rule.input and e.rule.input.split(":")[0] in bad:
                return not_determinable(e, items, f"The threshold input {e.rule.input} did not pass verification.",
                                        f"Code: the rule input's conclusion failed, so the score is not determinable.")
            new = e.model_copy(update={"conclusion_ids": good})
            return Proposal("downgrade", f"Dropped {_and(bad)}, which did not pass.", new)
    if e.score is None:
        plain = f"{label(e.criterion)} is not scored."
        if e.text != plain:
            return Proposal("downgrade", "Code: the N/A text is reset to a plain statement.",
                            e.model_copy(update={"text": plain}))
        return Proposal("not_determinable", "Code: the N/A item fails for a reason repair cannot fix.")
    return None


# ---------------------------------------------------------------------------
# Charts (code only)
# ---------------------------------------------------------------------------


def rebuild_chart(g: m.Chart, items: dict) -> Proposal:
    """Recompute every point from its ref and drop points whose ref didn't pass."""
    series, dropped = [], 0
    for s in g.series:
        pts = []
        for p in s.points:
            base = p.ref.split(":")[0]
            ok = base in items and items[base].verification.status in PASSING
            y = point_value(p.ref, g.unit, g.scale, items) if ok else None
            if y is None:
                dropped += 1
                continue
            pts.append(m.ChartPoint(x=p.x, y=float(half_up(y, 1)), ref=p.ref))
        if pts:
            series.append(m.ChartSeries(name=s.name, points=pts))
    if not series:
        return Proposal("delete", "No plotted point has a verified value left. The chart is dropped.")
    refs = list(dict.fromkeys(p.ref.split(":")[0] for s in series for p in s.points))
    new = g.model_copy(update={"series": series, "refs": refs})
    if new.series == g.series and new.refs == g.refs:
        return Proposal("delete", "Rebuilding the chart from its refs changed nothing. The chart is dropped.")
    if dropped:
        return Proposal("downgrade", f"Code dropped {dropped} point(s) whose ref did not pass and rebuilt the rest.", new)
    return Proposal("recompute", "Code recomputed every point from its ref.", new)


# ---------------------------------------------------------------------------
# The repairer agent (wording only)
# ---------------------------------------------------------------------------


def _apply_conclusion(c: m.Conclusion, out: repairer.RepairOutput) -> Proposal:
    if out.action != "downgrade":
        return Proposal("delete", f"Repairer: {out.note.strip()}")
    text = out.text.strip()
    if not text or text == c.text:
        return Proposal("delete", "The repairer gave no weaker text. The claim is dropped.")
    keep = [i for i in dict.fromkeys(out.keep_ids) if i in c.data_ids] or list(c.data_ids)
    dropped = [i for i in c.data_ids if i not in keep]
    new = c.model_copy(update={"text": text, "data_ids": keep,
                               "computations": keep_computations(c.computations, set(keep))})
    tail = f" Dropped {_and(dropped)}." if dropped else ""
    return Proposal("downgrade", f"Repairer: {out.note.strip()}{tail}", new)


def judgment_move_ok(old: int | None, new: int | None) -> bool:
    """A judgment score may stay or move to 0, never away from 0. N/A never becomes a score."""
    return new == old or (old is not None and new == 0)


def _lower_confidence(e: m.Extrapolation, proposed: str | None) -> tuple[str, str]:
    """Data coverage may only go down. Returns the coverage to use and a note when a raise was refused."""
    if proposed is None or _CONF_RANK[proposed] <= _CONF_RANK[e.confidence]:
        return proposed or e.confidence, ""
    return e.confidence, f"Code kept data coverage {e.confidence}: the repairer's {proposed} would raise it."


def _kept_conclusions(e: m.Extrapolation, out: repairer.RepairOutput) -> list[str]:
    """A subset of the cited conclusions, always with the rule input's conclusion."""
    keep = [i for i in dict.fromkeys(out.keep_ids) if i in e.conclusion_ids] or list(e.conclusion_ids)
    if e.rule.input:
        cid = e.rule.input.split(":")[0]
        if cid in e.conclusion_ids and cid not in keep:
            keep.append(cid)
    return keep


def _notes(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _apply_threshold(e: m.Extrapolation, out: repairer.RepairOutput, cs: CodeScore) -> Proposal:
    """A numeric-threshold score: code's recomputed score always stands. The repairer may narrow the text and
    lower the data coverage. A different score is refused, and then code's own sentence replaces the text,
    since the repairer's text argued for the refused score."""
    proposed = out.score if out.score is not None else e.score
    conf, conf_note = _lower_confidence(e, out.confidence)
    text = out.text.strip()
    refused = ""
    if proposed != cs.score:
        refused = (f"Code refused the repairer's score {_signed(proposed)}: {cs.rule_input} recomputes to "
                   f"{cs.shown_as}, which {cs.why}, so the score stays {_signed(cs.score)} with code's sentence.")
        text = cs.text
    elif not text or (text == e.text and conf == e.confidence):
        refused = "The repairer gave no weaker version, so code's own sentence for the recomputed score is checked again."
        text = cs.text
    new = e.model_copy(update={"score": cs.score, "confidence": conf, "text": text,
                               "conclusion_ids": _kept_conclusions(e, out)})
    action = "recompute" if refused else "downgrade"
    return Proposal(action, _notes(refused, conf_note, f"Repairer: {out.note.strip()}"), new)


def _apply_extrapolation(e: m.Extrapolation, out: repairer.RepairOutput, items: dict) -> Proposal:
    if out.action != "downgrade":
        reason = (out.na_reason or "").strip() or "The cited evidence does not support a score."
        return not_determinable(e, items, reason, f"Repairer: {out.note.strip()}")
    cs = code_score(e, items)
    if cs is not None:
        return _apply_threshold(e, out, cs)
    if numeric_bars(e.criterion) is not None:
        return not_determinable(e, items, "The threshold input could not be recomputed, so no score can stand.",
                                f"Code: no score passes the threshold check for {e.rule.input or 'a missing rule input'}.")
    score = out.score if out.score is not None else e.score
    if not judgment_move_ok(e.score, score):
        return not_determinable(e, items, "Repair proposed a stronger score, which is not allowed, so it is not scored.",
                                f"Code refused the repair: {_signed(e.score)} to {_signed(score)} moves away from 0, "
                                f"which is not a weakening. Repairer: {out.note.strip()}")
    conf, conf_note = _lower_confidence(e, out.confidence)
    text = out.text.strip()
    if not text or (text == e.text and score == e.score and conf == e.confidence):
        return not_determinable(e, items, "No weaker supported score was found.",
                                "The repairer gave no weaker version, so the score is not determinable.")
    new = e.model_copy(update={"score": score, "confidence": conf, "text": text,
                               "conclusion_ids": _kept_conclusions(e, out)})
    return Proposal("downgrade", _notes(conf_note, f"Repairer: {out.note.strip()}"), new)


async def agent_repair(ctx: RunContext, item: m.Item) -> Proposal:
    """One repairer call for a wording failure (judges or untraced numbers)."""
    state = ctx.state
    v = item.verification
    claim = {"id": item.id, **claim_view(item)}
    failing = [{"check": c.check, "detail": c.detail, "unsupported_spans": c.unsupported_spans}
               for c in v.checks if c.result == "fail"]
    history = [{"attempt": a.attempt, "action": a.action, "before": a.before, "after": a.after, "note": a.note}
               for a in v.attempts]
    rubric = rubric_view(item.criterion) if item.kind == "extrapolation" else None
    cs = code_score(item, state.items) if item.kind == "extrapolation" else None
    fixed = None if cs is None else {"score": cs.score, "rule_input": cs.rule_input, "display": cs.display,
                                     "code_sentence": cs.text}
    prompt = repairer.repair_prompt(claim, failing, evidence_view(state, item), history, rubric, code_score=fixed)
    n = sum(a.action in REPAIRS for a in v.attempts) + 1
    runner = AgentRunner(ctx, "repairer", f"Repair {item.id} (attempt {n})", [item.id], "large")
    giving_up = "not_determinable" if item.kind == "extrapolation" else "delete"
    try:
        with runner:
            out = await runner.ask(repairer.SYSTEM, prompt, repairer.RepairOutput, max_tokens=repairer.MAX_TOKENS)
    except StopRequested:
        raise
    except Blocked as e:
        return Proposal(giving_up, f"The repair agent was blocked by the gateway: {e.reason}")
    except AgentOutputError:
        return Proposal(giving_up, "The repair agent's output failed validation twice.")
    if item.kind == "conclusion":
        return _apply_conclusion(item, out)  # type: ignore[arg-type]
    return _apply_extrapolation(item, out, state.items)  # type: ignore[arg-type]


async def propose(ctx: RunContext, item: m.Item) -> Proposal:
    """Code repairs first. The repairer agent only gets wording failures."""
    items = ctx.state.items
    if item.kind == "conclusion":
        failing = _failing(item.verification)
        if "citations_resolve" in failing:
            p = _drop_data_refs(item, items)
            if p is not None:
                return p
        if "tier_rule" in failing:
            return Proposal("delete", "Only tier 3 sources support it, and repair cannot add support.")
        if "computation" in failing:
            p = recompute_conclusion(item, items)
            if p is not None:
                return p
        return await agent_repair(ctx, item)
    if item.kind == "extrapolation":
        p = repair_extrapolation_by_code(item, items)
        return p if p is not None else await agent_repair(ctx, item)
    if item.kind == "chart":
        return rebuild_chart(item, items)
    return Proposal("delete", "Data items are repaired in the extract stage.")


# ---------------------------------------------------------------------------
# The repair loop
# ---------------------------------------------------------------------------


def _attempt(n: int, status: str, found: list[m.CheckResult], *, action: str | None = None,
             before: str | None = None, after: str | None = None, note: str | None = None) -> m.Attempt:
    return m.Attempt(attempt=n, status=status, checks=list(found), action=action, before=before, after=after,
                     note=note, at=now_iso())


def _give_up(ctx: RunContext, item: m.Item, book: CertaintyBook) -> None:
    v = item.verification
    attempts = [*v.attempts, _attempt(len(v.attempts) + 1, "fail", v.checks, action="not_determinable",
                                      before=claim_text(item), note=GIVE_UP_NOTE)]
    item.verification = m.Verification(status="fail", checks=list(v.checks), attempts=attempts, verified_at=now_iso())
    emit_verified(ctx, item, book, message=f"{item.id} still fails after two repairs: declared as a hole")


def rests_on_blocked(v: m.Verification) -> bool:
    """A revoked, quarantined or rejected source (or a blocked item) sits behind the claim. Rewording cannot
    fix that, so repair leaves it alone even when another check failed, and finalize declares it."""
    return any(c.check == "citations_resolve" and c.result == "blocked" for c in v.checks)


async def repair_item(ctx: RunContext, item_id: str, book: CertaintyBook) -> None:
    """Repair one failed item until it passes, is blocked, is dropped, or has had two repairs."""
    state = ctx.state
    while True:
        item = state.items[item_id]
        v = item.verification
        if v.status != "fail" or rests_on_blocked(v):
            return
        if sum(a.action in REPAIRS for a in v.attempts) >= MAX_REPAIRS:
            _give_up(ctx, item, book)
            return
        ctx.check_stop()
        prop = await propose(ctx, item)
        item = state.items[item_id]
        v = item.verification
        attempts = [*v.attempts, _attempt(len(v.attempts) + 1, "fail", v.checks, action=prop.action,
                                          before=claim_text(item),
                                          after=claim_text(prop.item) if prop.item is not None else None,
                                          note=prop.note)]
        if prop.item is None:
            item.verification = m.Verification(status="fail", checks=list(v.checks), attempts=attempts,
                                               verified_at=now_iso())
            emit_verified(ctx, item, book, message=f"{item_id} {prop.action}: {prop.note}")
            return
        new = prop.item.model_copy(update={"verification": m.Verification(status="pending", attempts=attempts),
                                           "certainty": None})
        state.items[item_id] = new
        ctx.emit("item.updated", m.ItemUpdatedData(item=new, reason="repair"),
                 message=f"{item_id} repaired ({prop.action}): {prop.note}")
        found = await run_checks(ctx, new)
        new = state.items[item_id]
        status = checks.status_from(found)
        if status == "pass":
            status = "fixed"
            attempts = [*attempts, _attempt(len(attempts) + 1, "fixed", found, note="All checks pass")]
        new.verification = m.Verification(status=status, checks=found, attempts=attempts, verified_at=now_iso())
        emit_verified(ctx, new, book)


def _refs(item: m.Item) -> set[str]:
    if item.kind == "extrapolation":
        return set(item.conclusion_ids)
    if item.kind == "chart":
        return {r.split(":")[0] for r in item.refs}
    return set()


def _only_citations(item: m.Item, touched: set[str]) -> bool:
    """Failed only because something it cites had not passed, and one of those was just repaired."""
    return bool(_refs(item) & touched) and _failing(item.verification) <= {"citations_resolve"}


async def recheck(ctx: RunContext, item_id: str, book: CertaintyBook, repaired: set[str]) -> None:
    """Check a failed score or chart again after the conclusions it cites were repaired."""
    item = ctx.state.items[item_id]
    v = item.verification
    found = await run_checks(ctx, item)
    item = ctx.state.items[item_id]
    status = checks.status_from(found)
    attempts = list(v.attempts)
    if status == "pass":
        cited = sorted(_refs(item) & repaired)
        attempts += [_attempt(len(attempts) + 1, "fail", v.checks,
                              note=f"Failed while {_and(cited)} had not passed. Checked again after the repair."),
                     _attempt(len(attempts) + 2, "fixed", found, note="All checks pass")]
        status = "fixed"
    item.verification = m.Verification(status=status, checks=found, attempts=attempts, verified_at=now_iso())
    emit_verified(ctx, item, book)


def _failed(ctx: RunContext, kinds: tuple[str, ...]) -> list[str]:
    return [i.id for i in ctx.state.items.values() if i.kind in kinds and i.verification.status == "fail"]


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    book = CertaintyBook(ctx.state)
    conclusions = _failed(ctx, ("conclusion",))
    await asyncio.gather(*(repair_item(ctx, iid, book) for iid in conclusions))
    ctx.check_stop()
    touched = set(conclusions)
    dependents = [iid for iid in _failed(ctx, ("extrapolation", "chart")) if _only_citations(ctx.state.items[iid], touched)]
    await asyncio.gather(*(recheck(ctx, iid, book, touched) for iid in dependents))
    ctx.check_stop()
    await asyncio.gather(*(repair_item(ctx, iid, book) for iid in _failed(ctx, ("extrapolation", "chart"))))
    ctx.check_stop()
    finalize_report(ctx)
