"""Stage summarize: the summarizer writes the 3-page brief, code assigns H ids and streams summary.sentence.

The summarizer (large model) sees only verified items (pass or fixed), the rollup and the holes. Code then
drops what it cannot check (a sentence that cites nothing, a chart block without a verified chart, a second
scorecard), adds the scorecard when the model left it out, trims the brief to three estimated pages, and
only then assigns H- ids and streams the sentences. Every check runs in verify_summary.

Page estimate (stats.est_pages, an estimate: check it in print preview): words / 450, plus 0.3 page per
chart, 0.35 for the scorecard, 0.04 per section heading and 0.15 for the title, as-of line and disclaimer.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..agents import summarizer
from ..agents.base import AgentRunner
from ..contract import m
from ..verify.numbers import half_up

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

PASSING = ("pass", "fixed")
MAX_PAGES = 3.0
WORDS_PER_PAGE = 450
CHART_PAGES = 0.3
SCORECARD_PAGES = 0.35
SECTION_PAGES = 0.04
FRAME_PAGES = 0.15
MAX_CHARTS = 2
PLANNED_SECTIONS = 6          # bottom line, about three categories, scorecard, holes
SUMMARIZER_MAX_TOKENS = 8000  # about 700 words of sentences plus the JSON around them


# ---------------------------------------------------------------------------
# Walking and measuring a brief (verify_summary uses these too)
# ---------------------------------------------------------------------------


def block_sentences(b) -> list[m.SummarySentence]:
    if isinstance(b, (m.ParagraphBlock, m.BulletsBlock)):
        return list(b.sentences)
    if isinstance(b, m.ChartBlock):
        return [b.caption]
    return []


def all_sentences(sections: list[m.SummarySection]) -> list[tuple[str, m.SummarySentence]]:
    """(section heading, sentence) in reading order."""
    return [(sec.heading, s) for sec in sections for b in sec.blocks for s in block_sentences(b)]


def words_of(text: str) -> int:
    return len(text.split())


def estimate_pages(sections: list[m.SummarySection]) -> float:
    """Printed pages, rounded half up to one decimal."""
    words = sum(words_of(s.text) for _, s in all_sentences(sections))
    blocks = [b for sec in sections for b in sec.blocks]
    charts = sum(isinstance(b, m.ChartBlock) for b in blocks)
    cards = sum(isinstance(b, m.ScorecardBlock) for b in blocks)
    x = (words / WORDS_PER_PAGE + charts * CHART_PAGES + cards * SCORECARD_PAGES
         + len(sections) * SECTION_PAGES + FRAME_PAGES)
    return float(half_up(x, 1))


def word_budget(n_charts: int) -> int:
    """Words the summarizer may write so the planned brief fits, with a 10% margin."""
    overhead = (min(n_charts, MAX_CHARTS) * CHART_PAGES + SCORECARD_PAGES + PLANNED_SECTIONS * SECTION_PAGES
                + FRAME_PAGES)
    return max(150, int((MAX_PAGES - overhead) * WORDS_PER_PAGE * 0.9))


def make_stats(sections: list[m.SummarySection], *, failed: int = 0) -> m.SummaryStats:
    """failed counts sentences that failed every repair and were removed, so they are not in sections."""
    sents = [s for _, s in all_sentences(sections)]
    return m.SummaryStats(sentences=len(sents),
                          passed=sum(s.verification.status == "pass" for s in sents),
                          fixed=sum(s.verification.status == "fixed" for s in sents),
                          failed=failed + sum(s.verification.status == "fail" for s in sents),
                          words=sum(words_of(s.text) for s in sents), est_pages=estimate_pages(sections))


# ---------------------------------------------------------------------------
# Fitting three pages
# ---------------------------------------------------------------------------


def _text_blocks(sec: m.SummarySection) -> list:
    return [b for b in sec.blocks if isinstance(b, (m.ParagraphBlock, m.BulletsBlock)) and b.sentences]


def _protected(secs: list[m.SummarySection], i: int) -> bool:
    """The bottom line and the holes section go last: every gap stays declared as long as possible."""
    sents = [s for _, s in all_sentences([secs[i]])]
    return i == 0 or bool(sents) and all(any(r.startswith("HOLE-") for r in s.derived_from) for s in sents)


def _prune(secs: list[m.SummarySection]) -> None:
    for sec in secs:
        sec.blocks = [b for b in sec.blocks if not isinstance(b, (m.ParagraphBlock, m.BulletsBlock)) or b.sentences]
    secs[:] = [sec for sec in secs if sec.blocks]


def _drop_chart(secs: list[m.SummarySection], removed: list[m.SummarySentence]) -> bool:
    spots = [(i, j) for i, sec in enumerate(secs) for j, b in enumerate(sec.blocks) if isinstance(b, m.ChartBlock)]
    if not spots:
        return False
    i, j = spots[-1]
    removed.append(secs[i].blocks.pop(j).caption)
    return True


def _drop_sentence(secs: list[m.SummarySection], removed: list[m.SummarySentence], *, protected_too: bool) -> bool:
    """Drop the last sentence of the wordiest section that may be trimmed."""
    cands = [i for i, sec in enumerate(secs) if _text_blocks(sec) and (protected_too or not _protected(secs, i))]
    if not cands:
        return False
    i = max(cands, key=lambda k: (sum(words_of(s.text) for _, s in all_sentences([secs[k]])), k))
    removed.append(_text_blocks(secs[i])[-1].sentences.pop())
    return True


def fit_pages(sections: list[m.SummarySection], max_pages: float = MAX_PAGES
              ) -> tuple[list[m.SummarySection], list[m.SummarySentence]]:
    """A copy of the brief trimmed to max_pages, and the sentences cut. Order: charts beyond two, then
    sentences from the wordiest ordinary section, then any chart, then the bottom line and holes."""
    secs = [s.model_copy(deep=True) for s in sections]
    removed: list[m.SummarySentence] = []
    while estimate_pages(secs) > max_pages:
        n_charts = sum(isinstance(b, m.ChartBlock) for sec in secs for b in sec.blocks)
        done = ((n_charts > MAX_CHARTS and _drop_chart(secs, removed))
                or _drop_sentence(secs, removed, protected_too=False)
                or _drop_chart(secs, removed)
                or _drop_sentence(secs, removed, protected_too=True))
        _prune(secs)
        if not done:
            break
    return secs, removed


# ---------------------------------------------------------------------------
# From the model's draft to contract blocks
# ---------------------------------------------------------------------------


def _sentence(x: summarizer.BriefSentenceOut | None) -> m.SummarySentence | None:
    """A pending sentence without an id yet, or None when it has no text or cites nothing."""
    if x is None:
        return None
    text = " ".join(x.text.split())
    refs: list[str] = []
    for r in x.derived_from:
        r = r.strip().split(":")[0]          # "C-001:rotic" cites C-001
        if r and r not in refs:
            refs.append(r)
    if not text or not refs:
        return None
    return m.SummarySentence(id="", text=text, mode=x.mode, derived_from=refs,
                             verification=m.Verification(status="pending"))


def build_sections(draft: summarizer.BriefDraft, state: RunState) -> tuple[list[m.SummarySection], list[str]]:
    """Contract sections from the draft, plus one note per thing code dropped or added."""
    notes: list[str] = []
    charts_ok = {g.id for g in state.charts() if g.verification.status in PASSING}
    used: set[str] = set()
    has_card = False
    out: list[m.SummarySection] = []
    for sec in draft.sections:
        heading = " ".join(sec.heading.split()) or "Notes"
        blocks: list = []
        for b in sec.blocks:
            if b.type == "scorecard":
                if state.rollup is None or has_card:
                    notes.append("scorecard block dropped: " + ("no rollup" if state.rollup is None else "already shown"))
                    continue
                has_card = True
                blocks.append(m.ScorecardBlock())
            elif b.type == "chart":
                cid = (b.chart_id or "").strip()
                cap = _sentence(b.caption)
                if cid not in charts_ok or cid in used or cap is None or len(used) >= MAX_CHARTS:
                    why = ("already shown" if cid in used else "not a verified chart" if cid not in charts_ok
                           else "no caption" if cap is None else f"only {MAX_CHARTS} charts fit")
                    notes.append(f"chart block {cid or '(no id)'} dropped: {why}")
                    continue
                used.add(cid)
                blocks.append(m.ChartBlock(chart_id=cid, caption=cap))
            else:
                sents = [s for s in (_sentence(x) for x in b.sentences) if s is not None]
                if len(sents) < len(b.sentences):
                    notes.append(f"{len(b.sentences) - len(sents)} sentence(s) in '{heading}' dropped: "
                                 "empty or citing nothing")
                if sents:
                    blocks.append(m.ParagraphBlock(sentences=sents) if b.type == "paragraph"
                                  else m.BulletsBlock(sentences=sents))
        if blocks:
            out.append(m.SummarySection(heading=heading, blocks=blocks))
    if state.rollup is not None and not has_card:
        out.insert(min(1, len(out)), m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()]))
        notes.append("scorecard section added by code")
    return out, notes


def brief_title(state: RunState) -> str:
    run = state.run
    who = run.company + (f" ({run.ticker})" if run.ticker else "")
    return f"{who}: BOFFIN {'screen' if run.depth == 'screen' else 'full review'}"


def log(ctx: RunContext, agent_id: str, text: str, level: str = "info") -> None:
    agent = ctx.state.agents.get(agent_id)
    ctx.emit("agent.log", m.AgentLogData(text=text, transcript_path=agent.transcript_path if agent else None),
             message=f"{agent_id}: {text}", agent_id=agent_id, level=level)


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    st = ctx.state
    n_charts = sum(g.verification.status in PASSING for g in st.charts())
    budget = word_budget(n_charts)
    payload = summarizer.report_payload(st, word_budget=budget)
    runner = AgentRunner(ctx, "summarizer", "Write the 3-page brief from verified report items", [], "large")
    with runner:
        draft = await runner.ask(summarizer.summarizer_system(budget), summarizer.summarizer_user(payload),
                                 summarizer.BriefDraft, max_tokens=SUMMARIZER_MAX_TOKENS)
        sections, notes = build_sections(draft, st)
        sections, cut = fit_pages(sections)
        if cut:
            notes.append(f"{len(cut)} sentence(s) cut to fit {MAX_PAGES:g} estimated pages")
        for note in notes:
            log(ctx, runner.agent_id, note, level="warn")
        sentences = all_sentences(sections)
        if not sentences:
            raise RuntimeError("The summarizer wrote no sentence that cites the report")
        for _, s in sentences:
            s.id = ctx.next_id("H")
        st.summary = m.HumanReport(
            run_id=st.run.run_id, title=brief_title(st), company=st.run.company, ticker=st.run.ticker,
            as_of=st.as_of or st.run.created_at[:10], status="drafting", sections=sections, holes=list(st.holes),
            disclaimer=st.disclaimer, stats=make_stats(sections), certainty=None, markdown_path=None,
            as_of_seq=st.run.last_seq)
        for heading, s in sentences:
            ctx.emit("summary.sentence", m.SummarySentenceData(sentence=s, section_heading=heading),
                     message=f"{s.id} written ({s.mode}, cites {', '.join(s.derived_from)})",
                     stage="summarize", agent_id=runner.agent_id)
