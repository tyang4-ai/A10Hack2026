"""Stage verify_summary: copy_exact, numbers_traced, citations_resolve by code, faithfulness by three judges.

Per sentence, in parallel:
  1. Code checks (verify.checks.check_sentence): citations_resolve, then copy_exact for a copied sentence
     or numbers_traced for a derived one.
  2. A derived sentence whose code checks pass goes to three isolated small-model judges
     (verifier-<H id>-1..3). Each sees only the sentence and its cited evidence. Two of three pass it.
  3. A failure is repaired by weakening, at most twice: drop refs that do not resolve, recite a copied
     sentence to its item's exact text, or a repairer rewrite toward the evidence (first repair), then
     the cited item's own text as the fallback (second repair). Still failing: the sentence is deleted
     from the brief. A blocked result (revoked source, judges that could not run) is never repaired.
Then certainty per sentence (contract/certainty.py), the word-weighted ReportCertainty, the brief's final
stats, 12-Verification/brief-checks.json in the vault, and summary.ready.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..agents import summarizer
from ..agents.base import AgentOutputError, AgentRunner
from ..contract import RUBRIC, certainty, m
from ..gateway import Blocked
from ..ids import now_iso
from ..runtime import StopRequested
from ..verify import checks
from ..verify import text as vtext
from ..verify.numbers import half_up
from .summarize import all_sentences, fit_pages, log, make_stats, words_of

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

STAGE = "verify_summary"
PASSING = ("pass", "fixed")
JUDGES = 3
MAX_REPAIRS = 2
JUDGE_MAX_TOKENS = 500
REWRITE_MAX_TOKENS = 600
FALLBACK_MAX_WORDS = 60        # an item's own text replaces a failing sentence only when it is this short
CHECKS_RELPATH = "12-Verification/brief-checks.json"
METHOD = "Word-weighted mean over sentence certainties. high_pct is the share of words in high-band sentences."


@dataclass
class Env:
    """What every sentence check reads, built once per stage."""
    state: RunState
    sources: dict[str, m.Source]
    item_cert: dict[str, dict]
    items_json: list[dict]
    holes_json: list[dict]


@dataclass
class Repair:
    action: str
    sentence: m.SummarySentence | None     # None: deleted
    note: str


@dataclass
class Outcome:
    sentence: m.SummarySentence
    deleted: bool


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------


def code_checks(state: RunState, s: m.SummarySentence, sources: dict[str, m.Source] | None = None
                ) -> list[m.CheckResult]:
    """citations_resolve, then copy_exact (copied) or numbers_traced (derived). Run after the report is final."""
    return checks.check_sentence(s, state.items, state.holes, rollup=state.rollup, rubric=RUBRIC,
                                 sources=sources if sources is not None else state.sources_by_id())


def ref_resolves(state: RunState, s: m.SummarySentence, ref: str, sources: dict[str, m.Source]) -> bool:
    """One ref on its own (with the sentence's hole refs, so a failed item cited with its hole still counts)."""
    holes = [r for r in s.derived_from if r.startswith("HOLE-") and r != ref]
    probe = m.SummarySentence(id=s.id, text="", mode="derived", derived_from=[ref] + holes,
                              verification=m.Verification(status="pending"))
    return code_checks(state, probe, sources)[0].result == "pass"


def item_certainty_map(state: RunState) -> dict[str, dict]:
    """The certainty the report shows for each item. Recomputed by contract/certainty.py when one is missing."""
    if state.items and all(i.certainty is not None for i in state.items.values()):
        return {i.id: i.certainty.model_dump(mode="json") for i in state.items.values()}
    try:
        return certainty.item_certainties([i.model_dump(mode="json") for i in state.items.values()],
                                          [s.model_dump(mode="json") for s in state.sources])
    except (KeyError, TypeError, ValueError):
        none = m.Certainty(score=None, band="none", strength=0, support=0, inherited=0, completeness=1,
                           basis="No certainty").model_dump(mode="json")
        return {i.id: (i.certainty.model_dump(mode="json") if i.certainty else none) for i in state.items.values()}


def make_env(state: RunState) -> Env:
    return Env(state=state, sources=state.sources_by_id(), item_cert=item_certainty_map(state),
               items_json=[i.model_dump(mode="json") for i in state.items.values()],
               holes_json=[h.model_dump(mode="json") for h in state.holes])


def sentence_certainty(env: Env, s: m.SummarySentence) -> m.Certainty:
    c = certainty.sentence_certainty(s.model_dump(mode="json"), env.item_cert, env.items_json, env.holes_json)
    return m.Certainty.model_validate(c)


# ---------------------------------------------------------------------------
# Judges and the repairer
# ---------------------------------------------------------------------------


def _evidence(env: Env, refs: list[str]) -> list[dict]:
    st = env.state
    return summarizer.evidence_for(refs, st.items, st.holes, st.rollup)


def _raise_fatal(answers: list) -> None:
    """A stop or a cancellation ends the stage. Other errors count as a judge that did not answer."""
    for a in answers:
        if isinstance(a, StopRequested) or (isinstance(a, BaseException) and not isinstance(a, Exception)):
            raise a


class JudgePanel:
    """Three isolated verifier agents for one sentence, kept across repair rounds (verifier-<H id>-1..3)."""

    def __init__(self, ctx: RunContext, env: Env, sid: str) -> None:
        self.ctx, self.env, self.sid = ctx, env, sid
        self.runners: list[AgentRunner] = []

    def _ensure(self) -> None:
        if not self.runners:
            self.runners = [AgentRunner(self.ctx, "verifier", f"Judge whether {self.sid} is faithful to its items",
                                        [self.sid], "small", agent_id=f"verifier-{self.sid}-{k}")
                            for k in range(1, JUDGES + 1)]

    async def vote(self, s: m.SummarySentence) -> m.CheckResult:
        self._ensure()
        user = summarizer.judge_user(s.text, _evidence(self.env, s.derived_from))
        answers = await asyncio.gather(*(r.ask(summarizer.JUDGE_SYSTEM, user, summarizer.FaithfulnessVerdict,
                                               max_tokens=JUDGE_MAX_TOKENS) for r in self.runners),
                                       return_exceptions=True)
        _raise_fatal(answers)
        verdicts = [a for a in answers if isinstance(a, summarizer.FaithfulnessVerdict)]
        errors = [a for a in answers if isinstance(a, BaseException)]
        yes = sum(v.supported for v in verdicts)
        total = len(verdicts)
        said = vtext.normalize(s.text)
        spans: list[str] = []
        for v in verdicts:
            if not v.supported:
                spans += [x for x in v.unsupported_spans
                          if x.strip() and vtext.normalize(x).strip() in said and x not in spans]
        missing = f" {len(errors)} judge(s) did not answer ({_why(errors[0])})." if errors else ""
        if total < 2:
            result, detail = "blocked", f"Only {total} of {JUDGES} judges answered, so faithfulness is unknown.{missing}"
        elif yes >= 2:
            result, detail = "pass", f"{yes} of {total} judges found it faithful to the cited items.{missing}"
        else:
            reason = next((v.reason for v in verdicts if not v.supported and v.reason), "")
            result = "fail"
            detail = f"Only {yes} of {total} judges found it faithful." + (f" {reason}" if reason else "") + missing
        return m.CheckResult(check="faithfulness", method="llm_judge", result=result, detail=detail.strip(),
                             model_id=self.ctx.settings.model_small, agent_id=f"verifier-{self.sid}",
                             unsupported_spans=spans, votes_for=yes, votes_total=total)

    def finish(self, status: str) -> None:
        for r in self.runners:
            r.finish(status)


def _why(e: BaseException) -> str:
    if isinstance(e, Blocked):
        return f"blocked: {e.reason}"
    return f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".rstrip(": ")


class Fixer:
    """The repairer for one sentence, spawned only when a rewrite is needed."""

    def __init__(self, ctx: RunContext, sid: str) -> None:
        self.ctx, self.sid = ctx, sid
        self.runner: AgentRunner | None = None

    async def rewrite(self, user: str) -> summarizer.BriefRewrite:
        if self.runner is None:
            self.runner = AgentRunner(self.ctx, "repairer", f"Weaken brief sentence {self.sid} toward its evidence",
                                      [self.sid], "small")
        return await self.runner.ask(summarizer.REWRITE_SYSTEM, user, summarizer.BriefRewrite,
                                     max_tokens=REWRITE_MAX_TOKENS)

    def finish(self, status: str) -> None:
        if self.runner is not None:
            self.runner.finish(status)


# ---------------------------------------------------------------------------
# Repairs: weaken only, never add support
# ---------------------------------------------------------------------------


def _with(s: m.SummarySentence, **update) -> m.SummarySentence:
    return s.model_copy(update=update, deep=True)


def _delete(note: str) -> Repair:
    return Repair("delete", None, note)


def recite(env: Env, s: m.SummarySentence) -> Repair:
    """A copied sentence that differs from its item: keep the item it really copies, or use the item's text."""
    items = env.state.items
    refs = [r for r in s.derived_from if r in items]
    said = vtext.normalize(s.text).strip()
    match = [r for r in refs if vtext.normalize(summarizer.item_text(items[r])).strip() == said]
    if match:
        return Repair("recite", _with(s, derived_from=[match[0]]), f"Kept {match[0]}, the one item it copies")
    if not refs:
        return _delete("A copied sentence with no item to copy")
    ref = refs[0]
    return Repair("recite", _with(s, text=summarizer.item_text(items[ref]), derived_from=[ref]),
                  f"Text replaced with the exact text of {ref}")


def fallback(env: Env, s: m.SummarySentence) -> Repair:
    """Second repair of a derived sentence: the first cited verified item's own words, or delete."""
    items = env.state.items
    for r in s.derived_from:
        it = items.get(r)
        if it is not None and it.verification.status in PASSING:
            t = summarizer.item_text(it)
            if words_of(t) <= FALLBACK_MAX_WORDS:
                return Repair("recite", _with(s, text=t, mode="copied", derived_from=[r]),
                              f"Replaced with the exact text of {r}, its own words")
    return _delete("No short verified cited item to fall back on")


async def rewrite(env: Env, s: m.SummarySentence, failed: list[m.CheckResult], fixer: Fixer) -> Repair:
    """First repair of a derived sentence: the repairer weakens it. Its refs stay as they are."""
    task = [{"check": c.check, "detail": c.detail, "unsupported_spans": list(c.unsupported_spans)} for c in failed]
    try:
        out = await fixer.rewrite(summarizer.rewrite_user(s.text, task, _evidence(env, s.derived_from)))
    except (Blocked, AgentOutputError) as e:
        return fallback(env, s) if isinstance(e, AgentOutputError) else _delete(f"Repair blocked: {e.reason}")
    new = " ".join(out.text.split())
    if out.action == "delete" or not new:
        return _delete("The repairer found nothing supported to keep")
    action = "recompute" if {c.check for c in failed} == {"numbers_traced"} else "downgrade"
    return Repair(action, _with(s, text=new), "Weakened toward the cited items")


async def repair(env: Env, s: m.SummarySentence, res: list[m.CheckResult], rnd: int, fixer: Fixer) -> Repair:
    failed = [c for c in res if c.result == "fail"]
    names = {c.check for c in failed}
    if "citations_resolve" in names:
        good = [r for r in s.derived_from if ref_resolves(env.state, s, r, env.sources)]
        bad = [r for r in s.derived_from if r not in good]
        if not good:
            return _delete("None of its citations resolve")
        trimmed = _with(s, derived_from=good)
        if s.mode == "copied":
            return recite(env, trimmed)
        return Repair("downgrade", trimmed, f"Dropped citations that do not resolve: {', '.join(bad)}")
    if "copy_exact" in names:
        return recite(env, s)
    if names & {"numbers_traced", "faithfulness"} and s.mode == "derived":
        return await rewrite(env, s, failed, fixer) if rnd == 1 else fallback(env, s)
    return _delete("No repair applies")


# ---------------------------------------------------------------------------
# One sentence
# ---------------------------------------------------------------------------


async def check_round(env: Env, s: m.SummarySentence, panel: JudgePanel) -> list[m.CheckResult]:
    res = code_checks(env.state, s, env.sources)
    if s.mode == "derived" and checks.status_from(res) == "pass":
        res.append(await panel.vote(s))
    return res


def _first_fail(res: list[m.CheckResult]) -> str:
    c = next((c for c in res if c.result == "fail"), None)
    return f"{c.check}: {c.detail}" if c else "failed"


async def verify_sentence(ctx: RunContext, env: Env, s: m.SummarySentence) -> Outcome:
    """Check, repair at most twice, check again. Emits summary.verified for each failure and once at the end."""
    panel, fixer = JudgePanel(ctx, env, s.id), Fixer(ctx, s.id)
    attempts: list[m.Attempt] = []
    deleted = False
    try:
        for rnd in range(1, MAX_REPAIRS + 2):
            ctx.check_stop()
            res = await check_round(env, s, panel)
            status = checks.status_from(res)
            if status != "fail":
                break
            ctx.emit("summary.verified", m.SummaryVerifiedData(
                sentence_id=s.id, verification=m.Verification(status="fail", checks=res, attempts=list(attempts))),
                message=f"{s.id} failed: {_first_fail(res)}", stage=STAGE, level="warn")
            if rnd > MAX_REPAIRS:
                rep = _delete(f"Still failing after {MAX_REPAIRS} repairs")
            else:
                rep = await repair(env, s, res, rnd, fixer)
            attempts.append(m.Attempt(attempt=rnd, status="fail", checks=res, action=rep.action, before=s.text,
                                      after=rep.sentence.text if rep.sentence else None,
                                      note=rep.note + (". Removed from the brief." if rep.sentence is None else "."),
                                      at=now_iso()))
            if rep.sentence is None:
                deleted = True
                break
            s = rep.sentence
    except BaseException as e:
        how = "stopped" if isinstance(e, StopRequested) else "failed"
        panel.finish(how)
        fixer.finish(how)
        raise
    panel.finish("done")
    fixer.finish("done")
    final = "fail" if deleted else "fixed" if status == "pass" and attempts else status
    if final == "fixed":
        attempts.append(m.Attempt(attempt=len(attempts) + 1, status="fixed", checks=res, note="All checks pass.",
                                  at=now_iso()))
    s = _with(s, verification=m.Verification(status=final, checks=res, attempts=attempts, verified_at=now_iso()))
    s.certainty = sentence_certainty(env, s)
    tail = " (removed from the brief)" if deleted else f", certainty {s.certainty.score}" if s.certainty.score is not None else ""
    ctx.emit("summary.verified", m.SummaryVerifiedData(sentence_id=s.id, verification=s.verification,
                                                       certainty=s.certainty),
             message=f"{s.id} {final}{tail}", stage=STAGE, level="info" if final in PASSING else "warn")
    return Outcome(s, deleted)


# ---------------------------------------------------------------------------
# The brief as a whole
# ---------------------------------------------------------------------------


def replace_sentences(sections: list[m.SummarySection], final: dict[str, Outcome]) -> list[m.SummarySection]:
    """The sections with every sentence swapped for its checked version. Deleted sentences go, and a chart
    whose caption was deleted goes with it."""
    def keep(s: m.SummarySentence) -> m.SummarySentence | None:
        o = final.get(s.id)
        return None if o is None or o.deleted else o.sentence

    out = []
    for sec in sections:
        blocks = []
        for b in sec.blocks:
            if isinstance(b, (m.ParagraphBlock, m.BulletsBlock)):
                sents = [x for x in (keep(s) for s in b.sentences) if x is not None]
                if sents:
                    blocks.append(b.model_copy(update={"sentences": sents}))
            elif isinstance(b, m.ChartBlock):
                cap = keep(b.caption)
                if cap is not None:
                    blocks.append(b.model_copy(update={"caption": cap}))
            else:
                blocks.append(b)
        if blocks:
            out.append(sec.model_copy(update={"blocks": blocks}))
    return out


def coverage(state: RunState) -> tuple[int, int]:
    """(scored, in_scope) BOFFIN criteria: from the rollup, else counted from the E items."""
    if state.rollup is not None:
        return state.rollup.scored, state.rollup.in_scope
    scope = RUBRIC["depths"].get(state.run.depth)
    n_scope = len(RUBRIC["criteria"]) if scope == "all" or scope is None else len(scope)
    scored = sum(e.score is not None and e.verification.status in PASSING for e in state.extrapolations())
    return scored, n_scope


def brief_certainty(state: RunState, sections: list[m.SummarySection]) -> m.ReportCertainty:
    entries = [(s.certainty.model_dump(mode="json"), words_of(s.text)) for _, s in all_sentences(sections)
               if s.certainty is not None]
    scored, in_scope = coverage(state)
    return m.ReportCertainty.model_validate(certainty.report_certainty(entries, scored, in_scope, METHOD))


def checks_record(outcomes: list[Outcome], kept: set[str]) -> dict:
    """12-Verification/brief-checks.json: every sentence's final verification, removed and cut ones included."""
    out = {}
    for o in outcomes:
        s = o.sentence
        out[s.id] = {"text": s.text, "mode": s.mode, "derived_from": s.derived_from,
                     "in_brief": s.id in kept, "removed_by_repair": o.deleted,
                     "cut_for_length": not o.deleted and s.id not in kept,
                     "verification": s.verification.model_dump(mode="json"),
                     "certainty": s.certainty.model_dump(mode="json") if s.certainty else None}
    return {"sentences": out}


async def _gather_all(coros: list) -> list:
    """Run every sentence. A stop wins over other errors, and every task finishes before we raise."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    _raise_fatal(results)
    err = next((r for r in results if isinstance(r, BaseException)), None)
    if err is not None:
        raise err
    return results


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    st = ctx.state
    brief = st.summary
    if brief is None:
        raise RuntimeError("verify_summary needs the brief from the summarize stage")
    st.summary = brief = brief.model_copy(update={"status": "verifying"})
    env = make_env(st)
    outcomes: list[Outcome] = await _gather_all(
        [verify_sentence(ctx, env, s) for _, s in all_sentences(brief.sections)])
    sections, cut = fit_pages(replace_sentences(brief.sections, {o.sentence.id: o for o in outcomes}))
    if cut:
        log_id = next((a.agent_id for a in st.agents.values() if a.role == "summarizer"), None)
        if log_id:
            log(ctx, log_id, f"Cut {', '.join(s.id for s in cut)} to fit three estimated pages", level="warn")
    kept = {s.id for _, s in all_sentences(sections)}
    final = brief.model_copy(update={
        "sections": sections, "holes": list(st.holes), "status": "final",
        "stats": make_stats(sections, failed=sum(o.deleted for o in outcomes)),
        "certainty": brief_certainty(st, sections), "as_of_seq": st.run.last_seq})
    st.summary = m.HumanReport.model_validate(final.model_dump(mode="json"))
    try:
        await ctx.vault.aput_text(st.run.run_id, CHECKS_RELPATH,
                                  json.dumps(checks_record(outcomes, kept), indent=2, ensure_ascii=False))
    except Exception as e:  # the vault copy is a record. The brief in state is what the API serves
        ctx.emit("agent.log", m.AgentLogData(text=f"{CHECKS_RELPATH} not saved: {type(e).__name__}: {e}"),
                 message=f"{CHECKS_RELPATH} not saved", stage=STAGE, level="warn")
    stats = st.summary.stats
    ctx.emit("summary.ready", m.SummaryReadyData(sentences=stats.sentences, words=stats.words),
             message=(f"Brief ready: {stats.sentences} sentences, {stats.words} words, about {stats.est_pages:g} pages"
                      f" ({stats.fixed} fixed, {stats.failed} removed)"), stage=STAGE)


# ---------------------------------------------------------------------------
# Markdown (for 09-Analysis/human-report.md, written by the complete stage)
# ---------------------------------------------------------------------------


def _signed(x: float | None, places: int = 2) -> str:
    if x is None:
        return "n/a"
    s = half_up(x, places)
    return f"+{s}" if x > 0 else s


def _md_sentence(s: m.SummarySentence) -> str:
    score = s.certainty.score if s.certainty and s.certainty.score is not None else "none"
    return f"{s.text} [{s.id}: {', '.join(s.derived_from)}; certainty {score}]"


def _md_scorecard(rollup: m.Rollup | None, items: dict[str, m.Item] | None) -> list[str]:
    if rollup is None:
        return ["(No rollup: the scorecard is not available.)", ""]
    labels = {c["id"]: c["label"] for c in RUBRIC["categories"]}
    lines = ["| Category | Weight | Score | Scored | N/A |", "|---|---|---|---|---|"]
    for c in rollup.categories:
        na = ", ".join(summarizer.criterion_label(k) or k for k in c.na_criteria) or "none"
        lines.append(f"| {labels.get(c.category, c.category)} | {c.weight} | {_signed(c.score, 1)} | "
                     f"{c.scored} of {c.in_scope} | {na} |")
    lines += ["", f"Weighted average {_signed(rollup.weighted_average)}, {rollup.scored} of {rollup.in_scope} "
                  f"criteria scored. Normalized {rollup.normalized_pct}%, percent of max {rollup.percent_of_max_pct}% "
                  "(both are inferences about the original scoresheet).", ""]
    if items:
        es = [i for i in items.values() if isinstance(i, m.Extrapolation)]
        if es:
            lines += ["| Criterion | Score | Confidence | Certainty |", "|---|---|---|---|"]
            for e in es:
                score = _signed(e.score, 0) if e.score is not None else "N/A"
                cert = e.certainty.score if e.certainty and e.certainty.score is not None else "none"
                lines.append(f"| {summarizer.criterion_label(e.criterion)} | {score} | {e.confidence} | {cert} |")
            lines.append("")
    return lines


def render_markdown(brief: m.HumanReport, *, rollup: m.Rollup | None = None,
                    items: dict[str, m.Item] | None = None) -> str:
    """The brief as Markdown. Each sentence ends with its id, the report ids it cites and its certainty."""
    lines = [f"# {brief.title}", "", f"As of {brief.as_of}. Every sentence cites the agent report.", ""]
    for sec in brief.sections:
        lines += [f"## {sec.heading}", ""]
        for b in sec.blocks:
            if isinstance(b, m.ParagraphBlock):
                lines += [" ".join(_md_sentence(s) for s in b.sentences), ""]
            elif isinstance(b, m.BulletsBlock):
                lines += [f"- {_md_sentence(s)}" for s in b.sentences] + [""]
            elif isinstance(b, m.ChartBlock):
                lines += [f"Chart {b.chart_id}: {_md_sentence(b.caption)}", ""]
            else:
                lines += _md_scorecard(rollup, items)
    if brief.holes:
        lines += ["## Holes", ""] + [f"- {h.hole_id}: {h.reason}" for h in brief.holes] + [""]
    st, c = brief.stats, brief.certainty
    lines.append(f"{st.sentences} sentences, {st.words} words, about {st.est_pages:g} pages. "
                 f"{st.passed} passed, {st.fixed} fixed, {st.failed} removed.")
    if c is not None:
        lines.append(f"Certainty: mean {c.mean if c.mean is not None else 'none'}, {c.high_pct}% of words high. "
                     f"Coverage {c.coverage_scored} of {c.coverage_in_scope} criteria.")
    lines += ["", brief.disclaimer, ""]
    return "\n".join(lines)


__all__ = ["run", "verify_sentence", "code_checks", "render_markdown"]
