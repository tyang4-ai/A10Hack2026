"""Stage extract: one small-model extractor per page, offsets by code, data checks right away, code extraction for JSON.

Text sources: every page the plan lists (extract_pages, or the whole document for HTML pseudo-pages) gets
its own extractor agent. The agent returns quotes, never offsets: code locates each quote in the stored
page with verify.text, assigns the D- id, emits item.created (pending), runs the data checks, re-cites a
near miss from the closest real span (a `recite` attempt, the source wins), then emits item.verified with
the certainty from contract/certainty.py.

JSON sources (SEC companyfacts): code reads the needed concepts by JSON pointer, no model, extracted_by
"code". Quarantined documents never reach an extractor (RunState.agent_pages is empty for them).

After every page is done, the tier rule runs over the tier 2 and tier 3 data items, because it compares
them with the priority items.
"""
from __future__ import annotations

import asyncio
import json
import re
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
from ..verify.numbers import SCALES, half_up, parse_number, period_key, period_mentions, scan_numbers

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
    cits = [ev] + context_citations(job.doc_id, pages, ev, raw, value)
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


def context_citations(doc_id: str, pages: list[str], ev: m.Citation, raw: ExtractedItem,
                      value: m.Value | None) -> list[m.Citation]:
    """unit_context and period_context citations, always from the evidence's own page.

    The agent's header line is used when it is on that page (and, for the period, names a year).
    Otherwise code takes the nearest header above the evidence on the same page. A table row needs
    both or its scale and column cannot be checked. A percentage needs no scale header, and a quote
    that names its own scale or year needs no header either.
    """
    page = ev.locator.page
    page_text = pages[page - 1] if 1 <= page <= len(pages) else ""
    at = ev.locator.char_start
    out: list[m.Citation] = []
    pct = value is not None and value.unit == "%"
    ev_norm = text.normalize(ev.quote)
    if not pct:
        needs_unit = value is not None and value.scale != "one" and not any(t.word for t in scan_numbers(ev_norm))
        uc = _agent_header(doc_id, page_text, page, raw.unit_quote, "unit_context", at,
                           lambda q: bool(_NAMES_UNIT.search(q)))
        if uc is None and needs_unit:
            uc = _header_above(doc_id, page_text, page, at, "unit_context", _unit_spans(page_text))
        if uc is None and needs_unit and raw.unit_quote.strip():
            uc = cite_on_page(doc_id, pages, page, raw.unit_quote, "unit_context")  # fails with a diff
        if uc is not None:
            out.append(uc)
    needs_period = bool(value is not None and value.period and not _names_period(ev_norm, value.period))
    pc = _agent_header(doc_id, page_text, page, raw.period_quote, "period_context", at,
                       lambda q: bool(period_mentions(text.normalize(q))))
    if pc is None and needs_period:
        pc = _header_above(doc_id, page_text, page, at, "period_context", _year_spans(page_text))
    if pc is None and needs_period and raw.period_quote.strip():
        pc = cite_on_page(doc_id, pages, page, raw.period_quote, "period_context")  # fails with a diff
    if pc is not None:
        out.append(pc)
    return out


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
    """Runs of two or more years (one cell per line), and whole lines that name two or more periods and
    hold no amounts ("Fiscal year Note 2025 2024", "Sep 30, 2025 Sep 30, 2024")."""
    spans = [mm.span() for mm in _YEAR_RUN.finditer(page_text)]
    pos = 0
    for line in page_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if (len({y for y, _, _ in period_mentions(body)}) >= 2 and not re.search(r"\d,\d{3}|\d\.\d|%", body)
                and body.strip()):
            lead = len(body) - len(body.lstrip())
            spans.append((pos + lead, pos + len(body.rstrip())))
        pos += len(line)
    return spans


def _names_period(quote_norm: str, period: str) -> bool:
    """True when the quote itself names every year of the period, so no header is needed."""
    years, _ = period_key(period)
    found = {y for y, _, _ in period_mentions(quote_norm)}
    return bool(years) and years <= found


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
    if tok.word in ("thousand", "million", "billion"):
        scale = tok.word  # "€2.2 billion": the scale is part of what was reported
    unit = "%" if pct else unit_code(raw.unit, tok.currency)
    return m.Value(number=float(number), as_reported=shown, unit=unit, scale="one" if pct else scale,
                   period=raw.period.strip() or None)


_UNIT_CODES = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "usd": "USD", "us$": "USD",
               "dollar": "USD", "dollars": "USD", "£": "GBP", "gbp": "GBP", "chf": "CHF"}


def unit_code(unit: str, currency: str | None) -> str:
    u = unit.strip()
    if not u:
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
    """Run the data checks. A near-miss quote is re-cited once from the closest real span and checked
    again (status fixed when that passes). Pure: returns a new item. Runs in a worker thread."""
    first = checks.check_data_item(item, pages_by_doc, sources)
    item = checks.with_fixed_locators(item, first)
    status = checks.status_from(first)
    if status != "fail":
        return _set_verification(item, m.Verification(status=status, checks=first, verified_at=now_iso()))
    recited = recite(item, first, pages_by_doc)
    if recited is None:
        return _set_verification(item, m.Verification(status="fail", checks=first, verified_at=now_iso()))
    new_item, before, after, note = recited
    second = checks.check_data_item(new_item, pages_by_doc, sources)
    new_item = checks.with_fixed_locators(new_item, second)
    after_status = checks.status_from(second)
    final = "fixed" if after_status == "pass" else after_status
    if final == "fixed":
        result_note = "All checks pass after re-citing"
    else:
        bad = next((c for c in second if c.result != "pass"), None)
        result_note = (f"Still {after_status} after re-citing: {bad.detail}" if bad else f"Still {after_status}")
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


def recite(item: m.DataItem, results: list[m.CheckResult],
           pages_by_doc: dict[str, list[str]]) -> tuple[m.DataItem, str, str, str] | None:
    """Replace each failed quote with the real source span its quote_exact check found, when the span is
    close enough to be the same text. (item, before, after, note), or None when nothing could be re-cited."""
    text_idx = [i for i, c in enumerate(item.citations) if isinstance(c.locator, m.TextLocator)]
    qe = [c for c in results if c.check == "quote_exact"]
    if len(qe) != len(text_idx):
        return None
    new = item.model_copy(deep=True)
    before: list[str] = []
    after: list[str] = []
    notes: list[str] = []
    for i, res in zip(text_idx, qe):
        loc = res.closest_locator
        if res.result != "fail" or loc is None or not res.diff:
            continue
        sim = similarity(res.diff)
        cit = new.citations[i]
        pages = pages_by_doc.get(cit.doc_id) or []
        if sim < MIN_RECITE_SIMILARITY or not 1 <= loc.page <= len(pages):
            continue
        real = pages[loc.page - 1][loc.char_start:loc.char_end]
        if not real.strip():
            continue
        before.append(cit.quote)
        after.append(real)
        notes.append(f"{cit.role.replace('_', ' ')} on page {loc.page} ({round(sim * 100)}% similar)")
        new.citations[i] = cit.model_copy(update={"locator": loc, "quote": real})
    if not after:
        return None
    note = ("Quote re-cited by code from the closest real span: " + "; ".join(notes) +
            ". The source wins.")
    return new, " | ".join(before), " | ".join(after), note


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
