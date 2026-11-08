"""Demo controls (label them as demo in the UI): planted error injection and analyst edits, both re-verified by code.

inject_errors is the "watch the checker catch it" control. It takes data items whose code checks pass right
now, changes one digit in each evidence quote (a digit slip that appears nowhere in the stored source), and
re-runs the same code checks the pipeline uses: quote_exact fails with a diff. Then it re-cites each quote
from the closest real span of the stored page, the way the repair stage does, and the item ends `fixed`.
Every event message and attempt note says DEMO, so nobody mistakes it for a real extraction error.

patch_item applies an analyst's text edit, re-runs the code checks, and re-checks the items that cite it.
Certainty (contract/certainty.py) is recomputed for the changed item and every item that cites it, because
it is inherited along citations.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .contract import certainty, m
from .ids import now_iso
from .verify import checks, text

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

REPAIR_DELAY_S = 2.0        # seconds between "caught" and "re-cited", so the red row is visible. 0 = inline
DEMO = "DEMO"
INJECTION_NOTE = ("DEMO INJECTION (demo control, not a real extraction error): one digit of the quote was "
                  "changed on purpose. Code caught it and re-cited the exact span from the stored source.")
STALE_PREFIX = "From before the analyst edit (the judges have not re-read the new text): "
_DIGITS = "0123456789"
_PASSING = ("pass", "fixed")
_background: set[asyncio.Task] = set()      # delayed repairs, kept so they are not garbage collected


def _api_error(status: int, code: str, message: str) -> Exception:
    """The API's error envelope. Imported late: api.py imports this module inside its handlers."""
    from .api import ApiError

    return ApiError(status, code, message)


# ---------------------------------------------------------------------------
# Shared: code checks, certainty, persistence
# ---------------------------------------------------------------------------


def _pages(state: RunState) -> dict[str, list[str]]:
    """Page texts the checkers may read. Quarantined documents are left out (fail closed)."""
    return {d.doc_id: state.agent_pages(d.doc_id) for d in state.documents if not d.quarantined}


def _code_checks(item: m.Item, items: dict[str, m.Item], sources: dict[str, m.Source],
                 pages: dict[str, list[str]]) -> list[m.CheckResult]:
    """The same code checks the pipeline runs for this kind, plus the tier rule when it applies."""
    out = checks.check_item(item, items=items, sources=sources, pages_by_doc=pages)
    return out + checks.check_tier_rule(item, items, sources)


def _judge_checks(item: m.Item, *, stale: bool) -> list[m.CheckResult]:
    """Judge results are kept: code cannot re-run them. After an edit they are marked as older than the text."""
    kept = [c for c in item.verification.checks if c.method == "llm_judge"]
    if not stale:
        return kept
    return [c if c.detail.startswith(STALE_PREFIX) else c.model_copy(update={"detail": STALE_PREFIX + c.detail})
            for c in kept]


def _with_verification(item: m.Item, status: str, results: list[m.CheckResult],
                       attempt: m.Attempt | None = None) -> m.Item:
    new = item.model_copy(deep=True)
    attempts = list(new.verification.attempts) + ([attempt] if attempt is not None else [])
    new.verification = m.Verification(status=status, checks=results, attempts=attempts,
                                      verified_at=None if status == "pending" else now_iso())
    new.certainty = None
    return new


def _no_certainty(status: str) -> m.Certainty:
    return m.Certainty(score=None, band="none", strength=0.0, support=0.0, inherited=0.0, completeness=1.0,
                       weakest_ref=None, basis=f"Verification {status}")


def _certainties(state: RunState) -> dict[str, m.Certainty]:
    """contract/certainty.py over the whole report. Empty when the report is inconsistent (logged)."""
    items = [i.model_dump(mode="json") for i in state.items.values()]
    sources = [s.model_dump(mode="json") for s in state.sources]
    try:
        raw = certainty.item_certainties(items, sources)
    except (KeyError, TypeError, ZeroDivisionError):
        log.exception("certainty could not be computed for run %s", state.run.run_id)
        return {}
    return {iid: m.Certainty.model_validate(c) for iid, c in raw.items()}


def _verified(ctx: RunContext, item: m.Item, *, message: str, level: str = "info") -> m.Item:
    """Put the item in the state with its recomputed certainty, then emit item.verified."""
    state = ctx.state
    state.items[item.id] = item
    cert = _certainties(state).get(item.id)
    if cert is None and item.verification.status not in _PASSING:
        cert = _no_certainty(item.verification.status)
    item.certainty = cert
    ctx.emit("item.verified", {"item_id": item.id, "verification": item.verification, "certainty": cert},
             message=message, level=level)
    return item


def _sync_certainties(ctx: RunContext, changed_id: str) -> None:
    """Certainty is inherited, so a change to one item can move the items that cite it. Emit those."""
    state = ctx.state
    certs = _certainties(state)
    for iid in dependents(state.items, changed_id):
        it, cert = state.items.get(iid), certs.get(iid)
        if it is None or cert is None or it.verification.status == "pending":
            continue
        if it.certainty is not None and it.certainty.model_dump() == cert.model_dump():
            continue
        old = it.certainty.score if it.certainty is not None else None
        it.certainty = cert
        what = f"certainty {old} -> {cert.score}" if old != cert.score else "certainty basis updated"
        ctx.emit("item.verified", {"item_id": iid, "verification": it.verification, "certainty": cert},
                 message=f"{iid} {what} after {changed_id} changed",
                 level="info" if (cert.score or 0) >= (old or 0) else "warn")


async def _save(ctx: RunContext) -> None:
    """Live runs are saved. A replay stays as recorded on disk: demo changes to it live in memory only."""
    if ctx.state.run.mode == "live":
        await ctx.store.save_async(ctx.state)


def _refs(item: m.Item) -> list[str]:
    if item.kind == "conclusion":
        return list(item.data_ids)
    if item.kind == "extrapolation":
        return list(item.conclusion_ids)
    if item.kind == "chart":
        return [r.split(":")[0] for r in item.refs]
    return []


def dependents(items: dict[str, m.Item], item_id: str) -> list[str]:
    """Items that cite item_id directly or through others, nearest first (so D, then C, then E and G)."""
    found: list[str] = []
    frontier = {item_id}
    while frontier:
        hits = [it.id for it in items.values()
                if it.id != item_id and it.id not in found and frontier & set(_refs(it))]
        found += hits
        frontier = set(hits)
    return found


# ---------------------------------------------------------------------------
# inject_errors
# ---------------------------------------------------------------------------


@dataclass
class _Injection:
    item_id: str
    k: int                           # index of the corrupted citation
    original: m.DataItem
    bad: m.DataItem                  # corrupted quote, verification pending
    failed: m.DataItem               # corrupted quote, the checks that caught it
    fixed: m.DataItem | None         # re-cited from the stored source, verification fixed
    before: str                      # the quote as it was
    after: str                       # the corrupted quote
    caught: bool
    detail: str


def _evidence_quotes(item: m.DataItem) -> list[int]:
    """Indexes of text evidence citations whose quote has a digit."""
    return [k for k, c in enumerate(item.citations)
            if c.role == "evidence" and isinstance(c.locator, m.TextLocator) and any(ch in _DIGITS for ch in c.quote)]


def _candidates(items: dict[str, m.Item], pages: dict[str, list[str]]) -> list[m.DataItem]:
    """Verified data items with a numeric text quote and readable pages. Plain passes first, then by id."""
    out = [it for it in items.values()
           if it.kind == "data" and it.verification.status in _PASSING
           and any(pages.get(it.citations[k].doc_id) for k in _evidence_quotes(it))]
    return sorted(out, key=lambda it: (it.verification.status != "pass", it.id))


def digit_slips(quote: str, as_reported: str | None) -> Iterator[str]:
    """One-digit changes of the quote. The reported value's digits come first, last digit first (a subtle slip)."""
    positions = [i for i, ch in enumerate(quote) if ch in _DIGITS]
    start = quote.find(as_reported) if as_reported else -1
    in_value = [i for i in positions if start >= 0 and start <= i < start + len(as_reported or "")]
    order = in_value[::-1] + [i for i in reversed(positions) if i not in in_value]
    for i in order:
        d = int(quote[i])
        for step in (1, 2, 9):
            yield quote[:i] + str((d + step) % 10) + quote[i + 1:]


def _corrupt(item: m.DataItem, pages: dict[str, list[str]]) -> tuple[int, str] | None:
    """(citation index, corrupted quote) for a slip that appears nowhere in the stored document."""
    for k in _evidence_quotes(item):
        cit = item.citations[k]
        doc_pages = pages.get(cit.doc_id)
        if not doc_pages:
            continue
        for bad in digit_slips(cit.quote, item.value.as_reported if item.value else None):
            if text.locate(doc_pages, bad, cit.locator.page) is None:
                return k, bad
    return None


def _with_quote(item: m.DataItem, k: int, quote: str, locator: m.TextLocator | None = None) -> m.DataItem:
    new = item.model_copy(deep=True)
    update: dict = {"quote": quote}
    if locator is not None:
        update["locator"] = locator
    new.citations[k] = new.citations[k].model_copy(update=update)
    return new


def _repair(inj_bad: m.DataItem, original: m.DataItem, k: int, failed_checks: list[m.CheckResult],
            items: dict, sources: dict, pages: dict) -> tuple[m.DataItem, list[m.CheckResult], str] | None:
    """Re-cite from the closest real span the checker found. If that span does not pass every check,
    fall back to the quote as it was verified before the injection."""
    text_idx = [i for i, c in enumerate(inj_bad.citations) if isinstance(c.locator, m.TextLocator)]
    qe = [c for c in failed_checks if c.check == "quote_exact"]
    tries: list[tuple[m.DataItem, str]] = []
    if len(qe) == len(text_idx) and k in text_idx:
        res = qe[text_idx.index(k)]
        loc = res.closest_locator
        doc_pages = pages.get(inj_bad.citations[k].doc_id) or []
        if res.result == "fail" and loc is not None and 1 <= loc.page <= len(doc_pages):
            real = doc_pages[loc.page - 1][loc.char_start:loc.char_end]
            tries.append((_with_quote(inj_bad, k, real, loc), "the closest span the checker found"))
    tries.append((_with_quote(inj_bad, k, original.citations[k].quote, original.citations[k].locator),
                  "the span verified before the injection"))
    for candidate, how in tries:
        results = _code_checks(candidate, items, sources, pages) + _judge_checks(original, stale=False)
        if checks.status_from(results) == "pass":
            return checks.with_fixed_locators(candidate, results), results, how
    return None


def _plan_one(item: m.DataItem, items: dict, sources: dict, pages: dict) -> _Injection | None:
    """Pure: check the item passes now, corrupt one digit, re-check, and work out the repair."""
    now_checks = _code_checks(item, items, sources, pages) + _judge_checks(item, stale=False)
    if checks.status_from(now_checks) != "pass":
        return None                       # only items that pass today, so a failure is the injection's doing
    slip = _corrupt(item, pages)
    if slip is None:
        return None
    k, bad_quote = slip
    bad = _with_verification(_with_quote(item, k, bad_quote), "pending", [])
    bad_checks = _code_checks(bad, items, sources, pages) + _judge_checks(item, stale=False)
    status = checks.status_from(bad_checks)
    failed = _with_verification(bad, status, bad_checks)
    fails = [c for c in bad_checks if c.result != "pass"]
    detail = "; ".join(f"{c.check}: {c.detail}" for c in fails[:2]) or "every check passed"
    fixed = None
    if status == "fail":
        rep = _repair(bad, item, k, bad_checks, items, sources, pages)
        if rep is not None:
            rep_item, rep_checks, how = rep
            attempt = m.Attempt(attempt=len(item.verification.attempts) + 1, status="fail", checks=bad_checks,
                                action="recite", before=bad_quote, after=rep_item.citations[k].quote,
                                note=f"{INJECTION_NOTE} Re-cited from {how}.", at=now_iso())
            fixed = _with_verification(rep_item, "fixed", rep_checks, attempt)
    return _Injection(item_id=item.id, k=k, original=item, bad=bad, failed=failed, fixed=fixed,
                      before=item.citations[k].quote, after=bad_quote, caught=status == "fail", detail=detail)


def _plan(items: dict[str, m.Item], sources: dict[str, m.Source], pages: dict[str, list[str]],
          count: int) -> list[_Injection]:
    """Pure, over snapshots taken on the event loop, so it can run in a worker thread while the API serves."""
    out: list[_Injection] = []
    for item in _candidates(items, pages):
        if len(out) >= count:
            break
        inj = _plan_one(item, items, sources, pages)
        if inj is not None:
            out.append(inj)
    return out


def _slip_text(before: str, after: str) -> str:
    """'22,363' -> '22,364': the changed digit with a little context, for the event message."""
    i = next(n for n, (a, b) in enumerate(zip(before, after)) if a != b)
    lo, hi = max(0, i - 6), min(len(before), i + 7)
    return f"'{before[lo:hi]}' -> '{after[lo:hi]}'"


def _apply_injection(ctx: RunContext, inj: _Injection) -> None:
    state = ctx.state
    iid = inj.item_id
    state.items[iid] = inj.bad
    ctx.emit("item.updated", {"item": inj.bad, "reason": "demo_injection"}, level="warn",
             message=f"{DEMO}: changed one digit in {iid}'s quote on purpose ({_slip_text(inj.before, inj.after)}). "
                     f"Re-checking by code.")
    _verified(ctx, inj.failed, level="warn",
              message=f"{DEMO}: {iid} {inj.failed.verification.status.upper()}, caught by code. {inj.detail}")
    _sync_certainties(ctx, iid)


def _apply_repair(ctx: RunContext, inj: _Injection) -> bool:
    """Swap in the re-cited item, unless the run stopped or something else changed the item meanwhile."""
    state = ctx.state
    if inj.fixed is None or ctx.stopped() or state.items.get(inj.item_id) is not inj.failed:
        return False
    iid, fixed = inj.item_id, inj.fixed
    state.items[iid] = fixed
    cit = fixed.citations[inj.k]
    ctx.emit("item.updated", {"item": fixed, "reason": "repair"},
             message=f"{DEMO}: {iid} re-cited from the stored source ({cit.doc_id} page {cit.locator.page})")
    _verified(ctx, fixed, message=f"{DEMO}: {iid} fixed: every code check passes again")
    _sync_certainties(ctx, iid)
    return True


async def _repair_later(ctx: RunContext, plan: list[_Injection], delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        for inj in plan:
            _apply_repair(ctx, inj)
        await _save(ctx)
    except Exception:
        log.exception("demo repair failed for run %s", ctx.run_id)


def _note(plan: list[_Injection], asked: int, delay: float) -> str:
    n = len(plan)
    caught = sum(i.caught for i in plan)
    fixable = sum(i.fixed is not None for i in plan)
    parts = [f"Demo control (label it in the UI): changed one digit in {n} verified quote(s) on purpose.",
             f"Code caught {caught} of {n}: quote_exact failed, with a diff against the stored source."]
    if fixable and delay > 0:
        parts.append(f"In {delay:g} s, {fixable} will be re-cited from the stored source and end as fixed.")
    elif fixable:
        parts.append(f"{fixable} re-cited from the stored source and now fixed.")
    if n < asked:
        parts.append(f"Only {n} of the {asked} requested items had a verified numeric text quote.")
    missed = [i.item_id for i in plan if not i.caught]
    if missed:
        parts.append(f"Not caught: {', '.join(missed)} (left unchanged).")
    return " ".join(parts)


async def inject_errors(ctx: RunContext, req: m.InjectErrorsRequest, *,
                        repair_delay: float | None = None) -> m.InjectErrorsResponse:
    """Corrupt one digit in req.count passing quotes, re-run the checks, emit item.updated (demo_injection),
    then re-cite each from the stored source (now, or after repair_delay seconds)."""
    if ctx.stopped():
        raise _api_error(409, "invalid_state", "Run is stopped (kill switch)")
    delay = REPAIR_DELAY_S if repair_delay is None else max(0.0, repair_delay)
    state = ctx.state
    plan = await asyncio.to_thread(_plan, dict(state.items), state.sources_by_id(), _pages(state), req.count)
    if not plan:
        raise _api_error(409, "invalid_state",
                         "No data item has a verified numeric text quote yet. Wait for the extract stage.")
    applied: list[_Injection] = []
    for inj in plan:
        if not inj.caught:
            continue                      # never leave a corrupted quote in the report unnoticed
        if ctx.state.items.get(inj.item_id) is not inj.original:
            continue                      # the pipeline changed it while the checks ran
        _apply_injection(ctx, inj)
        applied.append(inj)
    if delay <= 0:
        for inj in applied:
            _apply_repair(ctx, inj)
    elif applied:
        task = asyncio.get_running_loop().create_task(_repair_later(ctx, applied, delay),
                                                      name=f"demo-repair-{ctx.run_id}")
        _background.add(task)
        task.add_done_callback(_background.discard)
    await _save(ctx)
    return m.InjectErrorsResponse(item_ids=[i.item_id for i in applied], note=_note(plan, req.count, delay))


# ---------------------------------------------------------------------------
# patch_item (analyst edit)
# ---------------------------------------------------------------------------


def _edited_status(old: str, new: str) -> str:
    """A pass after an item had failed (or had been fixed) is `fixed`: it failed once, then passed."""
    return "fixed" if new == "pass" and old in ("fail", "fixed") else new


def _recheck(item: m.Item, items: dict, sources: dict, pages: dict) -> tuple[str, list[m.CheckResult]]:
    results = _code_checks(item, items, sources, pages) + _judge_checks(item, stale=False)
    return _edited_status(item.verification.status, checks.status_from(results)), results


def _recheck_dependents(ctx: RunContext, item_id: str) -> None:
    """Re-run code checks on the items that cite the edited one, nearest first, keeping their judge results.
    A declared hole is never turned back into a pass here: that is the repair stage's call."""
    state = ctx.state
    sources, pages = state.sources_by_id(), _pages(state)
    holes = {h.item_id for h in state.holes if h.item_id}
    for did in dependents(state.items, item_id):
        dep = state.items[did]
        old = dep.verification.status
        if old == "pending":
            continue                      # the pipeline has not verified it yet
        status, results = _recheck(dep, state.items, sources, pages)
        if status == old or (status in _PASSING and old not in _PASSING and did in holes):
            continue
        new = _with_verification(dep, status, results)
        _verified(ctx, new, level="info" if status in _PASSING else "warn",
                  message=f"{did} {status} after the analyst edited {item_id}")


async def patch_item(ctx: RunContext, item_id: str, patch: m.ItemPatch) -> m.Item:
    """User edit of one item's text, then re-verify it by code (and re-check the items that cite it)."""
    state = ctx.state
    if ctx.stopped():
        raise _api_error(409, "invalid_state", "Run is stopped (kill switch)")
    item = state.items.get(item_id)
    if item is None:
        raise _api_error(404, "not_found", f"No item {item_id}")
    if item.kind == "chart":
        raise _api_error(422, "validation_error", "Charts are built by code and cannot be edited")
    new_text = patch.text.strip()
    if not new_text:
        raise _api_error(422, "validation_error", "The new text is empty")
    field = "statement" if item.kind == "data" else "text"
    before = getattr(item, field)
    if new_text == before.strip():
        return item
    edited = item.model_copy(deep=True)
    setattr(edited, field, new_text)
    pending = _with_verification(edited, "pending", [])
    state.items[item_id] = pending
    ctx.emit("item.updated", {"item": pending, "reason": "user_edit"},
             message=f"{item_id} edited by the analyst. Re-verifying by code.")

    items, sources, pages = dict(state.items), state.sources_by_id(), _pages(state)
    results = await asyncio.to_thread(_code_checks, pending, items, sources, pages)
    results += _judge_checks(item, stale=True)
    status = _edited_status(item.verification.status, checks.status_from(results))
    attempt = m.Attempt(attempt=len(item.verification.attempts) + 1, status=item.verification.status,
                        checks=list(item.verification.checks), action="user_edit", before=before, after=new_text,
                        note="Analyst edit. Code checks re-run on the new text.", at=now_iso())
    done = _with_verification(pending, status, results, attempt)
    if item.kind == "data":
        done = checks.with_fixed_locators(done, results)
    done = _verified(ctx, done, level="info" if status in _PASSING else "warn",
                     message=f"{item_id} re-verified after the analyst edit: {status}")
    _recheck_dependents(ctx, item_id)
    _sync_certainties(ctx, item_id)
    await _save(ctx)
    return done
