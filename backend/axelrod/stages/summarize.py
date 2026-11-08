"""Stage summarize: the summarizer writes the 3-page brief, code assigns H ids and streams summary.sentence.

The summarizer (large model) sees only verified items (pass or fixed), the rollup and the holes. Code then
drops what it cannot check (a sentence that cites nothing, a chart block without a verified chart, a second
scorecard), adds the scorecard when the model left it out, trims the brief to three estimated pages, and
only then assigns H- ids and streams the sentences. Every check runs in verify_summary.

Gaps: "What we could not determine" is a summary of at most five sentences, not the register (the full
register stays in AgentReport.holes and HumanReport.holes, and the UI shows it next to the brief). Code
writes it from the holes, grouped by kind: the register count first (stats has no field for it), unscored
criteria by name, analyst-only criteria, items that failed verification counted per criterion, and sources
or agents that gave nothing. Counts are words ("five data items"), so numbers_traced has no digit to trace,
and each sentence cites the HOLE- ids it covers. The model is asked to copy these sentences as the last
section. Whatever gap sentences it writes are replaced by code's, and model sentences that cite only HOLE-
ids are dropped elsewhere. A draft with no gap section at all gets none added (a warning says so).

Trimming is content first: charts beyond two, then extra content sentences (never the last sentence of a
section, never the last one about a category), then the gap summary down to two sentences, then charts,
then the bottom line down to one sentence. Nothing else is cut, so no category is ever dropped whole.

Page estimate (stats.est_pages, an estimate: check it in print preview): words / 450, plus 0.3 page per
chart, 0.35 for the scorecard, 0.04 per section heading and 0.15 for the title, as-of line and disclaimer.
It counts only what the brief itself renders, not the register beside it.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from ..agents import summarizer
from ..agents.base import AgentRunner
from ..contract import m
from ..verify.numbers import half_up
from .draft import ANALYST_ONLY

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
PLANNED_SECTIONS = 7          # bottom line, about four categories, scorecard, gaps
SUMMARIZER_MAX_TOKENS = 8000  # about 700 words of sentences plus the JSON around them
HOLE_HEADING = "What we could not determine"
MAX_HOLE_SENTENCES = 5
HOLE_FLOOR = 2                # trimming never takes the gap summary below this
ANALYST_BASIS = {"research_stage": "the analyst's own coverage history",
                 "fundamental_thesis": "a prior review of the company"}
SOURCE_WHY = {"failed": ("could not be fetched", "could not be fetched"),
              "blocked": ("was blocked by the gateway", "were blocked by the gateway"),
              "revoked": ("was revoked by the analyst", "were revoked by the analyst"),
              "quarantined": ("was quarantined after the prompt-attack scan",
                              "were quarantined after the prompt-attack scan"),
              "skipped": ("was skipped", "were skipped"),
              "unused": ("could not be used", "could not be used")}
UNSCORED_WHY = {"failed": ("its score did not pass verification", "their scores did not pass verification"),
                "unverified": ("its score could not be verified", "their scores could not be verified"),
                "missing": ("no score was drafted for it", "no score was drafted for them"),
                "na": ("the verified evidence does not determine it", "the verified evidence does not determine them")}


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


def word_budget(n_charts: int, *, reserved_words: int = 0) -> int:
    """Words the summarizer may write so the planned brief fits, with a 10% margin. reserved_words are
    the gap summary's, which code writes."""
    overhead = (min(n_charts, MAX_CHARTS) * CHART_PAGES + SCORECARD_PAGES + PLANNED_SECTIONS * SECTION_PAGES
                + FRAME_PAGES)
    return max(150, int((MAX_PAGES - overhead) * WORDS_PER_PAGE * 0.9) - reserved_words)


def make_stats(sections: list[m.SummarySection], *, failed: int = 0) -> m.SummaryStats:
    """failed counts sentences that failed every repair and were removed, so they are not in sections."""
    sents = [s for _, s in all_sentences(sections)]
    return m.SummaryStats(sentences=len(sents),
                          passed=sum(s.verification.status == "pass" for s in sents),
                          fixed=sum(s.verification.status == "fixed" for s in sents),
                          failed=failed + sum(s.verification.status == "fail" for s in sents),
                          words=sum(words_of(s.text) for s in sents), est_pages=estimate_pages(sections))


def hole_only(s: m.SummarySentence) -> bool:
    """A sentence about gaps alone: every ref is a HOLE- id."""
    return bool(s.derived_from) and all(r.startswith("HOLE-") for r in s.derived_from)


def _same_heading(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def is_gap_section(sec: m.SummarySection) -> bool:
    sents = [s for _, s in all_sentences([sec])]
    return _same_heading(sec.heading, HOLE_HEADING) or (bool(sents) and all(hole_only(s) for s in sents))


def split_counts(sections: list[m.SummarySection]) -> tuple[int, int]:
    """(content sentences, gap sentences): what the brief spends on findings versus on its gaps."""
    gap = sum(1 for sec in sections for _, s in all_sentences([sec]) if is_gap_section(sec) or hole_only(s))
    return len(all_sentences(sections)) - gap, gap


# ---------------------------------------------------------------------------
# The gap summary (code writes it, counts in words)
# ---------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def count_words(n: int) -> str:
    """A count in words ("twenty-five"), so a gap sentence holds no digit for numbers_traced."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return f"{_ONES[h]} hundred" + (f" {count_words(r)}" if r else "")
    t, r = divmod(n, 1000)
    return f"{count_words(t)} thousand" + (f" {count_words(r)}" if r else "")


def _n(n: int, one: str, many: str) -> str:
    return f"{count_words(n)} {one if n == 1 else many}"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _gap(text: str, holes: list[m.Hole]) -> m.SummarySentence:
    return m.SummarySentence(id="", text=text, mode="derived", derived_from=[h.hole_id for h in holes],
                             verification=m.Verification(status="pending"))


def _unscored_text(holes: list[m.Hole], items: dict[str, m.Item]) -> str:
    """'Secular growth and Leverage are not scored because their scores did not pass verification.'"""
    by_why: dict[str, list[str]] = {}
    for h in holes:
        e = items.get(h.item_id) if h.item_id else None
        st = e.verification.status if e is not None else None
        why = ("missing" if e is None else "na" if st in PASSING else "failed" if st == "fail" else "unverified")
        by_why.setdefault(why, []).append(summarizer.plain_label(h.criterion))
    clauses = [f"{_join(names)} {'is' if len(names) == 1 else 'are'} not scored because "
               f"{UNSCORED_WHY[why][len(names) > 1]}" for why, names in by_why.items()]
    return _cap(", and ".join(clauses)) + "."


def _analyst_text(holes: list[m.Hole]) -> str:
    names = [summarizer.plain_label(h.criterion) for h in holes]
    bases = list(dict.fromkeys(ANALYST_BASIS.get(h.criterion, "the analyst's own judgment") for h in holes))
    one = len(names) == 1
    return (f"{_join(names)} {'is' if one else 'are'} left to the analyst, because {'it rests' if one else 'they rest'}"
            f" on {_join(bases)}, which no source in this run can state.")


def _items_text(holes: list[m.Hole], items: dict[str, m.Item]) -> str:
    """'Five data items and four conclusions failed verification, so they are left out of the scores (...).'"""
    failed, unverified, by_crit = summarizer.left_out(holes, items)
    order = list(summarizer.KIND_WORDS)

    def kinds(c: Counter) -> str:
        return _join([_n(c[k], *summarizer.KIND_WORDS[k]) for k in order if c[k]])

    total = sum(failed.values()) + sum(unverified.values())
    named = [(c, n) for c, n in by_crit.most_common() if c]
    only = named[0][0] if len(named) == 1 and named[0][1] == total else None   # one criterion: in the subject
    fx = f" for {only}" if only else ""
    parts = []
    if failed:
        parts.append(f"{kinds(failed)}{fx} failed verification")
        fx = ""
    if unverified:
        parts.append(f"{kinds(unverified)}{fx} could not be verified")
    tail = ""
    if named and not only:
        rest = [f"{count_words(n)} for {c}" for c, n in named]
        if by_crit[None]:
            rest.append(f"{count_words(by_crit[None])} tied to no criterion")
        tail = f" ({_join(rest)})"
    so = "so it is" if total == 1 else "so they are"
    return _cap(", and ".join(parts)) + f", {so} left out of the scores{tail}."


def _sources_text(src: list[m.Hole], agents: list[m.Hole], state: RunState) -> str:
    """'One planned source, a planted test page (simulated), was quarantined after the prompt-attack scan.'"""
    by_id = state.sources_by_id()
    groups: dict[str, list[m.Source | None]] = {}
    for h in src:
        ref = re.search(r"\bS-\d{3}\b", h.reason)
        s = by_id.get(ref.group(0)) if ref else None
        groups.setdefault(s.status if s is not None and s.status in SOURCE_WHY else "unused", []).append(s)
    def simulated(ss: list[m.Source | None]) -> bool:
        return any(s is not None and "simulated" in s.title.lower() for s in ss)

    parts: list[str] = []
    if len(src) == 1:
        status, ss = next(iter(groups.items()))
        sim = ", a planted test page (simulated)," if simulated(ss) else ""
        parts.append(f"one planned source{sim} {SOURCE_WHY[status][0]}")
    elif src:
        clauses = [f"{count_words(len(ss))} {SOURCE_WHY[status][len(ss) > 1]}"
                   + (" (a planted test page, simulated)" if simulated(ss) else "") for status, ss in groups.items()]
        parts.append(f"{count_words(len(src))} planned sources were not used: {_join(clauses)}")
    if agents:
        k = len(agents)
        parts.append(f"{_n(k, 'extractor agent', 'extractor agents')} failed, so "
                     f"{'its pages' if k == 1 else 'their pages'} gave no data items")
    return _cap(", and ".join(parts)) + "."


def gap_sentences(state: RunState) -> list[m.SummarySentence]:
    """At most five sentences over the hole register, most important first (trimming cuts from the end)."""
    holes = list(state.holes)
    if not holes:
        return []
    groups: dict[str, list[m.Hole]] = {k: [] for k in ("criterion", "analyst", "item", "source", "agent", "other")}
    for h in holes:
        kind = summarizer.hole_kind(h)
        groups["analyst" if kind == "criterion" and h.criterion in ANALYST_ONLY else kind].append(h)
    out = [_gap(f"The full hole register lists {_n(len(holes), 'gap', 'gaps')} for this run.", holes)]
    if groups["criterion"]:
        out.append(_gap(_unscored_text(groups["criterion"], state.items), groups["criterion"]))
    if groups["analyst"]:
        out.append(_gap(_analyst_text(groups["analyst"]), groups["analyst"]))
    if groups["item"]:
        out.append(_gap(_items_text(groups["item"], state.items), groups["item"]))
    if groups["source"] or groups["agent"]:
        out.append(_gap(_sources_text(groups["source"], groups["agent"], state), groups["source"] + groups["agent"]))
    return out[:MAX_HOLE_SENTENCES]


def gap_section(gaps: list[m.SummarySentence]) -> m.SummarySection:
    return m.SummarySection(heading=HOLE_HEADING,
                            blocks=[m.BulletsBlock(sentences=[s.model_copy(deep=True) for s in gaps])])


# ---------------------------------------------------------------------------
# Fitting three pages, content first
# ---------------------------------------------------------------------------


def _text_blocks(sec: m.SummarySection) -> list:
    return [b for b in sec.blocks if isinstance(b, (m.ParagraphBlock, m.BulletsBlock)) and b.sentences]


def _n_text(sec: m.SummarySection) -> int:
    return sum(len(b.sentences) for b in _text_blocks(sec))


def _role(secs: list[m.SummarySection], i: int) -> str:
    """gap, card (no sentences: the scorecard), bottom (the first section) or content."""
    if is_gap_section(secs[i]):
        return "gap"
    if not all_sentences([secs[i]]):
        return "card"
    return "bottom" if i == 0 else "content"


def _cats(s: m.SummarySentence, items: dict[str, m.Item] | None) -> set[str]:
    return {items[r].category for r in s.derived_from if r in items} if items else set()


def _covers(secs: list[m.SummarySection], items: dict[str, m.Item] | None) -> Counter:
    """How many content sentences speak for each category (the bottom line does not count)."""
    c: Counter = Counter()
    for i, sec in enumerate(secs):
        if _role(secs, i) == "content":
            for _, s in all_sentences([sec]):
                c.update(_cats(s, items))
    return c


def _essential(sec: m.SummarySection, s: m.SummarySentence, items, covers: Counter, *, chart: bool = False) -> bool:
    """The last sentence of its section, or the last content sentence about one of its categories."""
    charts = sum(isinstance(b, m.ChartBlock) for b in sec.blocks)
    last = (_n_text(sec) == 0 and charts == 1) if chart else _n_text(sec) == 1
    return last or any(covers[c] <= 1 for c in _cats(s, items))


def _prune(secs: list[m.SummarySection]) -> None:
    for sec in secs:
        sec.blocks = [b for b in sec.blocks if not isinstance(b, (m.ParagraphBlock, m.BulletsBlock)) or b.sentences]
    secs[:] = [sec for sec in secs if sec.blocks]


def _drop_chart(secs: list[m.SummarySection], removed: list[m.SummarySentence], items=None, *,
                extra: bool = False) -> bool:
    """extra: a chart beyond the first two, whatever it is. Otherwise only a chart that is not essential."""
    covers = _covers(secs, items)
    spots = [(i, j) for i, sec in enumerate(secs) for j, b in enumerate(sec.blocks) if isinstance(b, m.ChartBlock)
             and (extra or _role(secs, i) != "content" or not _essential(sec, b.caption, items, covers, chart=True))]
    if not spots:
        return False
    i, j = spots[-1]
    removed.append(secs[i].blocks.pop(j).caption)
    return True


def _drop_content(secs: list[m.SummarySection], removed: list[m.SummarySentence], items=None) -> bool:
    """The last droppable sentence of the wordiest content section."""
    covers = _covers(secs, items)
    best = None
    for i, sec in enumerate(secs):
        if _role(secs, i) != "content":
            continue
        cands = [(j, k) for j, b in enumerate(sec.blocks) if isinstance(b, (m.ParagraphBlock, m.BulletsBlock))
                 for k, s in enumerate(b.sentences) if not _essential(sec, s, items, covers)]
        if cands:
            key = (sum(words_of(s.text) for _, s in all_sentences([sec])), i)
            if best is None or key > best[0]:
                best = (key, i, cands[-1])
    if best is None:
        return False
    _, i, (j, k) = best
    removed.append(secs[i].blocks[j].sentences.pop(k))
    return True


def _drop_last(secs: list[m.SummarySection], removed: list[m.SummarySentence], role: str, floor: int) -> bool:
    """The last sentence of the first section with this role, while it keeps more than floor sentences."""
    for i, sec in enumerate(secs):
        if _role(secs, i) == role and _n_text(sec) > floor:
            removed.append(_text_blocks(sec)[-1].sentences.pop())
            return True
    return False


def fit_pages(sections: list[m.SummarySection], max_pages: float = MAX_PAGES, *,
              items: dict[str, m.Item] | None = None) -> tuple[list[m.SummarySection], list[m.SummarySentence]]:
    """A copy of the brief trimmed to max_pages, and the sentences cut. Order: charts beyond two, extra
    content sentences, the gap summary down to two, other charts, the bottom line down to one. With items,
    the last content sentence about each category is kept too. If that is still too long, it stays so."""
    secs = [s.model_copy(deep=True) for s in sections]
    removed: list[m.SummarySentence] = []
    while estimate_pages(secs) > max_pages:
        n_charts = sum(isinstance(b, m.ChartBlock) for sec in secs for b in sec.blocks)
        done = ((n_charts > MAX_CHARTS and _drop_chart(secs, removed, extra=True))
                or _drop_content(secs, removed, items)
                or _drop_last(secs, removed, "gap", HOLE_FLOOR)
                or _drop_chart(secs, removed, items)
                or _drop_last(secs, removed, "bottom", 1))
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


def drafted_gaps(draft: summarizer.BriefDraft) -> list[str]:
    """The texts of the draft's gap sentences: its gap section, and any sentence citing only HOLE- ids."""
    out = []
    for sec in draft.sections:
        gap_heading = _same_heading(sec.heading, HOLE_HEADING)
        for b in sec.blocks:
            for x in [*b.sentences, *([b.caption] if b.caption else [])]:
                s = _sentence(x)
                if s is not None and (gap_heading or hole_only(s)):
                    out.append(s.text)
    return out


def build_sections(draft: summarizer.BriefDraft, state: RunState) -> tuple[list[m.SummarySection], list[str]]:
    """Contract sections from the draft, plus one note per thing code dropped or added. The draft's gap
    sentences are left out here: run() puts code's gap summary in their place."""
    notes: list[str] = []
    charts_ok = {g.id for g in state.charts() if g.verification.status in PASSING}
    used: set[str] = set()
    has_card = False
    out: list[m.SummarySection] = []
    for sec in draft.sections:
        heading = " ".join(sec.heading.split()) or "Notes"
        if _same_heading(heading, HOLE_HEADING):
            continue
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
                made = [_sentence(x) for x in b.sentences]
                sents = [s for s in made if s is not None and not hole_only(s)]
                if any(s is None for s in made):
                    notes.append(f"{sum(s is None for s in made)} sentence(s) in '{heading}' dropped: "
                                 "empty or citing nothing")
                if any(s is not None and hole_only(s) for s in made):
                    notes.append(f"{sum(s is not None and hole_only(s) for s in made)} sentence(s) in '{heading}' "
                                 "dropped: they cite only holes, and code writes the gap summary")
                if sents:
                    blocks.append(m.ParagraphBlock(sentences=sents) if b.type == "paragraph"
                                  else m.BulletsBlock(sentences=sents))
        if blocks:
            out.append(m.SummarySection(heading=heading, blocks=blocks))
    if state.rollup is not None and not has_card:
        out.insert(min(1, len(out)), m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()]))
        notes.append("scorecard section added by code")
    return out, notes


def place_gaps(sections: list[m.SummarySection], draft: summarizer.BriefDraft,
               gaps: list[m.SummarySentence]) -> list[str]:
    """Put code's gap summary last when the draft has gap sentences. Returns notes."""
    if not gaps:
        return []
    drafted = drafted_gaps(draft)
    if not drafted:
        return ["the draft has no gap section, so none was added: the hole register is shown beside the brief"]
    sections.append(gap_section(gaps))
    if drafted == [s.text for s in gaps]:
        return []
    return [f"the draft's {len(drafted)} gap sentence(s) were replaced by code's {len(gaps)}-sentence gap summary"]


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
    gaps = gap_sentences(st)
    n_charts = sum(g.verification.status in PASSING for g in st.charts())
    budget = word_budget(n_charts, reserved_words=sum(words_of(s.text) for s in gaps))
    payload = summarizer.report_payload(st, word_budget=budget, gap_summary=[
        {"text": s.text, "derived_from": list(s.derived_from)} for s in gaps])
    runner = AgentRunner(ctx, "summarizer", "Write the 3-page brief from verified report items", [], "large")
    with runner:
        draft = await runner.ask(summarizer.summarizer_system(budget), summarizer.summarizer_user(payload),
                                 summarizer.BriefDraft, max_tokens=SUMMARIZER_MAX_TOKENS)
        sections, notes = build_sections(draft, st)
        notes += place_gaps(sections, draft, gaps)
        sections, cut = fit_pages(sections, items=st.items)
        if cut:
            notes.append(f"{len(cut)} sentence(s) cut to fit {MAX_PAGES:g} estimated pages")
        for note in notes:
            log(ctx, runner.agent_id, note, level="warn")
        sentences = all_sentences(sections)
        if not sentences:
            raise RuntimeError("The summarizer wrote no sentence that cites the report")
        for _, s in sentences:
            s.id = ctx.next_id("H")
        content, gap = split_counts(sections)
        log(ctx, runner.agent_id, f"brief drafted: {content} content and {gap} gap sentence(s), "
                                  f"{len(st.holes)} hole(s) in the register")
        st.summary = m.HumanReport(
            run_id=st.run.run_id, title=brief_title(st), company=st.run.company, ticker=st.run.ticker,
            as_of=st.as_of or st.run.created_at[:10], status="drafting", sections=sections, holes=list(st.holes),
            disclaimer=st.disclaimer, stats=make_stats(sections), certainty=None, markdown_path=None,
            as_of_seq=st.run.last_seq)
        for heading, s in sentences:
            ctx.emit("summary.sentence", m.SummarySentenceData(sentence=s, section_heading=heading),
                     message=f"{s.id} written ({s.mode}, cites {', '.join(s.derived_from)})",
                     stage="summarize", agent_id=runner.agent_id)
