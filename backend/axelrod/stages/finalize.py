"""Stage complete, plus finalize_report: certainty for every item, the rollup, the holes register, report.ready,
and the final reports and check files written to the vault.

Everything here is code. Certainty comes from contract/certainty.py (never from a model), the rollup follows
the rubric's own formulas with round half up, and every gap is declared as a Hole: unscored criteria,
items that failed or stayed blocked, and approved sources that could not be used.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

from ..contract import RUBRIC, certainty, m
from ..ids import now_iso
from ..verify.numbers import half_up

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

PASSING = ("pass", "fixed")
BAD_SOURCE = ("failed", "blocked", "revoked", "quarantined", "skipped")
KIND_ORDER = {"data": 0, "conclusion": 1, "chart": 2, "extrapolation": 3}
KIND_WORD = {"data": "Data item", "conclusion": "Conclusion", "extrapolation": "Score", "chart": "Chart"}
ROLLUP_NOTE = "Lead with the weighted average. Both percentages are inferences about the original scoresheet."
REPORT_METHOD = "Plain mean over item certainties. high_pct is the share of items in the high band."

AGENT_MD = "09-Analysis/agent-report.md"
AGENT_JSON = "09-Analysis/agent-report.json"
HUMAN_MD = "09-Analysis/human-report.md"
HUMAN_JSON = "09-Analysis/human-report.json"
DATA_CHECKS = "12-Verification/data-checks.json"
REASONING_CHECKS = "12-Verification/reasoning-checks.json"

_CRIT = RUBRIC["criteria"]
_CAT_LABEL = {c["id"]: c["label"] for c in RUBRIC["categories"]}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def in_scope(depth: str, rubric: dict = RUBRIC) -> list[str]:
    """In-scope criteria in rubric order ("all" lists every criterion category by category)."""
    d = rubric["depths"][depth]
    if d == "all":
        return [k for c in rubric["categories"] for k in c["criteria"]]
    return list(d)


def label(criterion: str) -> str:
    return _CRIT.get(criterion, {}).get("label", criterion)


# ---------------------------------------------------------------------------
# Certainty (contract/certainty.py over contract-shaped dicts)
# ---------------------------------------------------------------------------


class CertaintyBook:
    """Certainty for items, computed by contract/certainty.py. Data items are dumped once per book, since the
    reasoning stages never change them. Make a new book when data items may have changed."""

    def __init__(self, state: RunState) -> None:
        self.state = state
        self._data: dict[str, dict] = {}

    def _dicts(self) -> list[dict]:
        out = []
        for it in self.state.items.values():
            if it.kind == "data":
                if it.id not in self._data:
                    self._data[it.id] = it.model_dump(mode="json")
                out.append(self._data[it.id])
            else:
                out.append(it.model_dump(mode="json"))
        return out

    def all(self) -> dict[str, dict]:
        sources = [s.model_dump(mode="json") for s in self.state.sources]
        return certainty.item_certainties(self._dicts(), sources)

    def of(self, item_id: str) -> m.Certainty | None:
        try:
            raw = self.all().get(item_id)
        except (KeyError, ZeroDivisionError):
            return None
        return m.Certainty.model_validate(raw) if raw is not None else None


def emit_verified(ctx: RunContext, item: m.Item, book: CertaintyBook, *, message: str | None = None) -> None:
    """Set the item's certainty from its verification and emit item.verified."""
    item.certainty = book.of(item.id)
    v = item.verification
    c = item.certainty
    note = f", certainty {c.score} ({c.band})" if c is not None and c.score is not None else ""
    ctx.emit("item.verified", m.ItemVerifiedData(item_id=item.id, verification=v, certainty=item.certainty),
             message=message or f"{item.id} {v.status}{note}", level="info" if v.status in PASSING else "warn")


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def _dec(x: float | int) -> Decimal:
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def scored_criteria(extrapolations: Iterable[m.Extrapolation], scope: list[str]) -> dict[str, int]:
    """criterion -> score, from passing extrapolations with a score (the first one per criterion)."""
    out: dict[str, int] = {}
    for e in extrapolations:
        if (e.criterion in scope and e.criterion not in out and e.verification.status in PASSING
                and e.score is not None):
            out[e.criterion] = e.score
    return out


def build_rollup(extrapolations: Iterable[m.Extrapolation], scope: list[str], rubric: dict = RUBRIC) -> m.Rollup:
    """The rubric's rollup: category means (1 decimal), the weighted average of the scored categories
    (2 decimals) and both percentages, all rounded half up. Failed or N/A scores count as not scored."""
    scores = scored_criteria(extrapolations, scope)
    cats: list[m.CategoryScore] = []
    for c in rubric["categories"]:
        crit = [k for k in c["criteria"] if k in scope]
        if not crit:
            continue
        vals = [scores[k] for k in crit if k in scores]
        mean = float(half_up(Decimal(sum(vals)) / len(vals), 1)) if vals else None
        cats.append(m.CategoryScore(category=c["id"], weight=c["weight"], score=mean, scored=len(vals),
                                    in_scope=len(crit), na_criteria=[k for k in crit if k not in scores]))
    scored_cats = [c for c in cats if c.score is not None]
    wavg = norm = pmax = None
    if scored_cats:
        total = sum(Decimal(c.weight) * _dec(c.score) for c in scored_cats)
        avg = Decimal(half_up(total / sum(c.weight for c in scored_cats), 2))
        wavg = float(avg)
        norm = int(half_up((avg + 1) / 2 * 100, 0))
        pmax = int(half_up(max(avg, Decimal(0)) * 100, 0))
    na = [k for k in scope if k not in scores]
    return m.Rollup(categories=cats, weighted_average=wavg, normalized_pct=norm, percent_of_max_pct=pmax,
                    scored=len(scope) - len(na), in_scope=len(scope),
                    framework_total=rubric.get("framework_total", 27), na_criteria=na, note=ROLLUP_NOTE)


# ---------------------------------------------------------------------------
# Holes
# ---------------------------------------------------------------------------


def _first_problem(v: m.Verification) -> str:
    for want in ("fail", "blocked"):
        for c in v.checks:
            if c.result == want:
                return f"{c.check}: {c.detail}"
    return "no checks ran" if not v.checks else "checks did not pass"


def _short(s: str, n: int = 220) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _hole_key(h: m.Hole) -> tuple[str, str]:
    if h.criterion:
        return ("crit", h.criterion)
    if h.item_id:
        return ("item", h.item_id)
    mm = re.match(r"(extractor-\d+) failed", h.reason)
    if mm:
        return ("agent", mm.group(1))
    mm = re.match(r"(S-\d+)", h.reason)
    return ("src", mm.group(1) if mm else h.reason)


def _criterion_reason(e: m.Extrapolation | None) -> str:
    if e is None:
        return "No score was drafted for this criterion."
    v = e.verification
    if v.status == "fail":
        return _short(f"The score failed verification after repair ({_first_problem(v)}). Not scored.")
    if v.status == "blocked":
        return _short(f"The score could not be verified ({_first_problem(v)}). Not scored.")
    if v.status == "pending":
        return "The score was never verified. Not scored."
    return _short(e.na_reason or "Not scored.")


def _item_reason(it: m.Item) -> str:
    v = it.verification
    word = KIND_WORD[it.kind]
    if v.status == "fail":
        return _short(f"{word} failed verification ({_first_problem(v)}). Excluded from scoring.")
    if v.status == "blocked":
        return _short(f"{word} could not be verified ({_first_problem(v)}). Excluded from scoring.")
    return f"{word} was never verified. Excluded from scoring."


def _source_reason(s: m.Source) -> str:
    why = {"failed": "could not be fetched", "blocked": "was blocked by the gateway",
           "revoked": "was revoked by the analyst", "quarantined": "was quarantined after the prompt-attack scan",
           "skipped": "was skipped"}[s.status]
    return _short(f"{s.source_id} ({s.title}) {why}" + (f": {s.error}" if s.error else "."))


def _last_warning(state: RunState, agent_id: str) -> str | None:
    for e in reversed(state.events):
        if e.type == "agent.log" and e.agent_id == agent_id and e.level in ("warn", "error"):
            return e.data.text
    return None


def _agent_reason(state: RunState, a: m.Agent) -> str:
    why = _last_warning(state, a.agent_id)
    return _short(f"{a.agent_id} failed: {a.task}, so it gave no data items" + (f" ({why})." if why else "."))


def build_holes(ctx: RunContext, scope: list[str]) -> list[m.Hole]:
    """Every gap: unscored in-scope criteria, items left failed, blocked or pending, approved sources that
    failed, were blocked, revoked, quarantined or skipped, and extractor agents that failed (their pages
    gave no data). Ids stay stable across calls."""
    state = ctx.state
    existing = {_hole_key(h): h.hole_id for h in state.holes}
    wanted: list[tuple[tuple[str, str], str | None, str | None, str]] = []   # key, criterion, item_id, reason
    by_crit: dict[str, m.Extrapolation] = {}
    for e in state.extrapolations():
        cur = by_crit.get(e.criterion)
        if cur is None or (cur.verification.status not in PASSING and e.verification.status in PASSING):
            by_crit[e.criterion] = e
    for crit in scope:
        e = by_crit.get(crit)
        if e is not None and e.verification.status in PASSING and e.score is not None:
            continue
        wanted.append((("crit", crit), crit, e.id if e else None, _criterion_reason(e)))
    covered = {w[2] for w in wanted if w[2]}
    for it in sorted(state.items.values(), key=lambda i: (KIND_ORDER[i.kind], i.id)):
        if it.verification.status in PASSING or it.id in covered:
            continue
        wanted.append((("item", it.id), None, it.id, _item_reason(it)))
    for s in state.sorted_sources():
        if s.approval in ("approved", "auto_approved") and s.status in BAD_SOURCE:
            wanted.append((("src", s.source_id), None, None, _source_reason(s)))
    for a in state.agents.values():
        if a.role == "extractor" and a.status == "failed":
            wanted.append((("agent", a.agent_id), None, None, _agent_reason(state, a)))
    out = []
    for key, crit, item_id, reason in wanted:
        hid = existing.get(key) or ctx.next_id("HOLE")
        out.append(m.Hole(hole_id=hid, criterion=crit, item_id=item_id, reason=reason))
    return out


# ---------------------------------------------------------------------------
# Sections and counts
# ---------------------------------------------------------------------------


def build_sections(state: RunState) -> list[m.ReportSection]:
    """One section per category in rubric order: data, then conclusions, charts and scores."""
    groups: dict[str, list[m.Item]] = {}
    for it in state.items.values():
        groups.setdefault(it.category, []).append(it)
    out = []
    for c in RUBRIC["categories"]:
        items = groups.get(c["id"])
        if items:
            ids = [i.id for i in sorted(items, key=lambda i: (KIND_ORDER[i.kind], i.id))]
            out.append(m.ReportSection(category=c["id"], title=c["label"], item_ids=ids))
    return out


def report_counts(state: RunState, holes: int) -> m.ReportReadyData:
    statuses = [i.verification.status for i in state.items.values()]
    passed, fixed = statuses.count("pass"), statuses.count("fixed")
    return m.ReportReadyData(items=len(statuses), passed=passed, fixed=fixed, failed=len(statuses) - passed - fixed,
                             holes=holes)


# ---------------------------------------------------------------------------
# finalize_report
# ---------------------------------------------------------------------------


def finalize_report(ctx: RunContext) -> None:
    """Certainty for every item, the rollup from the rubric, holes, ReportCertainty, then report.ready."""
    state = ctx.state
    scope = in_scope(state.run.depth)
    book = CertaintyBook(state)
    certs = book.all()
    for iid, it in state.items.items():
        new = m.Certainty.model_validate(certs[iid]) if iid in certs else None
        if new != it.certainty:
            it.certainty = new
            ctx.emit("item.verified", m.ItemVerifiedData(item_id=iid, verification=it.verification, certainty=new),
                     message=f"{iid} certainty {new.score if new else None} at finalize")
    rollup = build_rollup(state.extrapolations(), scope)
    holes = build_holes(ctx, scope)
    rc = certainty.report_certainty([(certs[i], 1) for i in state.items if i in certs], rollup.scored,
                                    rollup.in_scope, REPORT_METHOD)
    state.rollup = rollup
    state.holes = holes
    state.report_certainty = m.ReportCertainty.model_validate(rc)
    state.sections = build_sections(state)
    state.report_generated_at = now_iso()
    state.report_markdown_path = _put(ctx, AGENT_MD, agent_report_markdown(state))
    counts = report_counts(state, len(holes))
    wavg = "n/a" if rollup.weighted_average is None else _signed(rollup.weighted_average, 2)
    ctx.emit("report.ready", counts, stage="repair",
             message=(f"Agent report final: {counts.items} items, {counts.fixed} fixed, {counts.failed} failed, "
                      f"{counts.holes} holes, weighted average {wavg}, {rollup.scored} of {rollup.in_scope} scored"))
    _put(ctx, AGENT_JSON, state.report().model_dump_json(indent=2))


def _put(ctx: RunContext, relpath: str, text: str) -> str | None:
    """Sync vault write. A vault failure never stops the report: it is logged and the path stays None."""
    try:
        return ctx.vault.put_text(ctx.run_id, relpath, text)
    except Exception as e:  # noqa: BLE001 - the report must still go final
        ctx.emit("agent.log", m.AgentLogData(text=f"Vault write failed for {relpath}: {type(e).__name__}: {e}"),
                 message=f"Vault write failed for {relpath}", level="warn")
        return None


# ---------------------------------------------------------------------------
# Stage complete: the vault files
# ---------------------------------------------------------------------------


def _checks_entry(iid: str, kind: str, v: m.Verification, c: m.Certainty | None) -> dict:
    return {"id": iid, "kind": kind, "status": v.status, "verified_at": v.verified_at,
            "checks": [x.model_dump(mode="json") for x in v.checks],
            "attempts": [a.model_dump(mode="json") for a in v.attempts],
            "certainty": c.model_dump(mode="json") if c else None}


def _sentences(h: m.HumanReport) -> list[m.SummarySentence]:
    out = []
    for sec in h.sections:
        for b in sec.blocks:
            if b.type in ("paragraph", "bullets"):
                out.extend(b.sentences)
            elif b.type == "chart":
                out.append(b.caption)
    return out


def check_files(state: RunState) -> tuple[str, str]:
    """12-Verification/data-checks.json and reasoning-checks.json."""
    at = now_iso()
    data = [_checks_entry(i.id, i.kind, i.verification, i.certainty) for i in state.data_items()]
    reasoning = [_checks_entry(i.id, i.kind, i.verification, i.certainty)
                 for i in sorted(state.items.values(), key=lambda i: (KIND_ORDER[i.kind], i.id)) if i.kind != "data"]
    sentences = [_checks_entry(s.id, "sentence", s.verification, s.certainty)
                 for s in (_sentences(state.summary) if state.summary else [])]
    d = {"run_id": state.run.run_id, "written_at": at, "items": data}
    r = {"run_id": state.run.run_id, "written_at": at, "items": reasoning, "sentences": sentences}
    return json.dumps(d, ensure_ascii=False, indent=2), json.dumps(r, ensure_ascii=False, indent=2)


async def run(ctx: RunContext) -> None:
    """Write 09-Analysis/agent-report.json/.md, human-report.json/.md and 12-Verification/*.json to the vault.

    The brief's Markdown comes from verify_summary.render_markdown (B-brief). A run without a brief (it
    stopped early) gets no human-report files. B-brief writes 12-Verification/brief-checks.json itself."""
    state = ctx.state
    if not state.report_ready:
        finalize_report(ctx)
    files = {AGENT_MD: agent_report_markdown(state)}
    files[DATA_CHECKS], files[REASONING_CHECKS] = check_files(state)
    for rel, text in files.items():
        path = await ctx.vault.aput_text(ctx.run_id, rel, text)
        if rel == AGENT_MD:
            state.report_markdown_path = path
    await ctx.vault.aput_text(ctx.run_id, AGENT_JSON, state.report().model_dump_json(indent=2))
    if state.summary is None:
        return
    from .verify_summary import render_markdown

    md = render_markdown(state.summary, rollup=state.rollup, items=state.items)
    state.summary.markdown_path = await ctx.vault.aput_text(ctx.run_id, HUMAN_MD, md)
    await ctx.vault.aput_text(ctx.run_id, HUMAN_JSON, state.human_report().model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _signed(x: float | int, places: int) -> str:
    s = half_up(_dec(x), places)
    return f"+{s}" if Decimal(s) > 0 else s


def _cert(c: m.Certainty | None) -> str:
    if c is None or c.score is None:
        return "certainty none"
    return f"certainty {c.score} ({c.band})"


def _value(v: m.Value | None) -> str:
    if v is None:
        return ""
    scale = "" if v.scale == "one" else f" {v.scale}"
    unit = "" if v.unit == "%" else f" {v.unit}"
    period = f", {v.period}" if v.period else ""
    return f" Value {v.as_reported}{unit}{scale}{period}."


def _cite(c: m.Citation) -> str:
    loc = c.locator
    where = f"p. {loc.page}" if isinstance(loc, m.TextLocator) else f"`{loc.pointer}`"
    return f"{c.doc_id} {where}"


def _item_md(it: m.Item) -> str:
    st = f"_{it.verification.status}, {_cert(it.certainty)}_"
    if it.kind == "data":
        cites = "; ".join(_cite(c) for c in it.citations if c.role == "evidence")
        return f"- **{it.id}** {it.statement}{_value(it.value)} {it.source_id} ({it.source_tier}), {cites}. {st}"
    if it.kind == "conclusion":
        comps = "".join(f"\n  - `{c.name} = {c.expr}` gives {c.display}" for c in it.computations)
        return f"- **{it.id}** {it.text} Cites {', '.join(it.data_ids)}. {st}{comps}"
    if it.kind == "extrapolation":
        score = "N/A" if it.score is None else _signed(it.score, 0)
        why = f" N/A because: {it.na_reason}" if it.score is None and it.na_reason else ""
        cites = f" Cites {', '.join(it.conclusion_ids)}." if it.conclusion_ids else ""
        return (f"- **{it.id}** {label(it.criterion)}: **{score}** (data coverage {it.confidence}). "
                f"{it.text}{why}{cites} {st}")
    pts = "; ".join(f"{s.name} {p.x} {p.y} ({p.ref})" for s in it.series for p in s.points)
    unit = it.unit if it.scale == "one" else f"{it.unit} {it.scale}"
    return f"- **{it.id}** {it.title} ({it.chart_type}, {unit}): {pts}. {st}"


def rollup_markdown(r: m.Rollup | None) -> list[str]:
    if r is None:
        return ["No rollup yet."]
    wavg = "n/a" if r.weighted_average is None else _signed(r.weighted_average, 2)
    lines = [f"Weighted average: **{wavg}** (score under the BOFFIN framework, -1 to +1).",
             f"Coverage: {r.scored} of {r.in_scope} in-scope criteria scored."
             + (f" Not scored: {', '.join(label(k) for k in r.na_criteria)}." if r.na_criteria else "")]
    if r.normalized_pct is not None:
        lines.append(f"Inferences about the original scoresheet: normalized {r.normalized_pct}%, "
                     f"percent of max {r.percent_of_max_pct}%.")
    lines += ["", "| Category | Weight | Score | Scored |", "|---|---|---|---|"]
    for c in r.categories:
        score = "n/a" if c.score is None else _signed(c.score, 1)
        lines.append(f"| {_CAT_LABEL.get(c.category, c.category)} | {c.weight} | {score} | {c.scored} / {c.in_scope} |")
    return lines


def _holes_md(holes: list[m.Hole]) -> list[str]:
    if not holes:
        return ["No holes declared."]
    out = []
    for h in holes:
        what = ", ".join(x for x in (label(h.criterion) if h.criterion else None, h.item_id) if x)
        out.append(f"- **{h.hole_id}**" + (f" ({what})" if what else "") + f": {h.reason}")
    return out


def agent_report_markdown(state: RunState) -> str:
    run = state.run
    name = f"{run.company} ({run.ticker})" if run.ticker else run.company
    lines = [f"# {name}: agent report", "",
             f"Depth {run.depth}. As of {state.as_of or run.created_at[:10]}. Generated {state.report_generated_at}. "
             f"Run {run.run_id} ({run.mode})."]
    if state.fiscal_note:
        lines += ["", state.fiscal_note]
    lines += ["", f"> {state.disclaimer}", "", "## Scorecard", ""] + rollup_markdown(state.rollup)
    rc = state.report_certainty
    if rc is not None:
        mean = "n/a" if rc.mean is None else str(rc.mean)
        lines += ["", "## Certainty", "",
                  f"Mean {mean} over {rc.high + rc.medium + rc.low + rc.none} items, {rc.high_pct}% in the high band "
                  f"(high {rc.high}, medium {rc.medium}, low {rc.low}, none {rc.none}). "
                  f"Coverage {rc.coverage_scored} of {rc.coverage_in_scope} criteria, shown apart from certainty.",
                  "Certainty is how well each item is supported by checked evidence, not the probability it is true."]
    for sec in state.sections or build_sections(state):
        lines += ["", f"## {sec.title}", ""]
        lines += [_item_md(state.items[i]) for i in sec.item_ids if i in state.items]
    lines += ["", "## Holes", ""] + _holes_md(state.holes)
    return "\n".join(lines) + "\n"
