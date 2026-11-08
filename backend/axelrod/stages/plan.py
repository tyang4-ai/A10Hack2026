"""Stage plan: stream the frozen SHL plan (plan.need, plan.source, plan.ready) when the company matches an alias.

The plan is frozen for the demo: needs and sources were prepared before judging, not generated live by a
model. The plan.ready message and Run.notes say so. Any other company fails the run.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..config import BACKEND_DIR
from ..contract import m

if TYPE_CHECKING:
    from ..runtime import RunContext

PLAN_FILE = BACKEND_DIR / "data" / "shl" / "plan.json"
PACE_S = 0.15                      # gap between plan.need / plan.source events, so the UI fills in live
FROZEN_NOTE = ("Frozen demo plan: the needs and sources for Siemens Healthineers were prepared before the event, "
               "not generated live.")
UNAVAILABLE = "only the frozen Siemens Healthineers plan is available in this build"
_LEGAL_FORMS = {"ag", "se", "inc", "corp", "plc", "ltd", "nv", "sa", "co", "gmbh", "kgaa"}
TEST_PAGE_FILE = "/data/test_pages/injection.html"     # how plan.json's URL for the planted page ends
SERVED_RATIONALE = (" Served by Axelrod's own API at {url} (TEST_PAGE_URL), because the GitHub repo stays private "
                    "until submission. Still simulated: the API marks the page with X-Axelrod-Simulated: true.")
SERVED_NOTE = "{sid} is the planted test page (simulated), served by Axelrod's own API at {url}, not GitHub."


class PlanUnavailable(RuntimeError):
    """The requested company has no frozen plan."""


def load_plan(path: Path | None = None) -> dict[str, Any]:
    """Read plan.json at run time (never at import), so a new plan needs no restart."""
    p = Path(path or PLAN_FILE)
    if not p.is_file():
        raise PlanUnavailable(f"frozen plan file is missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _norm(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def matches(plan: dict[str, Any], company: str, ticker: str | None) -> bool:
    """Company or ticker equal to an alias, the plan's company or its ticker, ignoring case,
    punctuation and a trailing legal form ("AG"). "SHL.DE" counts as "SHL"."""
    keys = {_norm(a) for a in plan.get("aliases", [])} | {_norm(plan.get("company")), _norm(plan.get("ticker"))}
    keys.discard("")
    words = _norm(company).split()
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    candidates = {_norm(company), " ".join(words)}
    if ticker and _norm(ticker):
        t = _norm(ticker)
        candidates |= {t, t.split()[0]}
    return bool(candidates & keys)


def _fresh_source(raw: dict[str, Any]) -> m.Source:
    """A plan source in its start state: approval pending, status proposed, nothing fetched yet."""
    s = m.Source.model_validate(raw)
    return s.model_copy(update={"approval": "pending", "status": "proposed", "doc_ids": [],
                                "stats": m.SourceStats(), "error": None})


def _bump_counter(ctx: RunContext, prefix: str, ids: list[str]) -> None:
    """Ids in plan.json were written by hand. Move the allocator past them so S-/N- ids added later never clash."""
    nums = [int(i.split("-")[-1]) for i in ids if re.fullmatch(rf"{prefix}-\d+", i)]
    if nums:
        counters = ctx.state.ids.counters
        counters[prefix] = max(counters.get(prefix, 0), *nums)


async def run(ctx: RunContext) -> None:
    state = ctx.state
    plan = load_plan()
    if not matches(plan, state.run.company, state.run.ticker):
        raise PlanUnavailable(UNAVAILABLE)
    needs = [m.DataNeed.model_validate(n) for n in plan.get("needs", [])]
    sources = [_fresh_source(s) for s in plan.get("sources", [])]
    _apply_plan_fields(ctx, plan, needs, sources)
    served = serve_test_page(sources, ctx.settings.test_page_url)
    if served:
        _append_note(state.run, served)
    await _write_plan_files(ctx, plan, needs, sources)

    for need in needs:
        ctx.check_stop()
        state.needs.append(need)
        ctx.emit("plan.need", {"need": need}, stage="plan", message=f"Need {need.need_id}: {need.label}")
        await asyncio.sleep(PACE_S)
    for source in sources:
        ctx.check_stop()
        state.sources.append(source)
        ctx.emit("plan.source", {"source": source}, stage="plan",
                 message=f"Source {source.source_id} ({source.tier}, rank {source.rank}): {source.title}")
        await asyncio.sleep(PACE_S)
    ctx.check_stop()
    ctx.emit("plan.ready", {"needs": len(needs), "sources": len(sources)}, stage="plan",
             message=f"Plan ready: {len(needs)} needs, {len(sources)} sources. {FROZEN_NOTE}")


def _apply_plan_fields(ctx: RunContext, plan: dict[str, Any], needs: list[m.DataNeed],
                       sources: list[m.Source]) -> None:
    state = ctx.state
    for kind, ids in (("needs", [n.need_id for n in needs]), ("sources", [s.source_id for s in sources])):
        if len(set(ids)) != len(ids):
            raise ValueError(f"plan.json has duplicate {kind} ids")
    state.extract_pages = plan.get("extract_pages") or {}
    state.need_pages = plan.get("need_pages") or {}
    state.as_of = plan.get("as_of")
    state.fiscal_note = plan.get("fiscal_note")
    note = FROZEN_NOTE
    depth = plan.get("depth")
    if depth in ("screen", "full") and depth != state.run.depth:
        note += f" The frozen plan is {depth} depth, so this run is too (asked for {state.run.depth})."
        state.run.depth = depth
    state.run.notes = f"{state.run.notes.strip()} | {note}" if (state.run.notes or "").strip() else note
    _bump_counter(ctx, "N", [n.need_id for n in needs])
    _bump_counter(ctx, "S", [s.source_id for s in sources])


def is_test_page(source: m.Source) -> bool:
    """The planted page: its plan.json URL points at backend/data/test_pages/injection.html and it says simulated."""
    return urlsplit(source.url).path.endswith(TEST_PAGE_FILE) and "(simulated)" in source.title


def serve_test_page(sources: list[m.Source], url: str | None) -> str | None:
    """With TEST_PAGE_URL set, the planted page's source gets that URL (and its host as domain) in place.

    Returns the Run.notes line, or None when TEST_PAGE_URL is unset. A URL that isn't http(s) fails the run
    here, because the gateway would never allow it and the demo would break later with no clear reason.
    """
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"TEST_PAGE_URL must be an http(s) URL, got {url!r}")
    for i, source in enumerate(sources):
        if is_test_page(source):
            sources[i] = source.model_copy(update={
                "url": url, "domain": parts.hostname.lower(),
                "rationale": source.rationale + SERVED_RATIONALE.format(url=url)})
            return SERVED_NOTE.format(sid=source.source_id, url=url)
    return "TEST_PAGE_URL is set, but the plan has no planted test page, so no source URL was changed."


def _append_note(run: m.Run, note: str) -> None:
    run.notes = f"{run.notes.strip()} | {note}" if (run.notes or "").strip() else note


async def _write_plan_files(ctx: RunContext, plan: dict[str, Any], needs: list[m.DataNeed],
                            sources: list[m.Source]) -> None:
    """00-Meta/_plan.json and _url-manifest.json, so the vault drawer shows what the run was told to do."""
    body = {"frozen": True, "note": FROZEN_NOTE, "run_id": ctx.run_id, "company": plan.get("company"),
            "ticker": plan.get("ticker"), "depth": plan.get("depth"), "as_of": plan.get("as_of"),
            "fiscal_note": plan.get("fiscal_note"), "needs": [n.model_dump(mode="json") for n in needs],
            "sources": [s.model_dump(mode="json") for s in sources],
            "extract_pages": plan.get("extract_pages") or {}, "need_pages": plan.get("need_pages") or {}}
    await ctx.vault.aput_text(ctx.run_id, "00-Meta/_plan.json", json.dumps(body, indent=2, ensure_ascii=False))
    manifest = {s.source_id: s.url for s in sources}
    await ctx.vault.aput_text(ctx.run_id, "00-Meta/_url-manifest.json",
                              json.dumps(manifest, indent=2, ensure_ascii=False))
