"""Stage extract: one small-model extractor per page, offsets by code, data checks right away, code extraction for JSON.

Text sources: every page the plan lists (extract_pages, or the whole document for HTML pseudo-pages) gets
its own extractor agent. The agent returns quotes, never offsets: code locates each quote in the stored
page with verify.text, assigns the D- id, picks the unit and period context by position (table headers,
the scale written next to a number, the heading or phrase that names a sentence's period), emits
item.created (pending) and runs the data checks. A failure gets one fix by code toward the source (a
`recite` attempt, the source wins): a near-miss or cell-skipping quote is re-cited from the real span on
the page the extractor read, and a value the agent decorated is restated as printed. Then item.verified
carries the certainty from contract/certainty.py.

JSON sources (SEC companyfacts): code reads the needed concepts by JSON pointer, no model, extracted_by
"code". Quarantined documents never reach an extractor (RunState.agent_pages is empty for them).

After every page is done, the tier rule runs over the tier 2 and tier 3 data items, because it compares
them with the priority items.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..agents.base import AgentOutputError, AgentRunner
from ..agents.extractor import SYSTEM, ExtractedItem, ExtractorOutput, build_user_prompt
from ..contract import RUBRIC, certainty, m
from ..gateway import Blocked
from ..ids import now_iso
from ..runtime import StopRequested
from ..verify import checks, text
from ..verify.numbers import NumToken, SCALES, half_up, parse_number, period_key, period_mentions, scan_numbers

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

STAGE = "extract"
APPROVED = ("approved", "auto_approved")
SKIP_SOURCE = ("revoked", "blocked", "failed", "skipped", "quarantined")
MAX_TOKENS = 8192                 # one page rarely needs more than ~5k output tokens
MAX_ITEMS_PER_PAGE = 40
MAX_UNPLANNED_PDF_PAGES = 30      # a PDF with no extract_pages (analyst-added) is read this far only
MIN_RECITE_SIMILARITY = 0.8       # below this the closest span is a different text, not a slip
PEER_FIRST_YEAR = 2020            # first fiscal year of peer flows; balances start a year earlier
PEER_MAX_PERIODS = 7


# ---------------------------------------------------------------------------
# Work plan
# ---------------------------------------------------------------------------


@dataclass
class PageJob:
    source_id: str
    doc_id: str
    page: int
    needs: list[m.DataNeed]
    runner: AgentRunner | None = None


@dataclass
class JsonJob:
    source_id: str
    doc_id: str
    runner: AgentRunner | None = None


@dataclass
class SourceWork:
    source_id: str
    pages: list[PageJob] = field(default_factory=list)
    json_docs: list[JsonJob] = field(default_factory=list)


def plan_work(state: RunState) -> list[SourceWork]:
    """Approved, stored, readable sources in tier then rank order, with the pages each extractor reads."""
    docs = state.documents_by_id()
    done = {it.source_id for it in state.data_items()}   # extracted before a restart: never twice
    work: list[SourceWork] = []
    for source in state.sorted_sources():
        if source.approval not in APPROVED or source.status in SKIP_SOURCE or source.source_id in done:
            continue
        w = SourceWork(source.source_id)
        for doc_id in source.doc_ids:
            doc = docs.get(doc_id)
            if doc is None or not state.agent_pages(doc_id):
                continue  # quarantined or never stored: no agent reads it
            if doc.content_type == "json":
                w.json_docs.append(JsonJob(source.source_id, doc_id))
                continue
            for p in pages_to_read(state, source, doc):
                w.pages.append(PageJob(source.source_id, doc_id, p, needs_for_page(state, source, p)))
        if w.pages or w.json_docs:
            work.append(w)
    return work


def pages_to_read(state: RunState, source: m.Source, doc: m.Document) -> list[int]:
    """extract_pages when the plan has them (1-based, inclusive, clipped), else every page of an HTML or
    text document, else the first MAX_UNPLANNED_PDF_PAGES pages of a PDF."""
    n = len(state.pages.get(doc.doc_id, []))
    ranges = state.extract_pages.get(source.source_id)
    if ranges:
        return sorted({p for lo, hi in ranges for p in range(int(lo), int(hi) + 1) if 1 <= p <= n})
    if doc.content_type == "pdf":
        return list(range(1, min(n, MAX_UNPLANNED_PDF_PAGES) + 1))
    return list(range(1, n + 1))


def needs_for_page(state: RunState, source: m.Source, page: int) -> list[m.DataNeed]:
    """need_pages says which needs a page serves. Without an entry, the source's needs, else every need."""
    hits = {nid for nid, per_source in state.need_pages.items()
            if any(int(lo) <= page <= int(hi) for lo, hi in per_source.get(source.source_id, []))}
    wanted = hits or set(source.need_ids)
    needs = [n for n in state.needs if n.need_id in wanted]
    return needs or list(state.needs)


# ---------------------------------------------------------------------------
# Stage entry
# ---------------------------------------------------------------------------


async def run(ctx: RunContext) -> None:
    state = ctx.state
    work = plan_work(state)
    pages_by_doc = readable_pages(state)
    for w in work:  # spawn every agent up front (queued), so ids follow the plan's order
        for j in w.json_docs:
            j.runner = AgentRunner(ctx, "extractor", f"Read {j.source_id} structured facts by code (no model)",
                                   [j.source_id], None)
        for p in w.pages:
            p.runner = AgentRunner(ctx, "extractor", f"Extract {p.source_id} page {p.page}", [p.source_id], "small")
    sem = asyncio.Semaphore(max(1, ctx.settings.max_concurrency_small))
    results = await asyncio.gather(*(extract_source(ctx, w, pages_by_doc, sem) for w in work),
                                   return_exceptions=True)
    stop = next((r for r in results if isinstance(r, StopRequested)), None)
    if stop is not None:
        raise stop
    crash = next((r for r in results if isinstance(r, BaseException)), None)
    if crash is not None:
        raise crash
    changed = apply_tier_rules(ctx)
    for sid in changed:
        source = state.sources_by_id().get(sid)
        if source is not None and source.status == "extracted":
            set_source_status(ctx, source, "extracted")  # stats moved with the tier rule


def readable_pages(state: RunState) -> dict[str, list[str]]:
    """What the checker may match against: every stored document except quarantined ones."""
    return {d.doc_id: state.agent_pages(d.doc_id) for d in state.documents if not d.quarantined}


async def extract_source(ctx: RunContext, w: SourceWork, pages_by_doc: dict[str, list[str]],
                         sem: asyncio.Semaphore) -> None:
    source = ctx.state.sources_by_id()[w.source_id]
    if source.status not in ("revoked",):
        set_source_status(ctx, source, "extracting")
    jobs = [extract_json(ctx, j, pages_by_doc) for j in w.json_docs]
    jobs += [extract_page(ctx, p, pages_by_doc, sem) for p in w.pages]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    if source.status != "revoked":  # a revoke that landed meanwhile stays on screen
        set_source_status(ctx, source, "extracted")


# ---------------------------------------------------------------------------
# Text pages: one extractor agent per page
# ---------------------------------------------------------------------------


async def extract_page(ctx: RunContext, job: PageJob, pages_by_doc: dict[str, list[str]],
                       sem: asyncio.Semaphore) -> list[str]:
    """Ask one extractor for one page, then build and check its items. Returns the new item ids."""
    runner = job.runner or AgentRunner(ctx, "extractor", f"Extract {job.source_id} page {job.page}",
                                       [job.source_id], "small")
    async with sem:
        try:
            ctx.check_stop()
        except StopRequested:
            runner.finish("stopped")
            raise
        state = ctx.state
        source = state.sources_by_id()[job.source_id]
        pages = state.agent_pages(job.doc_id)
        if source.status == "revoked" or not pages:
            why = "source revoked" if source.status == "revoked" else "document quarantined"
            log(ctx, runner, f"Skipped {job.source_id} page {job.page}: {why}", level="warn")
            runner.finish("done")
            return []
        page_text = pages[job.page - 1]
        if not page_text.strip():
            log(ctx, runner, f"{job.source_id} page {job.page} has no text")
            runner.finish("done")
            return []
        doc = state.documents_by_id()[job.doc_id]
        user = build_user_prompt(source=source, doc=doc, page=job.page, page_text=page_text, needs=job.needs,
                                 fiscal_note=state.fiscal_note)
        try:
            out = await runner.ask(SYSTEM, user, ExtractorOutput, max_tokens=MAX_TOKENS)
        except StopRequested:
            runner.finish("stopped")
            raise
        except (Blocked, AgentOutputError) as e:
            reason = e.reason if isinstance(e, Blocked) else str(e).splitlines()[0]
            log(ctx, runner, f"No items from {job.source_id} page {job.page}: {reason}", level="warn")
            runner.finish("failed")
            return []
    try:
        ids = await add_agent_items(ctx, job, out.items, pages_by_doc, runner.agent_id)
    except BaseException:
        runner.finish("failed")
        raise
    log(ctx, runner, f"{job.source_id} page {job.page}: {len(ids)} item(s) extracted")
    runner.finish("done")
    return ids


async def add_agent_items(ctx: RunContext, job: PageJob, raw_items: list[ExtractedItem],
                          pages_by_doc: dict[str, list[str]], agent_id: str) -> list[str]:
    """Build, register and check each extracted item: item.created, then item.verified."""
    pages = pages_by_doc.get(job.doc_id) or ctx.state.agent_pages(job.doc_id)
    source = ctx.state.sources_by_id()[job.source_id]
    seen: set[tuple[str, str, str]] = set()
    ids: list[str] = []
    for raw in raw_items[:MAX_ITEMS_PER_PAGE]:
        key = (text.normalize(raw.quote).strip(), raw.value_as_reported.strip(), raw.period.strip())
        if not key[0] or key in seen:
            continue
        seen.add(key)
        # locating a quote that is not on the page searches the whole document, so keep it off the loop
        item = await asyncio.to_thread(build_item, ctx, source, job, raw, pages, agent_id)
        register(ctx, item)
        verified = await asyncio.to_thread(verify_item, item, pages_by_doc, ctx.state.sources_by_id())
        record_verified(ctx, verified)
        ids.append(item.id)
    return ids


def build_item(ctx: RunContext, source: m.Source, job: PageJob, raw: ExtractedItem, pages: list[str],
               agent_id: str) -> m.DataItem:
    """A pending DataItem: code-assigned id, citations located by code, value parsed by code."""
    value = make_value(raw)
    ev = cite_on_page(job.doc_id, pages, job.page, raw.quote, "evidence")
    value = scale_from_evidence(pages, ev, value)
    cits = [ev] + context_citations(job.doc_id, pages, ev, raw, value, source=source)
    page_needs = [n.need_id for n in job.needs]
    need_ids = [n for n in raw.need_ids if n in page_needs] or page_needs[:1]
    criteria, category = criteria_for(ctx.state, need_ids, source)
    statement = raw.statement.strip() or raw.metric.strip() or text.normalize(raw.quote).strip()
    return m.DataItem(
        id=ctx.next_id("D"), category=category, criteria=criteria, need_id=need_ids[0] if need_ids else None,
        statement=statement, value=value, citations=cits, source_id=source.source_id,
        source_tier=source.tier, extracted_by="agent", agent_id=agent_id,
        verification=m.Verification(status="pending"),
    )


def cite_on_page(doc_id: str, pages: list[str], page: int, quote: str, role: str) -> m.Citation:
    """The extractor read one page, so its quote is looked up on that page. Found: the citation carries
    the exact source characters at the span. Not found: it keeps the agent's text and points at the
    closest real span on the page, so the failed check shows the diff and the recite has a target."""
    page_text = pages[page - 1] if 1 <= page <= len(pages) else ""
    hit = text.find_quote(page_text, quote) if page_text else None
    if hit is not None:
        s, e = hit
        return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=page, char_start=s, char_end=e),
                          quote=page_text[s:e], role=role)
    _, (s, e) = text.near_miss(page_text, quote) if page_text else ([], (0, 0))
    return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=page, char_start=s, char_end=e), quote=quote,
                      role=role)


# Table headers code can find above a row: "(in millions of €, earnings per share in €)" and year runs
# such as "2025 \n2024" (one cell per line) or a header line like "Sep 30, 2025 Sep 30, 2024".
_UNIT_HEADER = re.compile(r"\((?:[Ii]n|[Aa]mounts in) (?:(?:€|EUR|\$|USD|US\$)\s?)?"
                          r"(?:thousands|millions|billions|m|bn)\b[^()\n]*\)")
_NAMES_UNIT = re.compile(r"thousand|million|billion|\bmn\b|\bbn\b|€|\$|£|\bEUR\b|\bUSD\b|\bCHF\b", re.IGNORECASE)
_YEAR_RUN = re.compile(r"(?<![\w.,])(?:19|20)\d{2}(?:[ \t ]*\n?[ \t ]*(?:19|20)\d{2})+(?![\w.,%])")


_SCALE_WORDS = ("thousand", "million", "billion")


def context_citations(doc_id: str, pages: list[str], ev: m.Citation, raw: ExtractedItem,
                      value: m.Value | None, *, source: m.Source | None = None) -> list[m.Citation]:
    """unit_context and period_context citations, from the evidence's own page (a release may also
    give its cover-page period).

    Unit: a scale written next to the number in the evidence ("€31 billion") is cited as it stands.
    Otherwise the agent's header line is used when it is on that page, else the nearest unit header
    above the evidence. A percentage needs no scale header.

    Period: the agent's header line when it is on the page above the evidence and names a period.
    When the evidence does not name the item's period itself, code looks on its own, in this order: the
    nearest table year header above the row, the nearest line above that names a fiscal year, quarter
    or date (a heading line is cited whole), then a release's cover-page period. Those are used only when
    the value's own clause in the evidence names no period ("8% (2024: 9%)": 8% may take the paragraph's
    year, 9% may not). When the evidence names several years ("For fiscal year 2027, we expect ...
    over fiscal year 2026"), its own leading period phrase before the value is cited. Code picks the
    context by position, never by the period the model claims, so the period check still tests the claim.
    """
    page = ev.locator.page
    page_text = _page(pages, page)
    at = ev.locator.char_start
    out: list[m.Citation] = []
    pct = value is not None and value.unit == "%"
    ev_norm = text.normalize(ev.quote)
    if not pct:
        needs_unit = value is not None and value.scale != "one" and not any(t.word for t in scan_numbers(ev_norm))
        uc = _scale_token_citation(doc_id, page_text, ev, value)
        if uc is None:
            uc = _agent_header(doc_id, page_text, page, raw.unit_quote, "unit_context", at,
                               lambda q: bool(_NAMES_UNIT.search(q)))
        if uc is None and needs_unit:
            uc = _header_above(doc_id, page_text, page, at, "unit_context", _unit_spans(page_text))
        if uc is None and needs_unit and raw.unit_quote.strip():
            uc = cite_on_page(doc_id, pages, page, raw.unit_quote, "unit_context")  # fails with a diff
        if uc is not None:
            out.append(uc)
    pc = _period_context(doc_id, pages, ev, raw, value, source)
    if pc is not None:
        out.append(pc)
    return out


def _names_period_header(q: str) -> bool:
    return bool(period_mentions(text.normalize(q)))


def _period_context(doc_id: str, pages: list[str], ev: m.Citation, raw: ExtractedItem, value: m.Value | None,
                    source: m.Source | None) -> m.Citation | None:
    """The period_context citation for context_citations, or None when the evidence settles the period."""
    page = ev.locator.page
    page_text = _page(pages, page)
    at = ev.locator.char_start
    pc = _agent_header(doc_id, page_text, page, raw.period_quote, "period_context", at, _names_period_header)
    if pc is not None or value is None or not value.period:
        return _with_quarter_labels(doc_id, page_text, pc) if pc is not None else None
    ev_norm = text.normalize(ev.quote)
    needs_period = not _names_period(ev_norm, value.period)
    several = len({y for y, _, _ in period_mentions(ev_norm)}) >= 2
    if needs_period:
        pc = _header_above(doc_id, page_text, page, at, "period_context", _year_spans(page_text))
        if pc is not None:
            return _with_quarter_labels(doc_id, page_text, pc)
    if pc is None and (needs_period or several) and _verbatim(page_text, ev) and _clause_free(ev_norm, value):
        own = _anchors(ev_norm, heading=False)
        if own:  # the evidence states a period of its own: only its leading phrase may set it
            pc = _leading_period(doc_id, page_text, ev, ev_norm, value, own)
        elif needs_period:
            pc = _anchor_above(doc_id, page_text, page, at) or _cover_period(doc_id, pages, source, page)
    if pc is None and needs_period and raw.period_quote.strip():
        pc = cite_on_page(doc_id, pages, page, raw.period_quote, "period_context")  # fails with a diff
    return pc


def _page(pages: list[str], page: int) -> str:
    return pages[page - 1] if 1 <= page <= len(pages) else ""


_QUARTER_LINE = re.compile(r"(?:Q[1-4]|[1-4]Q|H[12]|(?:first|second|third|fourth)[\s-]quarter)", re.IGNORECASE)


def _with_quarter_labels(doc_id: str, page_text: str, pc: m.Citation) -> m.Citation:
    """A year header widened up over the quarter labels of its columns. Quarterly tables print "Q4" per
    column on lines of their own above the unit header and the years, and the header is all of it."""
    s, e = pc.locator.char_start, pc.locator.char_end
    if not _verbatim(page_text, pc):
        return pc
    top = None
    pos = page_text.rfind("\n", 0, s) + 1  # start of the header's first line
    while pos > 0:
        prev = page_text.rfind("\n", 0, pos - 1) + 1
        line = page_text[prev:pos - 1]
        st = line.strip()
        unit = _UNIT_HEADER.match(st)
        if _QUARTER_LINE.fullmatch(st):
            top = prev + len(line) - len(line.lstrip())
        elif st and not (top is None and (st in ("Note", "Notes") or (unit and len(st) - unit.end() <= 2))):
            break
        pos = prev
    if top is None or top >= s:
        return pc
    return _cite_span(doc_id, page_text, pc.locator.page, top, e, "period_context")


def _verbatim(page_text: str, cit: m.Citation) -> bool:
    """True when the citation's span holds its quote (after normalization), so offsets inside it are real."""
    loc = cit.locator
    if not page_text or not 0 <= loc.char_start < loc.char_end <= len(page_text):
        return False
    return text.normalize(page_text[loc.char_start:loc.char_end]) == text.normalize(cit.quote)


def _cite_span(doc_id: str, page_text: str, page: int, s: int, e: int, role: str) -> m.Citation:
    return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=page, char_start=s, char_end=e),
                      quote=page_text[s:e], role=role)


def _cite_inside(doc_id: str, page_text: str, cit: m.Citation, fragment: str, role: str) -> m.Citation | None:
    """A citation of fragment (normalized text) found inside a verbatim citation's span."""
    s0, e0 = cit.locator.char_start, cit.locator.char_end
    hit = text.find_quote(page_text[s0:e0], fragment)
    if hit is None:
        return None
    return _cite_span(doc_id, page_text, cit.locator.page, s0 + hit[0], s0 + hit[1], role)


# ---------------------------------------------------------------------------
# The value inside its evidence: scale words, clauses, qualifiers
# ---------------------------------------------------------------------------

# Words and symbols a model puts in front of a number the page writes in words: "~37", ">31", "c. 5".
_QUALIFIER = re.compile(r"^(?:\s*(?:~|≈|∼|>=?|<=?|≥|≤|±|c\.|ca\.|approx\.?|approximately|about|around|over|"
                        r"more than|less than|nearly|almost|roughly|some)\s*)+", re.IGNORECASE)
_CURRENCY_MARK = re.compile(r"[€$£¥]\s?|\b(?:EUR|USD|GBP|CHF|JPY)\s?")


def _bare_reported(shown: str) -> str:
    """as_reported without a leading qualifier the model added ("~37" gives "37")."""
    return _QUALIFIER.sub("", shown).strip()


def _value_tokens(q_norm: str, as_reported: str) -> list[NumToken]:
    """Tokens in a normalized quote with the same digits as as_reported (qualifiers and signs aside)."""
    toks = scan_numbers(_bare_reported(text.normalize(as_reported).strip()), include_labels=True)
    if not toks:
        return []
    core = toks[0].core
    return [t for t in scan_numbers(q_norm, include_labels=True) if t.core == core]


def scale_from_evidence(pages: list[str], ev: m.Citation, value: m.Value | None) -> m.Value | None:
    """The scale written next to the number in the evidence ("€31 billion", "16 billion tests") wins over
    the model's scale. Code reads it from the stored page, so only a verbatim evidence quote counts."""
    if value is None or value.unit == "%" or not _verbatim(_page(pages, ev.locator.page), ev):
        return value
    word = _written_scale(_value_tokens(text.normalize(ev.quote), value.as_reported))
    return value if word is None or word == value.scale else value.model_copy(update={"scale": word})


def _written_scale(hits: list[NumToken]) -> str | None:
    """The scale word written next to every appearance of the value, or None. A value that also appears
    bare ("Loan (€700 million) ... 700") is a table cell, and the word belongs to the other number."""
    words = {t.word for t in hits}
    if len(words) != 1:
        return None
    word = words.pop()
    return word if word in _SCALE_WORDS else None


def _scale_token_citation(doc_id: str, page_text: str, ev: m.Citation, value: m.Value | None) -> m.Citation | None:
    """unit_context for a value whose scale is written next to it: the number with its scale word, cited
    inside the evidence ("€31 billion")."""
    if value is None or not _verbatim(page_text, ev):
        return None
    q = text.normalize(ev.quote)
    hits = _value_tokens(q, value.as_reported)
    if _written_scale(hits) != value.scale:
        return None
    tok = hits[0]
    return _cite_inside(doc_id, page_text, ev, q[tok.start:tok.end], "unit_context")


# Clause boundaries inside an evidence sentence: the checker's cuts plus "over" and "than", which
# introduce the comparison base ("growth of 4.0% over fiscal year 2026", "more than fiscal year 2024").
_CLAUSE_CUT = re.compile(r"[;()\[\]]|,\s|\s(?:compared\s+(?:with|to)|versus|vs\.?|against|from|to|while|whereas|"
                         r"over|than)\s", re.IGNORECASE)
_PRIOR_WORDS = re.compile(r"\b(?:prior|previous)[\s-]year\b|\byear[\s-](?:earlier|before|ago)\b", re.IGNORECASE)


def _clause_free(ev_norm: str, value: m.Value) -> bool:
    """True when some appearance of the value sits in a clause of the evidence that names no period and
    is not a prior-year comparative ("(2024: 9%)", "(prior year: €1.00)"). Only then may the period come
    from the context: a value whose own clause names a year is checked against that year."""
    hits = _value_tokens(ev_norm, value.as_reported)
    if not hits:
        return True  # value_in_quote fails on its own
    protected = ([(t.start, t.end) for t in scan_numbers(ev_norm, include_labels=True)]
                 + [(s, e) for _, s, e in period_mentions(ev_norm)])
    cuts = [mm.span() for mm in _CLAUSE_CUT.finditer(ev_norm) if not any(s <= mm.start() < e for s, e in protected)]
    for h in hits:
        left = max([ce for cs, ce in cuts if ce <= h.start], default=0)
        right = min([cs for cs, ce in cuts if cs >= h.end], default=len(ev_norm))
        clause = ev_norm[left:right]
        if not period_mentions(clause) and not _PRIOR_WORDS.search(clause):
            return True
    return False


# ---------------------------------------------------------------------------
# Period anchors: where a page says which period a sentence or bullet belongs to
# ---------------------------------------------------------------------------

_COMPARE_LEFT = re.compile(r"\b(?:over|than|versus|vs\.?|against|compared\s+(?:with|to)|level\s+of|relative\s+to)"
                           r"\s+(?:the\s+)?$", re.IGNORECASE)
_QUARTER_LEFT = re.compile(r"(?:\bQ[1-4]\s?|\b(?:first|second|third|fourth)[\s-]quarter(?:\s+of)?(?:\s+the)?\s+)$",
                           re.IGNORECASE)
_BARE_YEAR = re.compile(r"(?:19|20)\d{2}")
_RELEASE = re.compile(r"release|quarterly|interim|half[- ]year|results", re.IGNORECASE)
MAX_HEADING_CHARS = 80


def _anchors(seg: str, *, heading: bool) -> list[tuple[int, int, int]]:
    """Period mentions that say which period a value belongs to: not a prior-year comparative
    ("(2024: 9%)"), not a comparison base ("over fiscal year 2026"), and not a bare year in running
    text ("by 2030"), which may mean anything. A heading line may name a bare year ("Annual Report 2025")."""
    out = []
    for y, s, e in period_mentions(seg):
        if re.match(r"\s?\)?\s?:", seg[e:e + 4]) or _COMPARE_LEFT.search(seg[max(0, s - 40):s]):
            continue
        if not heading and _BARE_YEAR.fullmatch(seg[s:e]):
            continue
        out.append((y, s, e))
    return out


def _is_heading(line: str) -> bool:
    """A short line with words and a period, no amounts and no sentence end: "Q4 Fiscal Year 2026",
    "Outlook for Fiscal Year 2027". "... completed by mid-2028." is a sentence, not a heading."""
    s = text.normalize(line).strip()
    return (0 < len(s) <= MAX_HEADING_CHARS and bool(re.search(r"[A-Za-z]{3}", s)) and not scan_numbers(s)
            and not re.search(r"[.;!?]$", s) and bool(_anchors(s, heading=True)))


def _anchor_span(line: str, anchor: tuple[int, int, int]) -> tuple[int, int]:
    """The mention, widened left over a quarter label next to it ("fourth quarter of fiscal year 2026")."""
    _, s, e = anchor
    mm = _QUARTER_LEFT.search(line[:s])
    return (mm.start() if mm else s), e


def _lines(page_text: str, end: int) -> list[tuple[int, str, bool]]:
    """(start, text, whole) for each line of page_text[:end]; whole is False for a line cut by end."""
    out: list[tuple[int, str, bool]] = []
    pos = 0
    for line in page_text[:end].splitlines(keepends=True):
        body = line.rstrip("\r\n")
        whole = line != body or (pos + len(line) == end and page_text[end:end + 1] in ("\n", "\r", ""))
        out.append((pos, body, whole))
        pos += len(line)
    return out


def _anchor_above(doc_id: str, page_text: str, page: int, before: int) -> m.Citation | None:
    """The nearest period anchor above the evidence on its page. A heading line is cited whole; in running
    text the nearest mention is cited (with its quarter label)."""
    for pos, body, whole in reversed(_lines(page_text, before)):
        if whole and _is_heading(body):
            lead = len(body) - len(body.lstrip())
            return _cite_span(doc_id, page_text, page, pos + lead, pos + len(body.rstrip()), "period_context")
        found = _anchors(body, heading=False)
        if found:
            s, e = _anchor_span(body, found[-1])
            return _cite_span(doc_id, page_text, page, pos + s, pos + e, "period_context")
    return None


def _cover_period(doc_id: str, pages: list[str], source: m.Source | None, page: int) -> m.Citation | None:
    """A release's cover page states the period it reports on: its first heading or period mention."""
    if source is None or page <= 1 or not pages or not (source.kind == "company_ir" or _RELEASE.search(source.title)):
        return None
    cover = pages[0]
    for pos, body, _ in _lines(cover, len(cover)):
        if _is_heading(body):
            lead = len(body) - len(body.lstrip())
            return _cite_span(doc_id, cover, 1, pos + lead, pos + len(body.rstrip()), "period_context")
        found = _anchors(body, heading=False)
        if found:
            s, e = _anchor_span(body, found[0])
            return _cite_span(doc_id, cover, 1, pos + s, pos + e, "period_context")
    return None


def _leading_period(doc_id: str, page_text: str, ev: m.Citation, ev_norm: str, value: m.Value,
                    own: list[tuple[int, int, int]]) -> m.Citation | None:
    """The evidence's own first period anchor, when it comes before the value: "For fiscal year 2027, we
    expect growth between 4.0% and 5.0% over fiscal year 2026" belongs to 2027."""
    hits = _value_tokens(ev_norm, value.as_reported)
    if not hits:
        return None
    first = min(h.start for h in hits)
    lead = [a for a in own if a[2] <= first]
    if not lead:
        return None
    s, e = _anchor_span(ev_norm, lead[0])
    return _cite_inside(doc_id, page_text, ev, ev_norm[s:e], "period_context")


def _agent_header(doc_id: str, page_text: str, page: int, quote: str, role: str, before: int,
                  ok) -> m.Citation | None:
    """The agent's header line, if it is on the page above the evidence and passes ok(quote)."""
    if not quote.strip() or not page_text:
        return None
    hit = text.find_quote(page_text, quote)
    if hit is None or hit[0] > before or not ok(page_text[hit[0]:hit[1]]):
        return None
    return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=page, char_start=hit[0], char_end=hit[1]),
                      quote=page_text[hit[0]:hit[1]], role=role)


def _header_above(doc_id: str, page_text: str, page: int, before: int, role: str,
                  spans: list[tuple[int, int]]) -> m.Citation | None:
    """The last header span that ends at or before the evidence starts."""
    above = [(s, e) for s, e in spans if e <= before]
    if not above:
        return None
    s, e = max(above, key=lambda t: t[1])
    return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=page, char_start=s, char_end=e),
                      quote=page_text[s:e], role=role)


def _unit_spans(page_text: str) -> list[tuple[int, int]]:
    return [mm.span() for mm in _UNIT_HEADER.finditer(page_text)]


def _year_spans(page_text: str) -> list[tuple[int, int]]:
    """Runs of two or more years (one cell per line), whole lines that name two or more periods and
    hold no amounts ("Fiscal year Note 2025 2024", "Sep 30, 2025 Sep 30, 2024"), and blocks of two or
    more lines that each hold one period and nothing else ("Sept 30, 2019\\nSept 30, 2018")."""
    spans = [mm.span() for mm in _YEAR_RUN.finditer(page_text)]
    pos = 0
    block: list[int] | None = None  # [start, end, lines]
    for line in page_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = body.strip()
        lead = len(body) - len(body.lstrip())
        if (len({y for y, _, _ in period_mentions(body)}) >= 2 and not re.search(r"\d,\d{3}|\d\.\d|%", body)
                and stripped):
            spans.append((pos + lead, pos + len(body.rstrip())))
        ments = period_mentions(stripped)
        if len(ments) == 1 and ments[0][1] == 0 and ments[0][2] == len(stripped):
            end = pos + len(body.rstrip())
            block = [block[0], end, block[2] + 1] if block else [pos + lead, end, 1]
        elif stripped:
            if block and block[2] >= 2:
                spans.append((block[0], block[1]))
            block = None
        pos += len(line)
    if block and block[2] >= 2:
        spans.append((block[0], block[1]))
    return spans


_QUARTER_NAMES = {1: "first", 2: "second", 3: "third", 4: "fourth"}


def _names_period(quote_norm: str, period: str) -> bool:
    """True when the quote itself names every year of the period (and its quarter), so no header is needed."""
    years, quarter = period_key(period)
    found = {y for y, _, _ in period_mentions(quote_norm)}
    if not years or not years <= found:
        return False
    if quarter is None:
        return True
    rx = rf"\bQ{quarter}\b|\b{quarter}Q\b|\b{_QUARTER_NAMES[quarter]}[\s-]quarter\b"
    return bool(re.search(rx, quote_norm, re.IGNORECASE))


def make_value(raw: ExtractedItem) -> m.Value | None:
    """value.number by code from value_as_reported, before scale. None for a fact without a number."""
    shown = raw.value_as_reported.strip()
    if not shown:
        return None
    toks = scan_numbers(text.normalize(shown), include_labels=True)
    if not toks:
        return None
    tok = toks[0]
    parsed = parse_number(shown) if tok.word is None else None
    number = parsed if parsed is not None else tok.number
    pct = tok.pct or raw.unit.strip().lower() in ("%", "percent", "pct")
    scale = raw.scale
    if tok.word in _SCALE_WORDS:
        scale = tok.word  # "€2.2 billion": the scale is part of what was reported
    elif scale == "one" and _UNIT_SCALES.get(raw.unit.strip().lower()):
        scale = _UNIT_SCALES[raw.unit.strip().lower()]  # unit "billion" for "16 billion tests"
    unit = "%" if pct else unit_code(raw.unit, tok.currency)
    return m.Value(number=float(number), as_reported=shown, unit=unit, scale="one" if pct else scale,
                   period=raw.period.strip() or None)


_UNIT_CODES = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "usd": "USD", "us$": "USD",
               "dollar": "USD", "dollars": "USD", "£": "GBP", "gbp": "GBP", "chf": "CHF"}
# A scale word sent as the unit is a scale: the unit is then the currency, else a count.
_UNIT_SCALES = {"thousand": "thousand", "thousands": "thousand", "million": "million", "millions": "million",
                "mn": "million", "billion": "billion", "billions": "billion", "bn": "billion"}


def unit_code(unit: str, currency: str | None) -> str:
    u = unit.strip()
    if not u or u.lower() in _UNIT_SCALES:
        return currency or "count"
    return _UNIT_CODES.get(u.lower(), u)


def criteria_for(state: RunState, need_ids: list[str], source: m.Source) -> tuple[list[str], str]:
    """Criteria from the needs (in need order), else the source's. Category from the first criterion."""
    by_id = {n.need_id: n for n in state.needs}
    crits: list[str] = []
    for nid in need_ids:
        for c in by_id[nid].criteria if nid in by_id else []:
            if c not in crits:
                crits.append(c)
    crits = crits or list(source.criteria) or ["fundamental_thesis"]
    return crits, RUBRIC["criteria"][crits[0]]["category"]


# ---------------------------------------------------------------------------
# Verification: checks, one recite attempt, certainty
# ---------------------------------------------------------------------------


def verify_item(item: m.DataItem, pages_by_doc: dict[str, list[str]],
                sources: dict[str, m.Source]) -> m.DataItem:
    """Run the data checks. On a failure, code makes one fix toward the source and checks again (status
    fixed when that passes): a near-miss quote is re-cited from the real span on the page the extractor
    read (a whole table row when the agent skipped cells), and a value the agent decorated ("~37",
    "(4.2%)" for a printed "4.2%", "–607" for "– 607") is restated as printed. Pure: returns a new item.
    Runs in a worker thread."""
    first = checks.check_data_item(item, pages_by_doc, sources)
    item = checks.with_fixed_locators(item, first)
    status = checks.status_from(first)
    if status != "fail":
        return _set_verification(item, m.Verification(status=status, checks=first, verified_at=now_iso()))
    recited = code_fix(item, first, pages_by_doc, sources.get(item.source_id))
    if recited is None:
        return _set_verification(item, m.Verification(status="fail", checks=first, verified_at=now_iso()))
    new_item, before, after, note = recited
    second = checks.check_data_item(new_item, pages_by_doc, sources)
    new_item = checks.with_fixed_locators(new_item, second)
    after_status = checks.status_from(second)
    final = "fixed" if after_status == "pass" else after_status
    if final == "fixed":
        result_note = "All checks pass after the fix by code"
    else:
        bad = next((c for c in second if c.result != "pass"), None)
        result_note = (f"Still {after_status} after the fix by code: {bad.detail}" if bad else f"Still {after_status}")
        if bad is not None and bad.check == "value_in_quote":
            result_note += ". The value must be restated from the corrected quote."
    attempts = [
        m.Attempt(attempt=1, status="fail", checks=first, action="recite", before=before, after=after, note=note,
                  at=now_iso()),
        m.Attempt(attempt=2, status=final, checks=second, note=result_note, at=now_iso()),
    ]
    return _set_verification(new_item, m.Verification(status=final, checks=second, attempts=attempts,
                                                      verified_at=now_iso()))


def _set_verification(item: m.DataItem, v: m.Verification) -> m.DataItem:
    return item.model_copy(update={"verification": v})


def code_fix(item: m.DataItem, results: list[m.CheckResult], pages_by_doc: dict[str, list[str]],
             source: m.Source | None = None) -> tuple[m.DataItem, str, str, str] | None:
    """One fix toward the source: re-cite failed quotes, then restate a value the page prints plainer.
    (item, before, after, note), or None when code has nothing to fix."""
    new = item
    before: list[str] = []
    after: list[str] = []
    notes: list[str] = []
    rec = recite(item, results, pages_by_doc, source)
    if rec is not None:
        new, b, a, n = rec
        before.append(b)
        after.append(a)
        notes.append(n)
    value_failed = any(c.check == "value_in_quote" and c.result == "fail" for c in results)
    if new.value is not None and (value_failed or rec is not None):
        ev = [text.normalize(c.quote) for c in new.citations
              if c.role == "evidence" and isinstance(c.locator, m.TextLocator)]
        rs = restate_value(new, ev)
        if rs is not None:
            new, old, shown = rs
            before.append(old)
            after.append(shown)
            notes.append(f"Value restated by code as the page prints it: '{old}' to '{shown}'.")
    if not after:
        return None
    return new, " | ".join(before), " | ".join(after), " ".join(notes)


def recite(item: m.DataItem, results: list[m.CheckResult], pages_by_doc: dict[str, list[str]],
           source: m.Source | None = None) -> tuple[m.DataItem, str, str, str] | None:
    """Replace each failed quote with the real span on the page the extractor read: for the evidence,
    the whole table row when the agent quoted its label with only some of its cells, else the closest
    span when it is close enough to be the same text. Quotes never move to another page, since the
    extractor saw only its own. After the evidence moves, code picks its headers again.
    (item, before, after, note), or None when nothing could be re-cited."""
    text_idx = [i for i, c in enumerate(item.citations) if isinstance(c.locator, m.TextLocator)]
    qe = [c for c in results if c.check == "quote_exact"]
    if len(qe) != len(text_idx):
        return None
    new = item.model_copy(deep=True)
    before: list[str] = []
    after: list[str] = []
    notes: list[str] = []
    moved = False
    for i, res in zip(text_idx, qe):
        cit = new.citations[i]
        page_text = _page(pages_by_doc.get(cit.doc_id) or [], cit.locator.page)
        if res.result != "fail" or not page_text:
            continue
        row = cit.role == "evidence" and new.value is not None
        span = row_span(page_text, cit.quote, new.value, near=cit.locator.char_start) if row else None
        how = "the whole table row"
        if span is None:
            diff, near = text.near_miss(page_text, cit.quote)
            sim = similarity(diff)
            if sim >= MIN_RECITE_SIMILARITY:
                span, how = near, f"the closest real span, {round(sim * 100)}% similar"
            elif row:  # last: a row label with a column header in front of it
                span = row_span(page_text, cit.quote, new.value, near=cit.locator.char_start, tails=True)
        if span is None:
            continue
        real = page_text[span[0]:span[1]]
        if not real.strip():
            continue
        before.append(cit.quote)
        after.append(real)
        notes.append(f"{cit.role.replace('_', ' ')} on page {cit.locator.page} ({how})")
        loc = m.TextLocator(page=cit.locator.page, char_start=span[0], char_end=span[1])
        new.citations[i] = cit.model_copy(update={"locator": loc, "quote": real})
        moved = moved or cit.role == "evidence"
    if not after:
        return None
    if moved:
        new = recontext(new, pages_by_doc, source)
    note = "Quote re-cited by code from " + "; ".join(notes) + ". The source wins."
    return new, " | ".join(before), " | ".join(after), note


def recontext(item: m.DataItem, pages_by_doc: dict[str, list[str]], source: m.Source | None) -> m.DataItem:
    """Pick the unit and period headers again for a re-cited evidence quote, with the old header quotes
    as the hints (an agent header stays when it still sits above the evidence)."""
    ev = next((c for c in item.citations if c.role == "evidence" and isinstance(c.locator, m.TextLocator)), None)
    pages = pages_by_doc.get(ev.doc_id) if ev is not None else None
    if ev is None or not pages:
        return item
    old = {c.role: c.quote for c in reversed(item.citations) if isinstance(c.locator, m.TextLocator)}
    hint = ExtractedItem(statement=item.statement or "-", quote=ev.quote, unit_quote=old.get("unit_context", ""),
                         period_quote=old.get("period_context", ""))
    value = scale_from_evidence(pages, ev, item.value)
    rest = [c for c in item.citations if c.role == "evidence" and c is not ev]
    cits = [ev] + context_citations(ev.doc_id, pages, ev, hint, value, source=source) + rest
    return item.model_copy(update={"citations": cits, "value": value})


# A line that holds only table cells: numbers, notes, nil dashes and blanks.
_CELL_LINE = re.compile(r"[\s\d.,%()+\-‐‑‒–—―−]*")
_ROW_LEAD = re.compile(r"[\s\x00-\x1f•·]*")


def row_span(page_text: str, quote: str, value: m.Value, *, near: int = 0,
             tails: bool = False) -> tuple[int, int] | None:
    """The real table row the agent meant: a line that starts with the agent's row label, through the
    cell lines under it. Used only when every number the agent quoted is a cell of that row and the
    reported value is one of them, so the difference is how cells were joined, which cells were left
    out, or their order. The row nearest the agent's near-miss position wins. With tails, only the
    shorter word endings of the label are tried (see _label_forms)."""
    nq = text.normalize(quote).strip()
    label, toks = _label_and_cells(nq)
    shown = scan_numbers(_bare_reported(text.normalize(value.as_reported).strip()), include_labels=True)
    if not toks or not shown or not any(t.core == shown[0].core for t in toks):
        return None  # the reported value must be one of the quoted cells
    if len(label) < 3 or not re.search(r"[A-Za-z]{2}", label):
        return None
    want = Counter((t.number, t.pct) for t in toks)
    core = shown[0].core
    for lab in (_label_forms(label)[1:] if tails else [label]):
        best: tuple[int, tuple[int, int]] | None = None
        for start, end in _row_candidates(page_text, lab):
            cells = scan_numbers(text.normalize(page_text[start:end]), include_labels=True)
            have = Counter((t.number, t.pct) for t in cells)
            if all(have[k] >= n for k, n in want.items()) and any(t.core == core for t in cells):
                dist = abs(start - near)
                if best is None or dist < best[0]:
                    best = (dist, (start, end))
        if best is not None:
            return best[1]
    return None


_CELL_GAP = re.compile(r"[\s,‐‑‒–—―−-]*")


def _label_and_cells(nq: str) -> tuple[str, list[NumToken]]:
    """A one-line row quote split into its label and its cells: the cells are the numbers at the end with
    only spaces, commas or nil dashes between them. Numbers inside the label ("IFRS 3", "Loan (US$1,742
    million)") stay in the label."""
    toks = scan_numbers(nq, include_labels=True)
    cells: list[NumToken] = []
    end = len(nq)
    for t in reversed(toks):
        if not _CELL_GAP.fullmatch(nq[t.end:end]):
            break
        cells.insert(0, t)
        end = t.start
    return nq[:end].strip(), cells


def _label_forms(label: str) -> list[str]:
    """The agent's row label, then its shorter word endings: a model sometimes puts the column header in
    front of the row label ("Therein adjusted for ... adjustments Siemens Healthineers")."""
    words = label.split(" ")
    tails = [" ".join(words[i:]) for i in range(1, len(words))]
    return [label] + [t for t in tails if len(t) >= 5 and re.search(r"[A-Za-z]{2}", t)]


def _row_candidates(page_text: str, label: str) -> list[tuple[int, int]]:
    """(start, end) of each row whose line starts with label: the rest of that line plus the cell lines
    below it. Blank cells may follow the label, but a blank line after a cell ends the row (a footnote
    marker below a table is not a cell)."""
    lines = page_text.splitlines(keepends=True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    out: list[tuple[int, int]] = []
    for k, ln in enumerate(lines):
        lead = starts[k] + _ROW_LEAD.match(ln).end()
        window = page_text[lead:lead + 4 * len(label) + 40]
        hit = text.find_quote(window, label)
        if hit is None or hit[0] != 0:
            continue
        label_end = lead + hit[1]
        j = next((i for i in range(k, len(lines)) if starts[i + 1] > label_end), k)  # the line the label ends on
        end = starts[j] + len(lines[j].rstrip())
        cells = bool(scan_numbers(text.normalize(page_text[label_end:end]), include_labels=True))
        for i in range(j + 1, len(lines)):
            body = lines[i].rstrip("\r\n")
            if not _CELL_LINE.fullmatch(body) or (cells and not body.strip()):
                break
            if body.strip():
                end = starts[i] + len(body.rstrip())
                cells = True
        out.append((lead, end))
    return out


def restate_value(item: m.DataItem, ev_quotes: list[str]) -> tuple[m.DataItem, str, str] | None:
    """as_reported (and the number) as the evidence prints it, when the agent's version differs only by
    spacing, a qualifier it added ("~37", ">31") or a sign or brackets the page does not print ("(4.2%)"
    for "decline of 4.2%": the direction belongs in the words). Never adds a sign the agent left out, so
    a dropped minus still fails. (item, old as_reported, new as_reported), or None."""
    v = item.value
    if v is None:
        return None
    shown = text.normalize(v.as_reported).strip()
    toks = scan_numbers(_bare_reported(shown), include_labels=True)
    if not toks:
        return None
    mt = toks[0]
    printed = [(t, q) for q in ev_quotes for t in scan_numbers(q, include_labels=True)
               if t.core == mt.core and t.pct == mt.pct]
    pick = [(t, q) for t, q in printed if t.number == mt.number]
    if not pick and mt.number < 0:
        pick = [(t, q) for t, q in printed if t.number > 0]
    forms = {_printed_form(t, q) for t, q in pick}
    if len(forms) != 1:
        return None
    form = forms.pop()
    number = parse_number(form)
    if form == shown or number is None:
        return None
    new_v = v.model_copy(update={"as_reported": form, "number": float(number)})
    return item.model_copy(update={"value": new_v}), v.as_reported, form


def _printed_form(t: NumToken, q: str) -> str:
    """The number as printed, sign and brackets included, without currency or scale words."""
    head = _CURRENCY_MARK.sub("", q[t.start:t.core_end])
    paren = q[t.start:t.start + 1] == "(" and q[t.end - 1:t.end] == ")"
    return head + ("%" if t.pct else "") + (")" if paren else "")


def similarity(diff: list[m.DiffSegment]) -> float:
    """difflib-style ratio from diff segments: 2 * matching characters / all characters on both sides."""
    same = sum(len(d.agent) for d in diff if d.op == "equal")
    total = sum(len(d.agent) + len(d.source) for d in diff)
    return 2 * same / total if total else 0.0


def item_certainty(item: m.DataItem, source: m.Source | None) -> m.Certainty | None:
    """contract/certainty.py over this one item. Never asked of a model."""
    if source is None:
        return None
    res = certainty.item_certainties([item.model_dump(mode="json")], [source.model_dump(mode="json")])
    return m.Certainty.model_validate(res[item.id])


def register(ctx: RunContext, item: m.DataItem) -> None:
    ctx.state.items[item.id] = item
    ctx.emit("item.created", m.ItemCreatedData(item=item), stage=STAGE, agent_id=item.agent_id,
             message=f"{item.id} created: {_short(item.statement)}")


def record_verified(ctx: RunContext, item: m.DataItem) -> m.DataItem:
    """Certainty, then store and emit item.verified."""
    item = item.model_copy(update={"certainty": item_certainty(item, ctx.state.sources_by_id().get(item.source_id))})
    ctx.state.items[item.id] = item
    v = item.verification
    bad = next((c for c in v.checks if c.result != "pass"), None)
    tail = f" ({bad.check}: {_short(bad.detail, 120)})" if bad is not None and v.status != "pass" else ""
    level = "info" if v.status in ("pass", "fixed") else "warn"
    ctx.emit("item.verified", m.ItemVerifiedData(item_id=item.id, verification=v, certainty=item.certainty),
             stage=STAGE, agent_id=item.agent_id, level=level, message=f"{item.id} {v.status}{tail}")
    return item


def apply_tier_rules(ctx: RunContext) -> set[str]:
    """tier_rule on the tier 2 and tier 3 data items, now that every priority item is in. Returns the
    source ids whose items changed."""
    state = ctx.state
    changed: set[str] = set()
    results = checks.check_tier_rules(state.items, state.sources_by_id())
    for iid, res in results.items():
        it = state.items.get(iid)
        if not isinstance(it, m.DataItem) or it.verification.status == "pending":
            continue
        new_checks = [c for c in it.verification.checks if c.check != "tier_rule"] + list(res)
        status = checks.status_from(new_checks)
        if status == "pass" and it.verification.status == "fixed":
            status = "fixed"
        v = it.verification.model_copy(update={"status": status, "checks": new_checks, "verified_at": now_iso()})
        record_verified(ctx, it.model_copy(update={"verification": v}))
        changed.add(it.source_id)
    return changed


# ---------------------------------------------------------------------------
# JSON sources: SEC companyfacts read by code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Concept:
    """One ROTIC input and the XBRL tags that carry it, most specific first."""
    label: str
    instant: bool
    us_gaap: tuple[str, ...]
    ifrs: tuple[str, ...]


PEER_CONCEPTS: tuple[Concept, ...] = (
    Concept("operating income", False, ("OperatingIncomeLoss",), ("ProfitLossFromOperatingActivities",)),
    Concept("amortization of intangible assets", False, ("AmortizationOfIntangibleAssets",),
            ("AmortisationIntangibleAssetsOtherThanGoodwill",)),
    Concept("income before income taxes", False,
            ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
            ("ProfitLossBeforeTax",)),
    Concept("income tax expense", False, ("IncomeTaxExpenseBenefit",), ("IncomeTaxExpenseContinuingOperations",)),
    Concept("total assets", True, ("Assets",), ("Assets",)),
    Concept("cash and cash equivalents", True, ("CashAndCashEquivalentsAtCarryingValue",), ("CashAndCashEquivalents",)),
    Concept("goodwill", True, ("Goodwill",), ("Goodwill",)),
    Concept("total current liabilities", True, ("LiabilitiesCurrent",), ("CurrentLiabilities",)),
    Concept("short-term debt and current maturities of long-term debt", True,
            ("ShortTermBorrowings", "DebtCurrent", "LongTermDebtCurrent"),
            ("CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "ShorttermBorrowings")),
)
_TAXONOMY_FIELD = {"us-gaap": "us_gaap", "ifrs-full": "ifrs"}
_ANNUAL_FORMS = ("10-K", "20-F", "40-F")


@dataclass(frozen=True)
class Fact:
    concept: Concept
    taxonomy: str
    tag: str
    unit: str
    index: int
    val: int | float
    end: date
    form: str

    @property
    def pointer(self) -> str:
        return "/" + "/".join(_esc(p) for p in ("facts", self.taxonomy, self.tag, "units", self.unit,
                                                 str(self.index), "val"))


def _esc(token: str) -> str:
    """RFC 6901 escaping of one pointer token."""
    return token.replace("~", "~0").replace("/", "~1")


def _iso(s: Any) -> date | None:
    if not isinstance(s, str):
        return None
    mm = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s.strip())
    if not mm:
        return None
    try:
        return date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
    except ValueError:
        return None


def annual_rows(rows: list[dict], instant: bool) -> dict[date, tuple[int, dict]]:
    """One fact per period end from annual reports: full-year durations for flows, instants for balances.
    When several filings report the same period, the latest filing wins (restatements included)."""
    best: dict[date, tuple[int, dict]] = {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not str(r.get("form", "")).startswith(_ANNUAL_FORMS) or r.get("fp") != "FY":
            continue
        val = r.get("val")
        end, start = _iso(r.get("end")), _iso(r.get("start"))
        if end is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if instant != (start is None):
            continue
        if not instant and not 330 <= (end - start).days <= 400:
            continue
        prev = best.get(end)
        if prev is None or (str(r.get("filed", "")), i) > (str(prev[1].get("filed", "")), prev[0]):
            best[end] = (i, r)
    return best


def peer_facts(doc: Any, first_year: int = PEER_FIRST_YEAR, concepts: tuple[Concept, ...] = PEER_CONCEPTS) -> list[Fact]:
    """The ROTIC inputs in a companyfacts document, one per concept and period end, oldest first."""
    facts = doc.get("facts") if isinstance(doc, dict) else None
    if not isinstance(facts, dict):
        return []
    out: list[Fact] = []
    for concept in concepts:
        out += _concept_facts(facts, concept, first_year)
    return out


def _concept_facts(facts: dict, concept: Concept, first_year: int) -> list[Fact]:
    """The first tag of the concept that has annual facts in the window."""
    lo = first_year - 1 if concept.instant else first_year
    for taxonomy, attr in _TAXONOMY_FIELD.items():
        block = facts.get(taxonomy)
        if not isinstance(block, dict):
            continue
        for tag in getattr(concept, attr):
            units = (block.get(tag) or {}).get("units") if isinstance(block.get(tag), dict) else None
            if not isinstance(units, dict):
                continue
            for unit, rows in units.items():
                if not re.fullmatch(r"[A-Z]{3}", unit) or not isinstance(rows, list):
                    continue  # monetary units only (USD, EUR)
                picked = [(end, i, r) for end, (i, r) in annual_rows(rows, concept.instant).items() if end.year >= lo]
                if picked:
                    picked.sort(key=lambda t: t[0])
                    return [Fact(concept, taxonomy, tag, unit, i, r["val"], end, str(r.get("form")))
                            for end, i, r in picked[-PEER_MAX_PERIODS:]]
    return []


def entity_name(source: m.Source, doc: Any) -> str:
    """'SEC XBRL company facts: GE HealthCare (peer)' gives 'GE HealthCare'. Falls back to entityName."""
    title = source.title.split(":", 1)[-1]
    title = re.sub(r"\([^)]*\)", "", title).strip()
    if title:
        return title
    name = doc.get("entityName") if isinstance(doc, dict) else None
    return str(name) if name else source.source_id


def millions(val: int | float) -> str:
    """Display in millions with thousands separators, exact up to three decimals, rounded half up beyond."""
    d = Decimal(repr(val)) if isinstance(val, float) else Decimal(val)
    x = d / SCALES["million"]
    places = max(0, -x.normalize().as_tuple().exponent)
    s = half_up(x, min(places, 3))
    sign, s = ("-", s[1:]) if s.startswith("-") else ("", s)
    whole, _, frac = s.partition(".")
    return f"{sign}{int(whole):,}" + (f".{frac}" if frac else "")


def fact_statement(name: str, f: Fact) -> str:
    when = f"{f.end:%b} {f.end.day}, {f.end.year}"
    amount = f"{f.unit} {millions(f.val)} million"
    where = f"({f.taxonomy} {f.tag}, {f.form})"
    if f.concept.instant:
        return f"{name} {f.concept.label} at {when} were {amount} {where}."
    return f"{name} {f.concept.label} for FY{f.end.year} (year to {when}) was {amount} {where}."


def build_code_item(ctx: RunContext, source: m.Source, doc_id: str, f: Fact, name: str,
                    agent_id: str) -> m.DataItem:
    need_ids = list(source.need_ids[:1])
    criteria, category = criteria_for(ctx.state, need_ids, source)
    period = f.end.isoformat() if f.concept.instant else f"FY{f.end.year}"
    value = m.Value(number=float(f.val), as_reported=str(f.val), unit=f.unit, scale="one", period=period)
    cit = m.Citation(doc_id=doc_id, locator=m.JsonLocator(pointer=f.pointer), quote=str(f.val), role="evidence")
    return m.DataItem(
        id=ctx.next_id("D"), category=category, criteria=criteria, need_id=need_ids[0] if need_ids else None,
        statement=fact_statement(name, f), value=value, citations=[cit], source_id=source.source_id,
        source_tier=source.tier, extracted_by="code", agent_id=agent_id,
        verification=m.Verification(status="pending"),
    )


async def extract_json(ctx: RunContext, job: JsonJob, pages_by_doc: dict[str, list[str]]) -> list[str]:
    """Code-only extraction from a stored JSON document. Returns the new item ids."""
    runner = job.runner or AgentRunner(ctx, "extractor", f"Read {job.source_id} structured facts by code (no model)",
                                       [job.source_id], None)
    try:
        ctx.check_stop()
        source = ctx.state.sources_by_id()[job.source_id]
        pages = ctx.state.agent_pages(job.doc_id)
        try:
            doc = await asyncio.to_thread(json.loads, "".join(pages))
        except ValueError as e:
            log(ctx, runner, f"{job.doc_id} is not valid JSON ({e}), no items read", level="warn")
            runner.finish("failed")
            return []
        facts = peer_facts(doc)
        if not facts:
            log(ctx, runner, f"{job.doc_id}: no known XBRL concepts found, no items read", level="warn")
            runner.finish("done")
            return []
        name = entity_name(source, doc)
        items = [build_code_item(ctx, source, job.doc_id, f, name, runner.agent_id) for f in facts]
        for it in items:
            register(ctx, it)
        sources = ctx.state.sources_by_id()
        verified = await asyncio.to_thread(lambda: [verify_item(it, pages_by_doc, sources) for it in items])
        for it in verified:
            record_verified(ctx, it)
    except StopRequested:
        runner.finish("stopped")
        raise
    except BaseException:
        runner.finish("failed")
        raise
    tags = sorted({f.tag for f in facts})
    log(ctx, runner, f"{job.source_id}: {len(items)} fact(s) read by code from {len(tags)} XBRL tag(s)")
    runner.finish("done")
    return [it.id for it in items]


# ---------------------------------------------------------------------------
# Sources and events
# ---------------------------------------------------------------------------


def source_counts(state: RunState, source_id: str) -> tuple[int, int]:
    items = [i for i in state.data_items() if i.source_id == source_id]
    return len(items), sum(i.verification.status in ("pass", "fixed") for i in items)


def set_source_status(ctx: RunContext, source: m.Source, status: str) -> None:
    n, ok = source_counts(ctx.state, source.source_id)
    source.status = status
    source.stats = source.stats.model_copy(update={"data_items": n, "data_items_verified": ok})
    note = f": {ok} of {n} item(s) verified" if status == "extracted" else ""
    ctx.emit("source.status", m.SourceStatusData(source_id=source.source_id, status=status,
                                                 stats=source.stats.model_copy(), error=source.error),
             stage=STAGE, message=f"{source.source_id} {status}{note}")


def log(ctx: RunContext, runner: AgentRunner, text_: str, *, level: str = "info") -> None:
    ctx.emit("agent.log", m.AgentLogData(text=text_, transcript_path=runner.agent.transcript_path),
             stage=STAGE, agent_id=runner.agent_id, level=level, message=f"{runner.agent_id}: {text_}")


def _short(s: str, n: int = 90) -> str:
    s = text.normalize(s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."
