"""Stage draft: conclusions, BOFFIN scores and charts from verified data items.

In-scope criteria are grouped by the plan's needs (criteria that share a need share a drafter), and one
large-model drafter per group runs in parallel. Each sees only the verified data items that serve its
criteria. Code then turns the drafts into contract items:
- C, E and G ids come from code. The drafter's own keys ("c1") are only used to link scores and charts.
- Refs to data items that don't exist or didn't pass are dropped, and a conclusion left with none is dropped.
- Every computation is evaluated by code (verify.compute.safe_eval). The drafter's display is kept, so a
  wrong display is caught by the computation check and repaired by recompute.
- Chart points are computed by code from their refs. A model never draws a chart.
- Every in-scope criterion gets exactly one extrapolation. A criterion the drafter skipped, or one about the
  analyst rather than the company (research_stage), comes out N/A with a reason, which finalize declares
  as a hole. The data-coverage label can only be lowered by code, never raised.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import TYPE_CHECKING

from ..agents import drafter
from ..agents.base import AgentOutputError, AgentRunner
from ..contract import RUBRIC, m
from ..gateway import Blocked
from ..runtime import StopRequested
from ..verify import checks, compute
from ..verify.numbers import SCALES, half_up, period_key, scan_numbers, to_base
from .finalize import PASSING, in_scope, label

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

# Criteria about the analyst, not the company: no source can state them, so they are never scored by a model.
ANALYST_ONLY = {
    "research_stage": ("Research stage is about the analyst's own coverage history, which no source in this run "
                       "can state. It is declared as a gap instead of guessed."),
    "fundamental_thesis": ("Fundamental thesis is only meaningful against a prior review, and this run has none. "
                           "It is declared as a gap instead of guessed."),
}
_TEN_YEARS = re.compile(r"\b10\s?y\b|\b10[- ]years?\b|\bten years\b", re.IGNORECASE)
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_BAR = re.compile(r"(>=|<=|≥|≤|>|<)\s*\d")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _source_ok(src: m.Source | None) -> bool:
    return src is not None and src.status not in ("revoked", "quarantined") and src.approval != "rejected"


def usable_data(state: RunState) -> list[m.DataItem]:
    """Verified data items whose source still stands. Nothing else reaches a drafter."""
    srcs = state.sources_by_id()
    return [d for d in state.data_items() if d.verification.status in PASSING and _source_ok(srcs.get(d.source_id))]


def criterion_groups(scope: list[str], needs: list[m.DataNeed]) -> list[list[str]]:
    """Union of in-scope criteria that share a need, in scope order. A criterion no need names stands alone."""
    parent = {c: c for c in scope}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for need in needs:
        crit = [c for c in need.criteria if c in parent]
        for c in crit[1:]:
            a, b = find(crit[0]), find(c)
            if a != b:
                parent[max(a, b, key=scope.index)] = min(a, b, key=scope.index)
    groups: dict[str, list[str]] = {}
    for c in scope:
        groups.setdefault(find(c), []).append(c)
    return sorted(groups.values(), key=lambda g: scope.index(g[0]))


def data_row(d: m.DataItem) -> dict:
    """What a drafter sees of one data item: no quotes, no offsets, nothing else."""
    v = d.value
    row = {"id": d.id, "statement": d.statement, "tier": d.source_tier, "criteria": list(d.criteria)}
    if v is not None:
        row.update({"value": v.as_reported, "unit": v.unit, "scale": v.scale, "period": v.period})
    return row


def criterion_entry(criterion: str) -> dict:
    crit = RUBRIC["criteria"][criterion]
    out = {"criterion": criterion, "label": crit["label"], "category": crit["category"], "scoring": crit["scoring"],
           "plus_one": crit.get("plus_one"), "minus_one": crit.get("minus_one"),
           "evidence_hint": crit.get("evidence_hint")}
    if crit.get("zero"):
        out["zero"] = crit["zero"]
    if crit["scoring"] == "threshold" and _BAR.search(crit.get("plus_one") or "") and _BAR.search(crit.get("minus_one") or ""):
        out["numeric_bar"] = True
    if criterion in ANALYST_ONLY:
        out["not_scored"] = True
        out["not_scored_reason"] = ANALYST_ONLY[criterion]
    return out


def metrics_for(group: list[str]) -> dict | None:
    """The rubric's metric definitions, for groups that compute returns."""
    if any("rotic" in c for c in group):
        return {k: v for k, v in RUBRIC.get("metrics", {}).items()}
    return None


def company_view(state: RunState) -> dict:
    return {"name": state.run.company, "ticker": state.run.ticker, "depth": state.run.depth,
            "as_of": state.as_of, "fiscal_note": state.fiscal_note}


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


async def _draft_group(ctx: RunContext, group: list[str], rows: list[dict]) -> tuple[drafter.DraftOutput | None, str]:
    """One drafter for one group. Returns the draft, or None and why."""
    prompt = drafter.draft_prompt(company_view(ctx.state), [criterion_entry(c) for c in group], rows,
                                  metrics_for(group))
    runner = AgentRunner(ctx, "drafter", f"Draft {', '.join(group)}", [r["id"] for r in rows], "large")
    try:
        with runner:
            out = await runner.ask(drafter.SYSTEM, prompt, drafter.DraftOutput, max_tokens=drafter.MAX_TOKENS)
    except StopRequested:
        raise
    except Blocked as e:
        return None, f"The drafter was blocked by the gateway: {e.reason}"
    except AgentOutputError:
        return None, "The drafter's output failed validation twice."
    return out, runner.agent_id  # type: ignore[return-value]


def point_value(ref: str, unit: str, scale: str, items: dict) -> Decimal | None:
    """A chart point's value from its ref, in the chart's unit and scale: a data item's value, or a conclusion's
    computation recomputed by code. None when the ref has no value in that unit."""
    base, _, name = ref.partition(":")
    it = items.get(base)
    if it is None:
        return None
    if not name:
        if it.kind != "data" or it.value is None:
            return None
        v = it.value
        if unit == "%":
            return _dec(v.number) if v.unit == "%" else None
        if v.unit != unit:
            return None
        return to_base(_dec(v.number), v.scale) / SCALES[scale]
    if it.kind != "conclusion":
        return None
    try:
        x = checks.conclusion_env(it, items).get(name)
    except compute.ComputeError:
        return None
    if x is None:
        return None
    return x * 100 if unit == "%" else x / SCALES[scale]


class Builder:
    """Turns drafts into contract items. Code assigns every id, evaluates every computation and computes
    every chart point, and records what it rejected."""

    def __init__(self, ctx: RunContext, verified: dict[str, m.DataItem]) -> None:
        self.ctx = ctx
        self.verified = verified
        self.conclusions: dict[str, m.Conclusion] = {}
        self.keys: dict[tuple[int, str], str] = {}
        self.notes: dict[int, list[str]] = {}
        self.agent_ids: dict[int | None, str] = {}     # group index -> drafter agent id
        self.seen: dict[int, set[str]] = {}             # group index -> data ids its drafter was shown

    def note(self, gi: int, text: str) -> None:
        self.notes.setdefault(gi, []).append(text)

    # -- conclusions ---------------------------------------------------------------

    def _computations(self, gi: int, key: str, dc: drafter.DraftConclusion,
                      data_ids: list[str]) -> list[m.Computation]:
        env: dict[str, Decimal] = {i: checks.base_value(self.verified[i]) for i in data_ids
                                   if self.verified[i].value is not None}
        out: list[m.Computation] = []
        for comp in dc.computations:
            name = comp.name.strip()
            if not _NAME.match(name) or name in env:
                self.note(gi, f"{key}: computation {name!r} dropped (not a fresh snake_case name)")
                continue
            try:
                val = compute.safe_eval(comp.expr, env)
            except compute.ComputeError as e:
                self.note(gi, f"{key}: computation {name} dropped ({e})")
                continue
            env[name] = val
            out.append(m.Computation(name=name, expr=comp.expr, result=float(val), display=comp.display.strip()))
        return out

    def conclusion(self, gi: int, group: list[str], dc: drafter.DraftConclusion) -> m.Conclusion | None:
        key = dc.key.strip() or f"#{len(self.keys) + 1}"
        cited = list(dict.fromkeys(dc.data_ids + [i for c in dc.computations for i in compute.ids_in(c.expr)]))
        shown = self.seen.get(gi, set())
        data_ids = [i for i in cited if i in self.verified and i in shown]
        dropped = [i for i in cited if i not in data_ids]
        if dropped:
            self.note(gi, f"{key}: dropped {', '.join(dropped)} (not a verified data item shown to this drafter)")
        if not data_ids:
            self.note(gi, f"{key}: conclusion dropped, it cites no verified data")
            return None
        if not dc.text.strip():
            self.note(gi, f"{key}: conclusion dropped, it has no text")
            return None
        crit = [c for c in dict.fromkeys(dc.criteria) if c in group] or [group[0]]
        cid = self.ctx.next_id("C")
        c = m.Conclusion(id=cid, category=RUBRIC["criteria"][crit[0]]["category"], criteria=crit, text=dc.text.strip(),
                         data_ids=data_ids, computations=self._computations(gi, key, dc, data_ids),
                         agent_id=self.agent_ids.get(gi), verification=m.Verification(status="pending"))
        self.keys.setdefault((gi, key), cid)
        self.conclusions[cid] = c
        return c

    # -- extrapolations --------------------------------------------------------------

    def _mapped(self, gi: int, keys: list[str]) -> list[str]:
        out = []
        for k in keys:
            cid = self.keys.get((gi, k.strip()))
            if cid is None:
                self.note(gi, f"score cites unknown conclusion key {k!r}, dropped")
            elif cid not in out:
                out.append(cid)
        return out

    def _input(self, gi: int, criterion: str, rule_input: str | None, cids: list[str]) -> str | None:
        if RUBRIC["criteria"][criterion]["scoring"] != "threshold" or not rule_input:
            return None
        key, _, name = rule_input.partition(":")
        cid = self.keys.get((gi, key.strip()))
        if cid is None or name.strip() not in {c.name for c in self.conclusions[cid].computations}:
            self.note(gi, f"{criterion}: rule input {rule_input!r} names no computation, dropped")
            return None
        if cid not in cids:
            cids.append(cid)
        return f"{cid}:{name.strip()}"

    def _coverage(self, criterion: str, confidence: str, cids: list[str]) -> tuple[str, str | None]:
        """Code may lower the data-coverage label, never raise it: a 10-year criterion with fewer than ten
        fiscal years of cited data is low coverage."""
        crit = RUBRIC["criteria"][criterion]
        if not _TEN_YEARS.search(f"{crit['label']} {crit.get('evidence_hint', '')}") or confidence == "low":
            return confidence, None
        years: set[int] = set()
        for cid in cids:
            for did in self.conclusions[cid].data_ids:
                v = self.verified[did].value
                if v is not None and v.period:
                    years |= period_key(v.period)[0]
        if len(years) >= 10:
            return confidence, None
        return "low", f"{criterion}: data coverage lowered from {confidence} to low ({len(years)} of ten fiscal years)"

    def extrapolation(self, criterion: str, gi: int | None, ds: drafter.DraftScore | None, missing: str) -> m.Extrapolation:
        crit = RUBRIC["criteria"][criterion]
        rule = m.ScoreRule(type=crit["scoring"], plus_one=crit.get("plus_one"), minus_one=crit.get("minus_one"))
        base = {"id": self.ctx.next_id("E"), "category": crit["category"], "criterion": criterion, "rule": rule,
                "verification": m.Verification(status="pending"), "agent_id": self.agent_ids.get(gi)}
        cids = self._mapped(gi, ds.conclusion_keys) if (ds is not None and gi is not None) else []
        if criterion in ANALYST_ONLY:
            return m.Extrapolation(**base, score=None, na_reason=ANALYST_ONLY[criterion], text=f"{crit['label']} is not scored.",
                                   conclusion_ids=cids, confidence="low")
        if ds is None:
            return m.Extrapolation(**base, score=None, na_reason=missing, text=f"{crit['label']} is not scored.",
                                   conclusion_ids=[], confidence="low")
        rule.input = self._input(gi, criterion, ds.rule_input, cids) if gi is not None else None
        confidence, lowered = self._coverage(criterion, ds.confidence, cids)
        if lowered and gi is not None:
            self.note(gi, lowered)
        na = (ds.na_reason or "").strip() or None
        return m.Extrapolation(**base, score=ds.score, na_reason=na if ds.score is None else None,
                               text=ds.text.strip() or f"{crit['label']} is not scored.", conclusion_ids=cids,
                               confidence=confidence)

    # -- charts --------------------------------------------------------------------------

    def _resolve_ref(self, gi: int, ref: str) -> str | None:
        ref = ref.strip()
        if ":" not in ref:
            return ref if ref in self.verified and ref in self.seen.get(gi, set()) else None
        key, _, name = ref.partition(":")
        cid = self.keys.get((gi, key.strip()))
        return f"{cid}:{name.strip()}" if cid else None

    def _unit_scale(self, refs: list[str], hint_unit: str, hint_scale: str) -> tuple[str, str]:
        """The chart's unit from its first ref (a data item's own unit, or % for a percentage display). The
        scale is the drafter's display choice: code computes every point at that scale either way."""
        for ref in refs:
            base, _, name = ref.partition(":")
            if not name and base in self.verified and self.verified[base].value is not None:
                v = self.verified[base].value
                return ("%", "one") if v.unit == "%" else (v.unit, hint_scale)
            c = self.conclusions.get(base)
            comp = next((x for x in c.computations if x.name == name), None) if c else None
            if comp is not None:
                toks = scan_numbers(comp.display, include_labels=True)
                if toks and toks[0].pct:
                    return "%", "one"
        return hint_unit, hint_scale

    def chart(self, gi: int, group: list[str], dg: drafter.DraftChart) -> m.Chart | None:
        resolved = [(s.name, p.x, self._resolve_ref(gi, p.ref), p.ref) for s in dg.series for p in s.points]
        refs_ok = [r for _, _, r, _ in resolved if r]
        unit, scale = self._unit_scale(refs_ok, dg.unit.strip(), dg.scale)
        series: dict[str, list[m.ChartPoint]] = {}
        for sname, x, ref, raw in resolved:
            y = point_value(ref, unit, scale, {**self.verified, **self.conclusions}) if ref else None
            if y is None:
                self.note(gi, f"chart {dg.title!r}: point {x} ({raw}) dropped, it has no verified value in {unit}")
                continue
            series.setdefault(sname, []).append(m.ChartPoint(x=x, y=float(half_up(y, 1)), ref=ref))
        if not series:
            self.note(gi, f"chart {dg.title!r} dropped, no point could be computed")
            return None
        refs = list(dict.fromkeys(p.ref.split(":")[0] for pts in series.values() for p in pts))
        crit = dg.criterion if dg.criterion in group else group[0]
        return m.Chart(id=self.ctx.next_id("G"), category=RUBRIC["criteria"][crit]["category"], title=dg.title.strip(),
                       chart_type=dg.chart_type, unit=unit, scale=scale,
                       series=[m.ChartSeries(name=n, points=p) for n, p in series.items()], refs=refs,
                       verification=m.Verification(status="pending"))


async def _no_draft() -> None:
    """A group with no verified data gets no drafter call."""
    return None


def _dec(x: float) -> Decimal:
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def _missing_reason(group_has_data: bool, failure: str | None) -> str:
    if not group_has_data:
        return "No verified data item serves this criterion."
    if failure:
        return failure
    return "The drafter returned no score for this criterion."


def build_items(ctx: RunContext, scope: list[str], groups: list[list[str]], verified: dict[str, m.DataItem],
                results: list[tuple[drafter.DraftOutput | None, str] | None],
                shown: list[set[str]]) -> list[m.Item]:
    """Contract items from the drafts, in id order: conclusions, then one score per criterion, then charts.
    shown[gi] holds the data ids group gi's drafter saw: it may cite only those."""
    b = Builder(ctx, verified)
    b.agent_ids = {gi: r[1] for gi, r in enumerate(results) if r is not None and r[0] is not None}
    b.seen = dict(enumerate(shown))
    out: list[m.Item] = []
    for gi, (group, res) in enumerate(zip(groups, results)):
        if res is not None and res[0] is not None:
            for dc in res[0].conclusions:
                c = b.conclusion(gi, group, dc)
                if c is not None:
                    out.append(c)
    group_of = {c: gi for gi, g in enumerate(groups) for c in g}
    for crit in scope:
        gi = group_of[crit]
        res = results[gi]
        draft = res[0] if res is not None else None
        ds = next((s for s in draft.scores if s.criterion == crit), None) if draft is not None else None
        failure = res[1] if res is not None and res[0] is None else None
        out.append(b.extrapolation(crit, gi if draft is not None else None, ds, _missing_reason(res is not None, failure)))
    for gi, (group, res) in enumerate(zip(groups, results)):
        if res is not None and res[0] is not None:
            for dg in res[0].charts:
                g = b.chart(gi, group, dg)
                if g is not None:
                    out.append(g)
    for gi, lines in b.notes.items():
        aid = b.agent_ids.get(gi)
        ctx.emit("agent.log", m.AgentLogData(text="Code rejected or changed: " + "; ".join(lines),
                                             transcript_path=ctx.state.agents[aid].transcript_path if aid else None),
                 message=f"{aid or 'draft'}: code rejected or changed {len(lines)} part(s) of the draft",
                 agent_id=aid, level="warn")
    return out


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    state = ctx.state
    scope = in_scope(state.run.depth)
    groups = criterion_groups(scope, state.needs)
    data = usable_data(state)
    verified = {d.id: d for d in data}
    jobs, shown = [], []
    for group in groups:
        rows = [data_row(d) for d in data if set(d.criteria) & set(group)]
        shown.append({r["id"] for r in rows})
        jobs.append(_draft_group(ctx, group, rows) if rows else _no_draft())
    results = list(await asyncio.gather(*jobs))
    ctx.check_stop()
    for item in build_items(ctx, scope, groups, verified, results, shown):
        state.items[item.id] = item
        ctx.emit("item.created", m.ItemCreatedData(item=item), message=f"{item.id} drafted ({item.kind})",
                 agent_id=getattr(item, "agent_id", None))
