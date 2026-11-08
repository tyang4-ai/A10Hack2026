"""Revoke cascade: after a source is revoked, re-run citations_resolve on everything that rests on it.

The gateway's R-revoke rule stops new requests to the source, but items already extracted from it are still in
the report. So after Gateway.revoke, this module re-runs the code check citations_resolve on every data item from
that source, then on the items that cite those (D, then C, then E and G), then on brief sentences when a brief
exists. An item whose check changes goes blocked (blocked never counts as a pass) with the detail
"source revoked: S-006", its certainty is recomputed by contract/certainty.py, and two events go out for it:
item.updated with reason "source_revoked" (contract v0.2) carrying the whole new version, then item.verified with
the verification and certainty (summary.verified for a sentence).

A final report keeps its rollup: finalize is not re-run. The score items' details name the rollup criteria that
still count them. A source revoked before its items exist is simply never extracted (collect's fetch is blocked).
Pending items are left alone: the stage that verifies them sees the revoked source itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, get_args

from .contract import RUBRIC, certainty, m
from .ids import now_iso
from .verify import checks

if TYPE_CHECKING:
    from .runtime import RunContext
    from .store.state import RunState

log = logging.getLogger(__name__)

UPDATE_REASON = "source_revoked"
RESOLVE = "citations_resolve"
PASSING = ("pass", "fixed")


@dataclass
class CascadeResult:
    source_id: str
    item_ids: list[str] = field(default_factory=list)       # items whose verification changed, in check order
    sentence_ids: list[str] = field(default_factory=list)   # brief sentences whose verification changed
    criteria: list[str] = field(default_factory=list)       # final-rollup criteria resting on the revoked source


def update_reason_allowed() -> bool:
    """True when contract/models.py lists "source_revoked" as an item.updated reason (it does since v0.2)."""
    ann = m.ItemUpdatedData.model_fields["reason"].annotation
    return UPDATE_REASON in get_args(ann)


# ---------------------------------------------------------------------------
# Which items rest on a source
# ---------------------------------------------------------------------------


def _refs(item: m.Item) -> list[str]:
    if item.kind == "conclusion":
        return list(item.data_ids)
    if item.kind == "extrapolation":
        return list(item.conclusion_ids)
    if item.kind == "chart":
        return [r.split(":")[0] for r in item.refs]
    return []


def affected(items: dict[str, m.Item], source_id: str) -> list[str]:
    """The source's data items, then the items citing them, nearest first (D, then C, then E and G), by id
    within each level. Each item appears once, at the first level that reaches it."""
    level = sorted(i for i, it in items.items() if it.kind == "data" and it.source_id == source_id)
    found = list(level)
    seen = set(level)
    while level:
        hit = set(level)
        level = sorted(i for i, it in items.items() if i not in seen and hit & set(_refs(it)))
        found += level
        seen.update(level)
    return found


# ---------------------------------------------------------------------------
# Re-running the check
# ---------------------------------------------------------------------------


def _resolve_check(item: m.Item, state: RunState) -> m.CheckResult | None:
    """The item's citations_resolve result as the code checker gives it now. Pages are not needed for it."""
    results = checks.check_item(item, items=state.items, sources=state.sources_by_id(), pages_by_doc={},
                                rubric=RUBRIC)
    return next((c for c in results if c.check == RESOLVE), None)


def _revoked_detail(check: m.CheckResult, hit_refs: list[str], source_id: str, *, own_source: bool) -> str:
    """Say plainly that the source was revoked, whatever wording the checker used for this kind.

    A data item from the source gets exactly "source revoked: S-006". A dependent keeps the checker's words
    when they already name a revoked source (C and E do: "D-010: source revoked: S-006"). A chart's checker
    only sees its inputs' status, so it gets the same "D-010: source revoked: S-006" form."""
    phrase = f"source revoked: {source_id}"
    if own_source:
        return phrase
    if "source revoked: " in check.detail:
        return check.detail
    return "; ".join(f"{r}: {phrase}" for r in hit_refs) or phrase


def _rollup_note(item: m.Item, state: RunState) -> str | None:
    """For a scored criterion in a final report: the rollup still counts it, since finalize is not re-run."""
    if item.kind != "extrapolation" or item.score is None or not state.report_ready:
        return None
    score = f"{item.score:+d}" if item.score else "0"
    return f" The final rollup still counts {item.criterion} ({score}): finalize was not re-run after the revoke."


def _replace(found: list[m.CheckResult], new: m.CheckResult) -> list[m.CheckResult]:
    """The checks with citations_resolve swapped for the new result (appended when there was none)."""
    out = [new if c.check == RESOLVE else c for c in found]
    return out if any(c.check == RESOLVE for c in found) else out + [new]


def _new_status(old: str, found: list[m.CheckResult]) -> str:
    status = checks.status_from(found)
    return "fixed" if status == "pass" and old == "fixed" else status


def _recheck_item(item: m.Item, state: RunState, source_id: str, hit: set[str]) -> m.Item | None:
    """A copy with citations_resolve re-run, or None when nothing changed (or it was never verified)."""
    v = item.verification
    if v.status == "pending":
        return None
    new = _resolve_check(item, state)
    if new is None:
        return None
    if new.result == "blocked":
        own = item.kind == "data" and item.source_id == source_id
        detail = _revoked_detail(new, [r for r in _refs(item) if r in hit], source_id, own_source=own)
        note = _rollup_note(item, state)
        new = new.model_copy(update={"detail": detail + (note or "")})
    old = next((c for c in v.checks if c.check == RESOLVE), None)
    if old is not None and old.model_dump() == new.model_dump():
        return None
    found = _replace(list(v.checks), new)
    out = item.model_copy(deep=True)
    out.verification = m.Verification(status=_new_status(v.status, found), checks=found,
                                      attempts=list(v.attempts), verified_at=now_iso())
    return out


# ---------------------------------------------------------------------------
# Certainty
# ---------------------------------------------------------------------------


def _none_certainty(status: str) -> m.Certainty:
    return m.Certainty(score=None, band="none", strength=0.0, support=0.0, inherited=0.0, completeness=1.0,
                       weakest_ref=None, basis=f"Verification {status}")


def _item_certainties(state: RunState) -> dict[str, dict]:
    """contract/certainty.py over the whole report (certainty is inherited along citations). {} on bad data."""
    try:
        return certainty.item_certainties([i.model_dump(mode="json") for i in state.items.values()],
                                          [s.model_dump(mode="json") for s in state.sources])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        log.exception("certainty could not be computed for run %s", state.run.run_id)
        return {}


def _certainty_of(item: m.Item, certs: dict[str, dict]) -> m.Certainty:
    raw = certs.get(item.id)
    return m.Certainty.model_validate(raw) if raw is not None else _none_certainty(item.verification.status)


# ---------------------------------------------------------------------------
# Brief sentences
# ---------------------------------------------------------------------------


def _sentences(summary: m.HumanReport) -> list[m.SummarySentence]:
    out: list[m.SummarySentence] = []
    for sec in summary.sections:
        for b in sec.blocks:
            if b.type in ("paragraph", "bullets"):
                out.extend(b.sentences)
            elif b.type == "chart":
                out.append(b.caption)
    return out


def _recheck_sentence(s: m.SummarySentence, state: RunState, source_id: str, hit: set[str]) -> bool:
    """Re-run citations_resolve on one sentence in place. True when its verification changed."""
    if s.verification.status == "pending" or not hit & {r.split(":")[0] for r in s.derived_from}:
        return False
    results = checks.check_sentence(s, state.items, state.holes, rollup=state.rollup, rubric=RUBRIC,
                                    sources=state.sources_by_id())
    new = next((c for c in results if c.check == RESOLVE), None)
    if new is None:
        return False
    if new.result == "blocked":
        refs = [r for r in s.derived_from if r.split(":")[0] in hit]
        new = new.model_copy(update={"detail": _revoked_detail(new, refs, source_id, own_source=False)})
    old = next((c for c in s.verification.checks if c.check == RESOLVE), None)
    if old is not None and old.model_dump() == new.model_dump():
        return False
    found = _replace(list(s.verification.checks), new)
    s.verification = m.Verification(status=_new_status(s.verification.status, found), checks=found,
                                    attempts=list(s.verification.attempts), verified_at=now_iso())
    return True


def _sentence_certainty(s: m.SummarySentence, state: RunState, certs: dict[str, dict]) -> m.Certainty:
    try:
        raw = certainty.sentence_certainty(s.model_dump(mode="json"), certs,
                                           [i.model_dump(mode="json") for i in state.items.values()],
                                           [h.model_dump(mode="json") for h in state.holes])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        log.exception("sentence certainty could not be computed for %s", s.id)
        return _none_certainty(s.verification.status)
    return m.Certainty.model_validate(raw)


def _cascade_sentences(ctx: RunContext, source_id: str, hit: set[str], certs: dict[str, dict]) -> list[str]:
    state = ctx.state
    if state.summary is None:
        return []
    changed: list[str] = []
    for s in _sentences(state.summary):
        if not _recheck_sentence(s, state, source_id, hit):
            continue
        s.certainty = _sentence_certainty(s, state, certs)
        changed.append(s.id)
        ctx.emit("summary.verified", m.SummaryVerifiedData(sentence_id=s.id, verification=s.verification,
                                                           certainty=s.certainty),
                 level="warn", message=f"{s.id} {s.verification.status}: {source_id} was revoked")
    if changed:
        sents = _sentences(state.summary)
        state.summary.stats.passed = sum(x.verification.status == "pass" for x in sents)
        state.summary.stats.fixed = sum(x.verification.status == "fixed" for x in sents)
    return changed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _message(item: m.Item, source_id: str) -> str:
    status = item.verification.status
    tail = ""
    if item.kind == "extrapolation" and item.score is not None:
        tail = f" ({item.criterion} score rests on it)"
    return f"{item.id} {status}: {source_id} was revoked{tail}"


def revoke_cascade(ctx: RunContext, source_id: str) -> CascadeResult:
    """Call after Gateway.revoke(source_id). Re-runs citations_resolve down the citation graph, recomputes
    certainty and emits the events. Synchronous and quick: code checks only, no model calls."""
    state = ctx.state
    result = CascadeResult(source_id=source_id)
    ids = affected(state.items, source_id)
    hit = set(ids)
    changed: list[m.Item] = []
    for iid in ids:                     # nearest first, so a chart sees its data items' new status
        item = state.items.get(iid)
        new = _recheck_item(item, state, source_id, hit) if item is not None else None
        if new is None:
            continue
        state.items[iid] = new
        changed.append(new)
    certs = _item_certainties(state)
    for item in changed:
        item.certainty = _certainty_of(item, certs)
        ctx.emit("item.updated", m.ItemUpdatedData(item=item, reason=UPDATE_REASON), level="warn",
                 message=f"{item.id}: {source_id} was revoked, re-checking citations")
        ctx.emit("item.verified", m.ItemVerifiedData(item_id=item.id, verification=item.verification,
                                                     certainty=item.certainty),
                 level="warn", message=_message(item, source_id))
        result.item_ids.append(item.id)
        if state.report_ready and item.kind == "extrapolation" and item.score is not None:
            result.criteria.append(item.criterion)
    result.sentence_ids = _cascade_sentences(ctx, source_id, hit, certs)
    return result
