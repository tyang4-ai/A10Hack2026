"""Baseline evaluation, an experiment: the harness against one plain Claude report on the same SHL pages.

Usage, from the repo root (PY is the venv python):
  PY backend/scripts/baseline.py --keep                           live: fetch, extract, one baseline call, compare
  PY backend/scripts/baseline.py --reuse backend/.data/baseline   recompute from a kept run, no model calls
  PY backend/scripts/baseline.py --fake --out x.md                simulated models (pages are still fetched)

BACKEND.md section 11. The pages are the plan's extract_pages of S-001 to S-005 (data/shl/plan.json), fetched
through the real gateway into a throwaway local run (STORE=local). The harness side is the real extract stage.
The baseline is one large-model call (role drafter, through AgentRunner and the gateway, so it is metered and
shows up as traffic) with every page at once and a plain analyst prompt. Code then runs the same checks on
both sides, plants the same kinds of error into copies of clean items on both sides (stored pages are never
edited) and writes data/baseline/results.md and results.json, labeled as an experiment, not the demo run.

The run folder lives under backend/.data/baseline (short on purpose: Windows paths stop at 260 characters)
and is deleted afterwards unless --keep. Never commit a PDF.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND.parent))           # repo root, so `backend.axelrod` imports
sys.path.insert(0, str(Path(__file__).resolve().parent))  # presmoke.py next to this file

from pydantic import BaseModel, Field, field_validator, model_validator  # noqa: E402

import presmoke  # noqa: E402  reuse cut_plan and parse_pages
from backend.axelrod.agents import extractor as ex  # noqa: E402
from backend.axelrod.agents.base import AgentOutputError, AgentRunner, register_fake, tool_name  # noqa: E402
from backend.axelrod.contract import RUBRIC, m  # noqa: E402
from backend.axelrod.gateway import Blocked  # noqa: E402
from backend.axelrod.stages import extract as st  # noqa: E402
from backend.axelrod.verify import checks, text  # noqa: E402
from backend.axelrod.verify.numbers import parse_number, period_key, scan_numbers  # noqa: E402

PLAN_PATH = BACKEND / "data" / "shl" / "plan.json"
DEFAULT_DATA_DIR = BACKEND / ".data" / "baseline"
DEFAULT_OUT = BACKEND / "data" / "baseline" / "results.md"
SOURCE_IDS = ("S-001", "S-002", "S-003", "S-004", "S-005")
RAW_RELPATH = "00-Meta/baseline/raw.json"
NOTES_MARK = "Experiment (baseline.py)"
LABEL = "Experiment, run on {date}, not part of the demo run."

CORE = ("quote_exact", "value_in_quote", "unit_context", "period_match")
MAX_CLAIMS = 60
MAX_TOKENS = 12000          # about 100 output tokens per claim, plus scores, with room to spare
PER_KIND = 5
HAND_CHECK = 20

Scale = Literal["one", "thousand", "million", "billion"]
_SCALE_WORDS = ex._SCALE_WORDS


# ---------------------------------------------------------------------------
# The baseline's output model (flat, no contract ids)
# ---------------------------------------------------------------------------


class BaselineClaim(BaseModel):
    """One number or fact the screen relies on, with the document and page it is printed on."""

    statement: str = Field(description="One sentence stating the fact with its unit and period.")
    metric: str = Field(default="", description="What is measured, e.g. 'Revenue'.")
    value: float | None = Field(default=None, description="The number as printed, before scale, negative for "
                                                          "negatives: 23375 for '23,375' in a table in millions "
                                                          "of €, -1204 for '(1,204)', 5.9 for '5.9%'. Empty for a "
                                                          "fact without a number.")
    value_text: str = Field(default="", description="The number exactly as printed in the quote, with its sign "
                                                    "or parentheses, without currency or scale words.")
    unit: str = Field(default="", description="EUR, USD, %, count, x or years.")
    scale: Scale = Field(default="one", description="one, thousand, million or billion.")
    period: str = Field(default="", description="Fiscal year or date: 'FY2025', 'Q4 FY2026', '2025-09-30'.")
    document: str = Field(description="Tag of the document the quote comes from, e.g. 'AR2025'.")
    page: int = Field(default=0, description="The page number in the page header.")
    quote: str = Field(description="The line or sentence the number comes from, copied exactly from the page.")
    criterion: str = Field(default="", description="The BOFFIN criterion this claim supports.")

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        """Models send null for optional fields now and then. Treat it as empty."""
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if v is not None}
            for key in ("value_text", "period", "unit", "metric", "criterion"):
                if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                    data[key] = str(data[key])
        return data

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: Any) -> Any:
        """'23,375' as a string is still a number. Text that holds none is no value."""
        if isinstance(v, str):
            n = parse_number(v)
            return float(n) if n is not None else None
        return v

    @field_validator("scale", mode="before")
    @classmethod
    def _scale(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        key = re.sub(r"[^a-z0-9']", "", v.lower().replace("eur", "").replace("usd", ""))
        return _SCALE_WORDS.get(key, v)

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v: Any) -> Any:
        """'p. 138' and '138' both mean page 138."""
        if isinstance(v, str):
            mm = re.search(r"\d+", v)
            return int(mm.group(0)) if mm else 0
        return v


class CriterionScore(BaseModel):
    """The screen's score for one BOFFIN criterion."""

    criterion: str
    score: int | None = Field(default=None, description="-1, 0 or +1. Empty when the pages do not say enough.")
    reasoning: str = Field(default="", description="Two or three sentences with the numbers the score rests on.")

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, v: Any) -> Any:
        if isinstance(v, str):
            mm = re.fullmatch(r"\s*([+\-−]?)\s*(\d)\s*", v)
            if not mm:
                return None
            return -int(mm.group(2)) if mm.group(1) in ("-", "−") else int(mm.group(2))
        return v


class BaselineReport(BaseModel):
    """A BOFFIN screen of the company: a short summary, a score per criterion, and the claims it rests on."""

    TOOL_NAME: ClassVar[str] = "write_boffin_screen"
    summary: str = ""
    criteria: list[CriterionScore] = Field(default_factory=list)
    claims: list[BaselineClaim] = Field(default_factory=list)


TOOL = tool_name(BaselineReport)


# ---------------------------------------------------------------------------
# Documents and the prompt
# ---------------------------------------------------------------------------


@dataclass
class DocRef:
    """One stored document as the baseline sees it: a readable tag, its title and the pages it reads."""
    tag: str
    doc_id: str
    source_id: str
    title: str
    pages: list[int]


def doc_refs(state, source_ids) -> list[DocRef]:
    """The stored, readable documents of the given sources, with the same pages the harness reads."""
    by_id, docs = state.sources_by_id(), state.documents_by_id()
    refs: list[DocRef] = []
    used: set[str] = set()
    for sid in source_ids:
        src = by_id.get(sid)
        for doc_id in src.doc_ids if src else []:
            doc = docs.get(doc_id)
            if doc is None or not state.agent_pages(doc_id):
                continue  # quarantined or never stored
            tag = _tag_for(src, used)
            used.add(tag)
            refs.append(DocRef(tag, doc_id, sid, src.title, st.pages_to_read(state, src, doc)))
    return refs


def _tag_for(src: m.Source, used: set[str]) -> str:
    """'Annual Report FY2025' gives AR2025, 'Q4 FY2026 earnings release' gives Q4FY2026."""
    q = re.search(r"\bQ([1-4])\s*FY\s?(\d{4})\b", src.title)
    y = re.search(r"\bFY\s?(\d{4})\b", src.title)
    if q:
        base = f"Q{q.group(1)}FY{q.group(2)}"
    elif y and src.kind == "annual_report":
        base = f"AR{y.group(1)}"
    elif y:
        base = f"DOCFY{y.group(1)}"
    else:
        base = "DOCUMENT"
    tag, n = base, 2
    while tag in used:
        tag, n = f"{base}-{n}", n + 1
    return tag


SYSTEM = ("You are an equity research analyst. You write short, factual company screens from the documents "
          "you are given, and you cite where every number comes from.")


def _criteria_lines() -> list[str]:
    lines = []
    for key in RUBRIC["depths"]["screen"]:
        c = RUBRIC["criteria"][key]
        bits = [f"- {key}: {c['label']}."]
        if c.get("plus_one"):
            bits.append(f"+1: {c['plus_one']}.")
        if c.get("minus_one"):
            bits.append(f"-1: {c['minus_one']}.")
        hint = re.sub(r",?\s*framework definitions below\.?", ".", c.get("evidence_hint") or "")
        if hint:
            bits.append(hint)
        lines.append(" ".join(bits))
    return lines


def build_prompt(state, refs: list[DocRef], max_claims: int = MAX_CLAIMS) -> str:
    """What an analyst would write, plus the pages. The structure lives in the tool schema."""
    company = state.run.company + (f" ({state.run.ticker})" if state.run.ticker else "")
    lines = [f"Write a BOFFIN screen of {company} from the pages below. Cite the document and page number for "
             "every number you use, and quote each number exactly as it is printed.", ""]
    if state.fiscal_note:
        lines += [f"Fiscal years: {state.fiscal_note}", ""]
    lines.append("BOFFIN screen criteria. Score each -1, 0 or +1 under the framework, or leave the score empty "
                 "when the pages do not say enough:")
    lines += _criteria_lines()
    lines += ["", "Documents:"] + [f"- {r.tag}: {r.title}" for r in refs]
    lines += ["", f"List up to {max_claims} claims, the most important numbers first. For each, give the "
                  "statement, the value, the value text exactly as printed, unit, scale, period, the document "
                  "tag, the page number in the page header, and an exact quote of the line or sentence the "
                  "number comes from.", ""]
    for r in refs:
        pages = state.agent_pages(r.doc_id)
        for p in r.pages:
            lines += [f"=== {r.tag} page {p} ===", pages[p - 1], f"=== end of {r.tag} page {p} ===", ""]
    lines.append(f"Call {TOOL} with the screen.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fake baseline (FAKE_MODELS=1). Simulated: the extractor's pattern-matching fake on each page, no model.
# ---------------------------------------------------------------------------

_BLOCK = re.compile(r"^=== (?P<tag>\S+) page (?P<page>\d+) ===\n(?P<text>.*?)\n=== end of (?P=tag) page (?P=page) ===$",
                    re.M | re.S)


def fake_baseline(system: str, user: str, schema: dict) -> dict:
    """Simulated plain report for FAKE_MODELS=1: table rows and euro sentences found by pattern matching."""
    claims = []
    for mm in _BLOCK.finditer(user):
        wrapped = f"{ex.PAGE_START}\n{mm.group('text')}\n{ex.PAGE_END}"
        for it in ex.fake_extract("", wrapped, {})["items"]:
            num = parse_number(it["value_as_reported"])
            claims.append({"statement": it["statement"], "metric": it["metric"],
                           "value": float(num) if num is not None else None, "value_text": it["value_as_reported"],
                           "unit": it["unit"], "scale": it["scale"], "period": it["period"],
                           "document": mm.group("tag"), "page": int(mm.group("page")), "quote": it["quote"]})
    note = "Simulated baseline (FAKE_MODELS=1). No model was called."
    return {"summary": note, "claims": claims[:MAX_CLAIMS],
            "criteria": [{"criterion": k, "score": None, "reasoning": note} for k in RUBRIC["depths"]["screen"]]}


register_fake(TOOL, fake_baseline)


# ---------------------------------------------------------------------------
# The run: plan cut to the sources, collect, harness extract, one baseline call
# ---------------------------------------------------------------------------


def cut_plan(plan: dict, source_ids, max_pages: int | None = None):
    """(needs, sources, extract_pages, need_pages) for the given sources, auto-approved (presmoke's cut, merged)."""
    sources, extract, want = [], {}, set()
    for sid in source_ids:
        _, source, ext, _ = presmoke.cut_plan(plan, sid, None)
        sources.append(source)
        want.update(source.need_ids)
        if max_pages and ext.get(sid):
            flat = sorted({p for lo, hi in ext[sid] for p in range(int(lo), int(hi) + 1)})[:max_pages]
            ext = {sid: [[p, p] for p in flat]}
        extract.update(ext)
    needs = [m.DataNeed.model_validate(n) for n in plan.get("needs", []) if n.get("need_id") in want]
    need_pages = {}
    for nid, per in (plan.get("need_pages") or {}).items():
        kept = {sid: rng for sid, rng in per.items() if sid in source_ids}
        if kept:
            need_pages[nid] = kept
    return needs, sources, extract, need_pages


def build_context(plan: dict, source_ids, *, data_dir: Path, budget: float, fake: bool,
                  max_pages: int | None = None):
    """A throwaway local run with the real Store, RunContext and Gateway. Approval is automatic."""
    from backend.axelrod.config import load_settings
    from backend.axelrod.runtime import RunContext
    from backend.axelrod.store import Store

    base = load_settings()
    settings = base.model_copy(update={
        "store": "local", "data_dir": data_dir, "vault_bucket": None, "runs_table": None, "events_table": None,
        "stage_pacing": 0.0, "fake_models": bool(fake or base.fake_models)})
    needs, sources, extract, need_pages = cut_plan(plan, source_ids, max_pages)
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(
        company=plan.get("company", "Siemens Healthineers AG"), ticker=plan.get("ticker"),
        notes=f"{NOTES_MARK}: {', '.join(source_ids)}, auto-approved. Not part of the demo run.",
        budget_usd=budget, created_via="cli"))
    state.needs, state.sources = needs, sources
    state.extract_pages, state.need_pages = extract, need_pages
    state.as_of, state.fiscal_note, state.plan_ready = plan.get("as_of"), plan.get("fiscal_note"), True
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="Baseline experiment run (not the demo run)", stage="plan")
    ctx.state.run.stage = "approve"
    ctx.gateway.build_policy()  # allows only these sources, default deny for the rest
    return ctx


async def run_stage(ctx, stage: str) -> float:
    """One real pipeline stage, timed."""
    mod = importlib.import_module(f"backend.axelrod.stages.{stage}")
    ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
    t0 = time.perf_counter()
    await mod.run(ctx)
    return time.perf_counter() - t0


async def run_baseline(ctx, refs: list[DocRef], *, max_tokens: int = MAX_TOKENS,
                       max_claims: int = MAX_CLAIMS) -> dict:
    """The one plain Claude call: role drafter, large model, through AgentRunner and the gateway."""
    user = build_prompt(ctx.state, refs, max_claims)
    runner = AgentRunner(ctx, "drafter", "Plain Claude baseline (experiment): one BOFFIN screen from every page "
                                         "at once", sorted({r.source_id for r in refs}), "large")
    t0 = time.perf_counter()
    report, error = None, None
    try:
        with runner:
            report = await runner.ask(SYSTEM, user, BaselineReport, max_tokens=max_tokens)
    except (Blocked, AgentOutputError) as e:
        error = e.reason if isinstance(e, Blocked) else str(e).splitlines()[0]
    return {"agent_id": runner.agent_id, "seconds": time.perf_counter() - t0, "error": error,
            "prompt_chars": len(SYSTEM) + len(user), "max_tokens": max_tokens,
            "report": report.model_dump(mode="json") if report is not None else None}


async def compare(ctx, source_ids, *, max_tokens: int = MAX_TOKENS, max_claims: int = MAX_CLAIMS,
                  collect_seconds: float | None = None) -> dict:
    """Harness extract, then the baseline call, on the documents already stored. Saves raw.json and the state."""
    refs = doc_refs(ctx.state, source_ids)
    if not refs:
        raise ValueError("no stored pages to compare: every source failed or was quarantined")
    harness_seconds = await run_stage(ctx, "extract")
    baseline = await run_baseline(ctx, refs, max_tokens=max_tokens, max_claims=max_claims)
    raw = {"experiment": NOTES_MARK, "run_id": ctx.run_id, "created_at": ctx.state.run.created_at,
           "simulated": bool(ctx.settings.fake_models), "source_ids": list(source_ids),
           "refs": [asdict(r) for r in refs], "collect_seconds": collect_seconds,
           "harness_seconds": harness_seconds, "baseline": baseline}
    await asyncio.to_thread(ctx.vault.put_text, ctx.run_id, RAW_RELPATH,
                            json.dumps(raw, ensure_ascii=False, indent=1))
    ctx.store.save(ctx.state)
    return raw


def load_kept(data_dir: Path):
    """(state, raw) of the newest baseline run kept under data_dir. No network, no models."""
    from backend.axelrod.config import load_settings
    from backend.axelrod.store import Store

    settings = load_settings().model_copy(update={"store": "local", "data_dir": data_dir, "vault_bucket": None,
                                                  "runs_table": None, "events_table": None})
    store = Store(settings)
    states = [s for s in store._load_local() if NOTES_MARK in (s.run.notes or "")]
    if not states:
        raise LookupError(f"no kept baseline run under {data_dir}")
    state = max(states, key=lambda s: s.run.created_at)
    raw = json.loads(store.vault.get_text(state.run.run_id, RAW_RELPATH))
    return state, raw


# ---------------------------------------------------------------------------
# Checking both sides
# ---------------------------------------------------------------------------


@dataclass
class Checked:
    """One harness item or baseline claim with the code checks it got."""
    side: str
    label: str
    item: m.DataItem | None
    results: list[m.CheckResult]
    cited_page: int | None = None
    found_page: int | None = None
    note: str = ""

    def core(self) -> list[m.CheckResult]:
        return [c for c in self.results if c.check in CORE]

    @property
    def status(self) -> str:
        """pass, fail or blocked over the four core checks. A claim code could not resolve fails."""
        return "fail" if self.item is None else checks.status_from(self.core())

    def failed(self, check: str) -> bool:
        return any(c.check == check and c.result == "fail" for c in self.results)

    @property
    def page_off(self) -> bool:
        """The quote is on the stored page, but not the one the claim cited."""
        return (self.cited_page is not None and self.found_page is not None and not self.failed("quote_exact")
                and self.cited_page != self.found_page)


def check_harness(state, source_ids, pages_by_doc, sources) -> list[Checked]:
    """The harness's agent-extracted data items, re-checked with the same code as the baseline."""
    out = []
    for it in sorted(state.data_items(), key=lambda i: i.id):
        if (it.source_id not in source_ids or it.extracted_by != "agent"
                or not isinstance(it.citations[0].locator, m.TextLocator)):
            continue
        res = checks.check_data_item(it, pages_by_doc, sources)
        out.append(Checked("harness", it.id, it, res, found_page=it.citations[0].locator.page))
    return out


def _unit(raw: str, value_text: str) -> str:
    toks = scan_numbers(text.normalize(value_text)) if value_text else []
    currency = toks[0].currency if toks else None
    low = raw.strip().lower()
    if "%" in raw or "percent" in low or value_text.strip().endswith("%"):
        return "%"
    if "eur" in low or "€" in raw:
        return "EUR"
    if "usd" in low or "$" in raw:
        return "USD"
    return st.unit_code(raw, currency)


def claim_value(c: BaselineClaim) -> m.Value | None:
    """The claim's own number and value text. value_in_quote then checks the two agree with the quote."""
    vt = c.value_text.strip()
    number = c.value
    if number is None and vt:
        parsed = parse_number(vt)
        number = float(parsed) if parsed is not None else None
    if number is None:
        return None
    unit = _unit(c.unit, vt)
    shown = vt or format(Decimal(repr(float(number))).normalize(), "f")
    return m.Value(number=float(number), as_reported=shown, unit=unit, scale="one" if unit == "%" else c.scale,
                   period=c.period.strip() or None)


def locate_claim(doc_id: str, pages: list[str], quote: str, hint: int) -> tuple[m.Citation, bool]:
    """Offsets by code. Found: the citation holds the exact page text. Not found: the claim's own text at the
    closest real span, so quote_exact fails with a diff."""
    hint = min(max(hint, 1), len(pages))
    hit = text.locate(pages, quote, hint) if text.normalize(quote).strip() else None
    if hit is not None:
        p, s, e = hit
        return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=p, char_start=s, char_end=e),
                          quote=pages[p - 1][s:e]), True
    best = text.closest_span(pages, quote, hint) if text.normalize(quote).strip() else None
    p, (s, e) = (best[0], best[2]) if best else (hint, (0, 0))
    return m.Citation(doc_id=doc_id, locator=m.TextLocator(page=p, char_start=s, char_end=e), quote=quote), False


def _criteria(c: BaselineClaim, src: m.Source) -> tuple[list[str], str]:
    crit = c.criterion.strip().lower().replace(" ", "_")
    crits = [crit] if crit in RUBRIC["criteria"] else list(src.criteria[:1]) or ["fundamental_thesis"]
    return crits, RUBRIC["criteria"][crits[0]]["category"]


def check_claim(n: int, c: BaselineClaim, refs: dict[str, DocRef], pages_by_doc, sources,
                agent_id: str) -> Checked:
    """A baseline claim as a DataItem (id by code), then the same data checks the harness gets. The unit and
    period headers above a found quote are added by code, as the harness does for its own items."""
    label = f"claim {n}"
    ref = refs.get(re.sub(r"\s+", "", c.document).upper())
    pages = pages_by_doc.get(ref.doc_id) if ref else None
    if ref is None or not pages:
        miss = m.CheckResult(check="quote_exact", method="code", result="fail",
                             detail=f"The claim cites document '{c.document}', which is not one of the documents given")
        return Checked("baseline", label, None, [miss], cited_page=c.page or None,
                       note=f"unknown document '{c.document}'")
    src = sources[ref.source_id]
    value = claim_value(c)
    ev, found = locate_claim(ref.doc_id, pages, c.quote, c.page or 1)
    cits = [ev]
    if found:
        raw = ex.ExtractedItem(statement=c.statement or "-", quote=c.quote)
        cits += st.context_citations(ref.doc_id, pages, ev, raw, value)
    crits, category = _criteria(c, src)
    item = m.DataItem(id=f"BL-{n:03d}", category=category, criteria=crits, statement=c.statement.strip() or c.quote,
                      value=value, citations=cits, source_id=src.source_id, source_tier=src.tier,
                      extracted_by="agent", agent_id=agent_id, verification=m.Verification(status="pending"))
    res = checks.check_data_item(item, pages_by_doc, sources)
    return Checked("baseline", label, item, res, cited_page=c.page or None,
                   found_page=ev.locator.page if found else None)


def check_baseline(report: BaselineReport, refs: list[DocRef], pages_by_doc, sources, agent_id: str) -> list[Checked]:
    by_tag = {r.tag.upper(): r for r in refs}
    return [check_claim(i, c, by_tag, pages_by_doc, sources, agent_id) for i, c in enumerate(report.claims, 1)]


# ---------------------------------------------------------------------------
# Planted errors: the B-verify kinds, into copies of clean items. Stored pages are never edited.
# ---------------------------------------------------------------------------

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_NOT = re.compile(r"\s(?:not|never)\b")
_AUX = re.compile(r"\b(?:is|was|are|were|will|would|has|have|had|can|could|did|does|do|should|must)\b")
_SCALE_DOWN = {"million": "thousand", "billion": "million", "thousand": "million"}


def _whole(a: str) -> re.Pattern:
    """a as a whole number: not inside a longer one."""
    return re.compile(rf"(?<![\d.,]){re.escape(a)}(?!\d|[.,]\d)")


def _core(a: str) -> str:
    return re.sub(r"[()\s−–\-+%]", "", a)


def _ev(item: m.DataItem) -> int:
    return next(i for i, c in enumerate(item.citations) if c.role == "evidence")


def _with(item: m.DataItem, *, quote: str | None = None, statement: str | None = None, **value) -> m.DataItem:
    """A deep copy of item with a new evidence quote, statement or value fields. The locator stays as it was."""
    new = item.model_copy(deep=True)
    if quote is not None:
        k = _ev(new)
        new.citations[k] = new.citations[k].model_copy(update={"quote": quote})
    if statement is not None:
        new.statement = statement
    if value:
        new.value = new.value.model_copy(update=value)
    return new


def plant_digit_swap(item: m.DataItem):
    """Swap the last two neighbouring digits that differ, in the value and in the quote: 18,742 to 18,724."""
    v = item.value
    if v is None:
        return None
    a = v.as_reported
    pairs = [i for i in range(len(a) - 1) if a[i].isdigit() and a[i + 1].isdigit() and a[i] != a[i + 1]]
    if not pairs:
        return None
    i = pairs[-1]
    new_a = a[:i] + a[i + 1] + a[i] + a[i + 2:]
    q = item.citations[_ev(item)].quote
    mm = _whole(a).search(q)
    num = parse_number(new_a)
    if mm is None or num is None:
        return None
    sign = -1 if v.number < 0 else 1
    statement = item.statement.replace(_core(a), _core(new_a), 1)
    planted = _with(item, quote=q[:mm.start()] + new_a + q[mm.end():], statement=statement,
                    number=float(abs(num)) * sign, as_reported=new_a)
    return planted, f"'{a}' to '{new_a}' in the quote and the value"


def plant_dropped_minus(item: m.DataItem):
    """Drop the sign of a negative value: (10,233) to 10,233. The quote keeps its sign."""
    v = item.value
    if v is None or v.number >= 0:
        return None
    a = v.as_reported.strip()
    new_a = a[1:-1].strip() if a.startswith("(") and a.endswith(")") else re.sub(r"^[−–\-]\s*", "", a)
    statement = re.sub(rf"[−–\-]\s?(?=[€$]?\s?{re.escape(_core(a))})", "", item.statement, count=1)
    return _with(item, statement=statement, number=abs(v.number), as_reported=new_a), f"'{a}' to '{new_a}'"


def plant_wrong_year(item: m.DataItem):
    """Move the period one year back: FY2025 to FY2024, 2025-09-30 to 2024-09-30."""
    v = item.value
    if v is None or not v.period:
        return None
    years = {int(y) for y in _YEAR.findall(v.period)}
    if not years:
        return None

    def back(s: str) -> str:
        return _YEAR.sub(lambda mm: str(int(mm.group(0)) - 1) if int(mm.group(0)) in years else mm.group(0), s)

    new_period = back(v.period)
    return _with(item, statement=back(item.statement), period=new_period), f"{v.period} to {new_period}"


def plant_wrong_scale(item: m.DataItem):
    """One scale step off: million to thousand, billion to million, thousand to million."""
    v = item.value
    if v is None or v.unit == "%" or v.scale not in _SCALE_DOWN:
        return None
    new = _SCALE_DOWN[v.scale]
    statement = re.sub(rf"\b{v.scale}(s?)\b", lambda mm: new + mm.group(1), item.statement, count=1,
                       flags=re.IGNORECASE)
    return _with(item, statement=statement, scale=new), f"{v.scale} to {new}"


def flip_not(s: str) -> str | None:
    """Drop the first 'not' or 'never', else add 'not' after the first auxiliary verb. None if neither."""
    mm = _NOT.search(s)
    if mm:
        return s[:mm.start()] + s[mm.end():]
    mm = _AUX.search(s)
    if mm:
        return s[:mm.end()] + " not" + s[mm.end():]
    return None


def plant_flipped_not(item: m.DataItem):
    """Flip a 'not' in the quote: 'did not reach' to 'did reach'. Table rows without a verb are skipped."""
    q = item.citations[_ev(item)].quote
    new_q = flip_not(q)
    if new_q is None:
        return None
    return _with(item, quote=new_q, statement=flip_not(item.statement) or item.statement), "a 'not' flipped in the quote"


KINDS = (
    ("digit swap", "quote_exact", plant_digit_swap),
    ("dropped minus", "value_in_quote", plant_dropped_minus),
    ("wrong year", "period_match", plant_wrong_year),
    ("wrong scale", "unit_context", plant_wrong_scale),
    ("flipped not", "quote_exact", plant_flipped_not),
)


@dataclass
class Plant:
    side: str
    kind: str
    target: str
    base: str
    change: str
    caught: bool
    target_hit: bool
    fired: list[str]
    detail: str


def spread(xs: list, n: int) -> list:
    """Up to n items, evenly spaced over xs, first and last included. Deterministic."""
    if n <= 0 or not xs:
        return []
    if len(xs) <= n:
        return list(xs)
    if n == 1:
        return [xs[0]]
    span = len(xs) - 1
    return [xs[i] for i in sorted({(2 * k * span + n - 1) // (2 * (n - 1)) for k in range(n)})]  # half up


def plant_side(side: str, checked: list[Checked], pages_by_doc, sources, per_kind: int = PER_KIND) -> list[Plant]:
    """Each kind into up to per_kind clean items (all four checks pass). Caught means a core check fails."""
    clean = [c for c in checked if c.item is not None and c.status == "pass"]
    out: list[Plant] = []
    for kind, target, fn in KINDS:
        cands = [(c, got) for c in clean if (got := fn(c.item)) is not None]
        for c, (planted, change) in spread(cands, per_kind):
            res = checks.check_data_item(planted, pages_by_doc, sources)
            fails = [r for r in res if r.check in CORE and r.result == "fail"]
            detail = fails[0].detail if fails else "all four checks pass"
            out.append(Plant(side, kind, target, c.label, change, bool(fails),
                             any(r.check == target for r in fails), [r.check for r in fails], detail))
    return out


# ---------------------------------------------------------------------------
# Key numbers for Gordon to hand-check
# ---------------------------------------------------------------------------

_LEAD = r"^(?:(?:siemens healthineers|shl|the group|the company)(?:'s|’s)?\s+)?(?:total\s+)?"
KEY_METRICS = (
    ("Revenue", _LEAD + r"revenue\b(?!\s+(?:growth|grew|increased))"),
    ("Comparable revenue growth", r"comparable revenue growth|revenue grew|comparable basis"),
    ("Adjusted EBIT", _LEAD + r"adjusted ebit\b(?!\s+margin)"),
    ("Adjusted EBIT margin", r"adjusted ebit margin"),
    ("EBIT", _LEAD + r"ebit\b"),
    ("Net income", _LEAD + r"net income\b(?!\s+attributable to non-controlling)"),
    ("Income before income taxes", r"income before income taxes"),
    ("Income tax expenses", _LEAD + r"income tax"),
    ("Earnings per share", r"earnings per share"),
    ("Free cash flow", r"free cash flow"),
    ("Cash flows from operating activities", r"cash flows? from operating activities"),
    ("Research and development", r"research and development"),
    ("Amortization", r"amorti[sz]ation"),
    ("Total assets", _LEAD + r"assets\b"),
    ("Cash and cash equivalents", _LEAD + r"cash and cash equivalents"),
    ("Goodwill", _LEAD + r"goodwill"),
    ("Long-term financial debt", r"long-term financial debt"),
    ("Short-term financial debt", r"short-term financial debt"),
    ("Net debt", r"\bnet debt\b"),
    ("Pensions", r"pension"),
    ("Lease liabilities", r"lease liabilit"),
    ("Total equity", _LEAD + r"equity\b"),
    ("Dividend", r"dividend"),
    ("Total current liabilities", _LEAD + r"current liabilities"),
)


@dataclass
class HandCheck:
    n: int
    metric: str
    item_id: str
    statement: str
    as_reported: str
    unit: str
    scale: str
    period: str
    source_id: str
    page: int
    quote: str
    baseline: str


def _years(v: m.Value | None) -> frozenset[int]:
    return frozenset(period_key(v.period)[0]) if v is not None and v.period else frozenset()


def _rank(c: Checked, order: dict[str, int]):
    ys = _years(c.item.value)
    return (-(max(ys) if ys else 0), order.get(c.item.source_id, 99), len(c.item.statement), c.item.id)


def same_span(a: m.DataItem, b: m.DataItem) -> bool:
    """Both evidence quotes overlap on the same stored page: the same table row or sentence."""
    ea, eb = a.citations[0], b.citations[0]
    la, lb = ea.locator, eb.locator
    return (ea.doc_id == eb.doc_id and isinstance(la, m.TextLocator) and isinstance(lb, m.TextLocator)
            and la.page == lb.page and la.char_start < lb.char_end and lb.char_start < la.char_end)


def same_number(a: m.DataItem, b: m.DataItem) -> bool:
    """Same unit and the same value in base units, wherever it is printed."""
    return a.value.unit == b.value.unit and checks.base_value(a) == checks.base_value(b)


def has_fact(item: m.DataItem, others: list[Checked]) -> bool:
    """The other side has a passing item for the same period, on the same row or sentence or with the same
    number on any page (a prior-year column, a summary table)."""
    return any(c.item is not None and c.item.value is not None and c.status == "pass"
               and _years(c.item.value) == _years(item.value)
               and (same_span(item, c.item) or same_number(item, c.item)) for c in others)


def coverage(mine: list[Checked], others: list[Checked]) -> tuple[int, int, list[Checked]]:
    """(found in others, passing numbers here, the ones the other side has no item for)."""
    passing = [c for c in mine if c.item is not None and c.item.value is not None and c.status == "pass"]
    missing = [c for c in passing if not has_fact(c.item, others)]
    return len(passing) - len(missing), len(passing), missing


def baseline_view(item: m.DataItem, baseline: list[Checked]) -> str:
    """What the plain report said for the same table row or sentence and period, by code."""
    same_row = [c for c in baseline if c.item is not None and c.item.value is not None and same_span(item, c.item)]
    for c in same_row:
        if _years(c.item.value) == _years(item.value):
            tail = "" if not c.failed("quote_exact") else ", quote not exact"
            if checks.base_value(c.item) == checks.base_value(item):
                return f"same ({c.label}{tail})"
            return f"differs: {c.item.value.as_reported} {c.item.value.scale} ({c.label}{tail})"
    if same_row:
        c = same_row[0]
        return f"same row, other period: {c.item.value.period} ({c.label})"
    elsewhere = next((c for c in baseline if c.item is not None and c.item.value is not None
                      and _years(c.item.value) == _years(item.value) and same_number(item, c.item)), None)
    if elsewhere is not None:
        b = elsewhere.item
        return f"same ({elsewhere.label}, from {b.source_id} p. {b.citations[0].locator.page})"
    return "not given"


def hand_check(harness: list[Checked], baseline: list[Checked], source_order: dict[str, int],
               limit: int = HAND_CHECK) -> list[HandCheck]:
    """Up to limit key numbers from harness items that pass all four checks: latest period first."""
    good = [c for c in harness if c.item is not None and c.item.value is not None and c.status == "pass"]
    picked: list[tuple[str, Checked]] = []
    used: set[str] = set()
    for label, rx in KEY_METRICS:
        cands = sorted((c for c in good if c.item.id not in used and re.search(rx, c.item.statement, re.IGNORECASE)),
                       key=lambda c: _rank(c, source_order))
        if cands:
            picked.append((label, cands[0]))
            used.add(cands[0].item.id)
        if len(picked) >= limit:
            break
    heads = {" ".join(c.item.statement.lower().split()[:3]) for _, c in picked}
    for c in sorted(good, key=lambda c: _rank(c, source_order)):
        if len(picked) >= limit:
            break
        head = " ".join(c.item.statement.lower().split()[:3])
        if c.item.id in used or head in heads:
            continue
        picked.append(("Other", c))
        used.add(c.item.id)
        heads.add(head)
    out = []
    for n, (label, c) in enumerate(picked, 1):
        it, v = c.item, c.item.value
        out.append(HandCheck(n, label, it.id, it.statement, v.as_reported, v.unit, v.scale, v.period or "",
                             it.source_id, it.citations[0].locator.page, text.normalize(it.citations[0].quote),
                             baseline_view(it, baseline)))
    return out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    date: str
    company: str
    run_id: str
    created_at: str
    simulated: bool
    refs: list[DocRef]
    model_small: str
    model_large: str
    harness: list[Checked]
    baseline: list[Checked]
    harness_usage: m.Usage
    baseline_usage: m.Usage
    run_usage: m.Usage
    harness_seconds: float
    baseline_seconds: float
    collect_seconds: float | None
    plants: dict[str, list[Plant]]
    hand: list[HandCheck]
    report: BaselineReport | None
    baseline_error: str | None
    gateway_allowed: int
    gateway_blocked: int
    per_kind: int
    harness_fixed: int
    sources: list[tuple[str, str, str | None]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABEL.format(date=self.date)

    @property
    def n_pages(self) -> int:
        return sum(len(r.pages) for r in self.refs)


def _sum_usage(agents) -> m.Usage:
    u = m.Usage()
    for a in agents:
        u = m.Usage(model_calls=u.model_calls + a.usage.model_calls, tokens_in=u.tokens_in + a.usage.tokens_in,
                    tokens_out=u.tokens_out + a.usage.tokens_out,
                    cost_usd=round(u.cost_usd + a.usage.cost_usd, 6))
    return u


def _local_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return iso[:10]


def analyse(state, raw: dict, per_kind: int = PER_KIND) -> Summary:
    """Pure code over the stored state and the saved baseline output: checks, plants, hand-check list."""
    pages_by_doc = st.readable_pages(state)
    sources = state.sources_by_id()
    refs = [DocRef(**r) for r in raw["refs"]]
    source_ids = [r.source_id for r in refs]
    base = raw["baseline"]
    report = BaselineReport.model_validate(base["report"]) if base.get("report") else None
    harness = check_harness(state, source_ids, pages_by_doc, sources)
    baseline = check_baseline(report, refs, pages_by_doc, sources, base["agent_id"]) if report else []
    agents = state.agents
    extractors = [a for a in agents.values() if a.role == "extractor"]
    b_agent = agents.get(base["agent_id"])
    order = {sid: i for i, sid in enumerate(raw.get("source_ids") or source_ids)}
    counts = state.run.counts
    return Summary(
        date=_local_date(raw["created_at"]), company=state.run.company, run_id=raw["run_id"], created_at=raw["created_at"],
        simulated=bool(raw.get("simulated")), refs=refs,
        model_small=next((a.model_id for a in extractors if a.model_id), "") or "",
        model_large=(b_agent.model_id if b_agent else "") or "",
        harness=harness, baseline=baseline, harness_usage=_sum_usage(extractors),
        baseline_usage=b_agent.usage if b_agent else m.Usage(), run_usage=state.run.usage,
        harness_seconds=float(raw.get("harness_seconds") or 0.0), baseline_seconds=float(base.get("seconds") or 0.0),
        collect_seconds=raw.get("collect_seconds"),
        plants={"harness": plant_side("harness", harness, pages_by_doc, sources, per_kind),
                "baseline": plant_side("baseline", baseline, pages_by_doc, sources, per_kind)},
        hand=hand_check(harness, baseline, order), report=report, baseline_error=base.get("error"),
        gateway_allowed=counts.gateway_allowed, gateway_blocked=counts.gateway_blocked, per_kind=per_kind,
        harness_fixed=sum(c.item.verification.status == "fixed" for c in harness),
        sources=[(s.source_id, s.status, s.error) for s in state.sources if s.source_id in source_ids])


def side_stats(checked: list[Checked]) -> dict[str, int]:
    return {
        "items": len(checked),
        "pass": sum(c.status == "pass" for c in checked),
        "fail_any": sum(c.status == "fail" for c in checked),
        "blocked": sum(c.status == "blocked" for c in checked),
        **{f"fail_{k}": sum(c.failed(k) for c in checked) for k in CORE},
        "page_off": sum(c.page_off for c in checked),
        "fail_numbers_traced": sum(c.failed("numbers_traced") for c in checked),
        "no_value": sum(c.item is not None and c.item.value is None for c in checked),
    }


def period_reasons(checked: list[Checked]) -> dict[str, int]:
    """Why period_match failed, from the check's own detail wording."""
    out = {"not named": 0, "other year": 0, "other": 0}
    for c in checked:
        for r in c.results:
            if r.check != "period_match" or r.result != "fail":
                continue
            if "does not mention" in r.detail:
                out["not named"] += 1
            elif "sits in value column" in r.detail or "sits next to" in r.detail:
                out["other year"] += 1
            else:
                out["other"] += 1
    return out


def caught(plants: list[Plant]) -> tuple[int, int]:
    return sum(p.caught for p in plants), len(plants)


def headline(s: Summary) -> str:
    hx, hy = caught(s.plants["harness"])
    b = side_stats(s.baseline)
    if s.report is None:
        return (f"Our checks caught {hx} of {hy} planted errors in the harness report. The plain Claude report "
                f"failed, so there is nothing to compare: {s.baseline_error}")
    return (f"Our checks caught {hx} of {hy} planted errors in the harness report. A plain Claude report gave "
            f"{b['items']} claims, and {b['fail_quote_exact']} of them failed our own quote check.")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(s: Any, n: int = 0) -> str:
    out = " ".join(str(s).split()).replace("|", "\\|")
    if n and len(out) > n:
        out = out[: n - 3] + "..."
    return out


def _plain(s: str) -> str:
    """Check details joined with '; ' read as sentences in this file."""
    return s.replace("; ", ". ")


def _ranges(pages: list[int]) -> str:
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else f"{pages[i]}-{pages[j]}")
        i = j + 1
    return ", ".join(out)


def _money(u: m.Usage) -> str:
    return f"${u.cost_usd:.4f}"


def _secs(x: float | None) -> str:
    return "n/a" if x is None else (f"{x:.0f} s" if x >= 10 else f"{x:.1f} s")


def _per_pass(u: m.Usage, n: int) -> str:
    return f"${u.cost_usd / n:.4f}" if n else "n/a"


def render_markdown(s: Summary) -> str:
    h, b = side_stats(s.harness), side_stats(s.baseline)
    hc, bc = coverage(s.harness, s.baseline), coverage(s.baseline, s.harness)
    sim = s.simulated
    lines = ["# Baseline evaluation", "", f"**{s.label}**", ""]
    if sim:
        lines += ["**Simulated: model answers came from offline fakes (FAKE_MODELS=1), so the numbers below test "
                  "the code, not Claude.**", ""]
    lines += [headline(s), ""]
    if s.report is not None:
        lines += [f"{b['fail_any']} of the {b['items']} plain claims and {h['fail_any']} of the {h['items']} harness "
                  "items failed at least one of the four checks.", ""]
    lines += ["This compares two ways of reading the same pages. It is one run with one prompt, not a benchmark. "
              f"Run {s.run_id}, started {s.created_at}.", "", "## Setup", ""]
    lines.append(f"- Pages: the same {s.n_pages} pages the harness reads (`extract_pages` in `data/shl/plan.json`), "
                 "fetched through the gateway into a throwaway local run. Stored pages were never edited.")
    for r in s.refs:
        lines.append(f"  - {r.source_id} ({r.tag}): {_cell(r.title)}, pages {_ranges(r.pages)}")
    for sid, status, err in s.sources:
        if status not in ("fetched", "extracting", "extracted"):
            lines.append(f"  - {sid} was not read: {status}" + (f" ({_cell(err, 120)})" if err else ""))
    lines += [
        f"- Harness: one small-model extractor per page ({s.model_small or 'small model'}). Code locates every quote, "
        "assigns the ids and runs the checks. A near-miss quote gets one re-cite by code from the closest real span.",
        f"- Plain Claude: one large-model call ({s.model_large or 'large model'}) with all {s.n_pages} pages and an "
        f"analyst's prompt: write a BOFFIN screen of {s.company} from these pages, cite page numbers and "
        "quote the numbers you use. It was asked for structured claims (value, unit, period, page and quote) so our "
        "checks could run on them. No harness: no page-by-page agents, no re-cite, no repair. The call still went "
        "through the gateway, so it was metered like every other call.",
        "- Our checks, the same code on both sides: quote_exact, value_in_quote, unit_context and period_match. For "
        "both sides, code took the unit and period header lines above each found quote, so a table value does not "
        "fail just for a missing header.",
        "", "## Results", "",
        "| | Harness | Plain Claude |", "|---|---|---|",
        f"| Items or claims | {h['items']} | {b['items']} |",
        f"| Pass all four checks | {h['pass']} | {b['pass']} |",
        f"| Fixed by the code re-cite | {s.harness_fixed} | not used |",
        f"| Failed quote_exact | {h['fail_quote_exact']} | {b['fail_quote_exact']} |",
        f"| Failed value_in_quote | {h['fail_value_in_quote']} | {b['fail_value_in_quote']} |",
        f"| Failed unit_context | {h['fail_unit_context']} | {b['fail_unit_context']} |",
        f"| Failed period_match | {h['fail_period_match']} | {b['fail_period_match']} |",
        f"| Failed any of the four | {h['fail_any']} | {b['fail_any']} |",
        f"| Could not be checked (blocked) | {h['blocked']} | {b['blocked']} |",
        f"| Quote found, but on another page than cited | n/a | {b['page_off']} |",
        f"| Statement has a number its quote does not hold | {h['fail_numbers_traced']} | {b['fail_numbers_traced']} |",
        f"| Facts without a number | {h['no_value']} | {b['no_value']} |",
        f"| Model calls | {s.harness_usage.model_calls} | {s.baseline_usage.model_calls} |",
        f"| Tokens in / out | {s.harness_usage.tokens_in:,} / {s.harness_usage.tokens_out:,} | "
        f"{s.baseline_usage.tokens_in:,} / {s.baseline_usage.tokens_out:,} |",
        f"| Cost (gateway meter) | {_money(s.harness_usage)} | {_money(s.baseline_usage)} |",
        f"| Wall time | {_secs(s.harness_seconds)} | {_secs(s.baseline_seconds)} |",
        f"| Cost per item that passes all four | {_per_pass(s.harness_usage, h['pass'])} | "
        f"{_per_pass(s.baseline_usage, b['pass'])} |",
        f"| Passing numbers the other side also has (same period and row, or same value) | {hc[0]} of {hc[1]} | "
        f"{bc[0]} of {bc[1]} |",
        "",
        _period_line(s), "",
        f"Fetching the pages took {_secs(s.collect_seconds)} and is shared by both sides. The whole run spent "
        f"{_money(s.run_usage)} on {s.run_usage.model_calls} model calls. The gateway allowed {s.gateway_allowed} "
        f"requests and blocked {s.gateway_blocked}.",
    ]
    if s.baseline_error:
        lines += ["", f"The plain Claude call did not return a report: {_cell(s.baseline_error, 300)}"]
    lines += _plants_section(s)
    lines += _failures_section(s)
    lines += _missed_section(s, bc[2])
    lines += _scores_section(s)
    lines += _hand_section(s)
    lines += [
        "", "## Limits", "",
        "- One run, one prompt, one model call. A different prompt or model could do better or worse.",
        "- The harness side here is extract only, the data layer of the agent report. Draft, judges and repair "
        "are not part of this comparison.",
        "- The planted set is five kinds of slip, planted by code into copies of items that passed. Kinds a side "
        "had no eligible item for are planted fewer times or not at all.",
        "- Known checker limit: a dash followed by a space before a number can be a range dash, so a dropped minus "
        "there is accepted, not caught.",
        "- An exact quote proves the page says it. It does not prove the number is the right one for the question.",
        "", "## Rerun", "", "From the repo root:", "", "```",
        ".venv/Scripts/python backend/scripts/baseline.py --keep",
        ".venv/Scripts/python backend/scripts/baseline.py --reuse backend/.data/baseline", "```", "",
        "The first line fetches, extracts and makes the one baseline call (live Bedrock, about $1). The second "
        "recomputes this file from a kept run with no model calls.", ""]
    return "\n".join(lines)


def _period_line(s: Summary) -> str:
    hr, br = period_reasons(s.harness), period_reasons(s.baseline)
    line = (f"Why period_match failed. The quote and its header do not name the period: harness {hr['not named']}, "
            f"plain {br['not named']}. The value sits under another year in the quote or table: harness "
            f"{hr['other year']}, plain {br['other year']}.")
    if hr["other"] or br["other"]:
        line += f" Other reasons: harness {hr['other']}, plain {br['other']}."
    return line + (" The first kind is often a true value whose year is printed elsewhere on the page, so the "
                   "check is strict there, the same way on both sides.")


def _plants_section(s: Summary) -> list[str]:
    hp, bp = s.plants["harness"], s.plants["baseline"]
    lines = ["", "## Planted errors", "",
             f"Code planted each kind of error into copies of up to {s.per_kind} clean items per side (items that "
             "pass all four checks), spread evenly over the items. The same kinds as the B-verify planted set. "
             "Caught means at least one of the four checks fails on the planted copy.", "",
             "| Error | Target check | Harness caught | Plain Claude caught |", "|---|---|---|---|"]
    for kind, target, _ in KINDS:
        hx, hy = caught([p for p in hp if p.kind == kind])
        bx, by = caught([p for p in bp if p.kind == kind])
        lines.append(f"| {kind} | {target} | {hx} of {hy} | {bx} of {by} |")
    hx, hy = caught(hp)
    bx, by = caught(bp)
    lines += [f"| Total | | {hx} of {hy} | {bx} of {by} |", "",
              "A plain Claude report has no checks of its own. Without ours it would catch none of these.", ""]
    missed = [p for p in hp + bp if not p.caught]
    off = [p for p in hp + bp if p.caught and not p.target_hit]
    if missed:
        lines += ["Missed:", ""]
        lines += [f"- {p.side} {p.base}, {p.kind} ({_cell(p.change)}): {_cell(_plain(p.detail), 200)}" for p in missed]
        lines.append("")
    if off:
        lines += ["Caught by another check than the target:", ""]
        lines += [f"- {p.side} {p.base}, {p.kind}: {', '.join(p.fired)}" for p in off]
        lines.append("")
    return lines


def _failures_section(s: Summary, limit: int = 30) -> list[str]:
    bad = [c for c in s.baseline if c.status != "pass"]
    lines = ["## Plain Claude claims that did not pass", ""]
    if not bad:
        return lines + ["None.", ""]
    lines += ["| Claim | Statement | Check | Why |", "|---|---|---|---|"]
    for c in bad[:limit]:
        first = next((r for r in c.core() if r.result != "pass"), None) or c.results[0]
        stmt = c.item.statement if c.item is not None else c.note
        lines.append(f"| {c.label} | {_cell(stmt, 110)} | {first.check} | {_cell(_plain(first.detail), 220)} |")
    if len(bad) > limit:
        lines.append(f"| ... | {len(bad) - limit} more in results.json | | |")
    hbad = [c for c in s.harness if c.status != "pass"]
    lines += ["", f"Harness items that did not pass: {len(hbad)}" + (" (listed in results.json)." if hbad else "."), ""]
    return lines


def _missed_section(s: Summary, missing: list[Checked], limit: int = 30) -> list[str]:
    """Passing plain claims the harness has no passing item for (see has_fact): its coverage gaps."""
    if s.report is None:
        return []
    lines = ["## Plain Claude numbers the harness has no item for", ""]
    if not missing:
        return lines + ["None. Every passing plain claim has a harness item for the same row and period.", ""]
    lines += ["These plain claims pass all four checks, but no passing harness item has the same period on the "
              "same row or sentence, or the same value on any page. The extractors only take what the plan's data "
              "needs ask for, so some of these are left out by design. The rest are numbers the page-by-page "
              "extractors missed.", "",
              "| Claim | Statement | Source, page | Quote |", "|---|---|---|---|"]
    for c in missing[:limit]:
        ev = c.item.citations[0]
        lines.append(f"| {c.label} | {_cell(c.item.statement, 110)} | {c.item.source_id} p. {ev.locator.page} | "
                     f"{_cell(text.normalize(ev.quote), 120)} |")
    if len(missing) > limit:
        lines.append(f"| ... | {len(missing) - limit} more in results.json | | |")
    return lines + [""]


def _scores_section(s: Summary) -> list[str]:
    if s.report is None:
        return []
    lines = ["## The plain report's own scores (not checked by code)", ""]
    if s.report.summary:
        lines += [_cell(s.report.summary, 900), ""]
    lines += ["| Criterion | Score | Reasoning |", "|---|---|---|"]
    for c in s.report.criteria:
        score = "empty" if c.score is None else (f"{c.score:+d}" if c.score else "0")
        lines.append(f"| {_cell(c.criterion)} | {score} | {_cell(c.reasoning, 300)} |")
    lines += ["", "No code checked these scores or the arithmetic in their reasoning. In the harness, scores cite "
                  "conclusions, and code checks computations and rubric thresholds."]
    rs = next((c for c in s.report.criteria if c.criterion == "research_stage"), None)
    if rs is not None and rs.score is not None:
        lines.append("The plain report scored research_stage. The rubric says that criterion is about the analyst's "
                     "own coverage history, which no page can state. The harness leaves it as a declared gap.")
    return lines + [""]


def _hand_section(s: Summary) -> list[str]:
    lines = ["## Key numbers to hand-check", "",
             f"{len(s.hand)} numbers picked by code from harness items that pass all four checks, latest period "
             "first. Page is the 1-based page in the PDF file, not the printed page label. The last column says "
             "what the plain Claude report gave for the same row or sentence and period.", "",
             "| # | What | Value | Period | Source, page | Quote | Plain report |", "|---|---|---|---|---|---|---|"]
    for hc in s.hand:
        unit = "" if hc.unit == "%" and hc.as_reported.strip().endswith("%") else f" {hc.unit}"
        value = f"{hc.as_reported}{unit}" + (f" {hc.scale}" if hc.scale != "one" else "")
        lines.append(f"| {hc.n} | {_cell(hc.statement, 90)} ({hc.item_id}) | {_cell(value)} | {_cell(hc.period)} | "
                     f"{hc.source_id} p. {hc.page} | {_cell(hc.quote, 160)} | {_cell(hc.baseline)} |")
    return lines


def _checked_json(c: Checked) -> dict:
    it = c.item
    return {"label": c.label, "status": c.status, "statement": it.statement if it else None,
            "value": it.value.model_dump(mode="json") if it and it.value else None,
            "doc_id": it.citations[0].doc_id if it else None, "cited_page": c.cited_page, "found_page": c.found_page,
            "quote": it.citations[0].quote if it else None, "note": c.note or None,
            "not_passing": [{"check": r.check, "result": r.result, "detail": r.detail}
                            for r in c.results if r.result != "pass"]}


def render_json(s: Summary) -> str:
    report = s.report.model_dump(mode="json") if s.report else None
    doc = {
        "label": s.label, "simulated": s.simulated, "date": s.date, "run_id": s.run_id, "created_at": s.created_at,
        "headline": headline(s), "models": {"harness": s.model_small, "baseline": s.model_large},
        "documents": [asdict(r) for r in s.refs], "pages": s.n_pages, "sources": s.sources,
        "period_match_reasons": {"harness": period_reasons(s.harness), "baseline": period_reasons(s.baseline)},
        "coverage": {side: {"also_on_other_side": got, "passing_numbers": total,
                            "missing_on_other_side": [c.label for c in missing]}
                     for side, (got, total, missing) in (("harness", coverage(s.harness, s.baseline)),
                                                         ("baseline", coverage(s.baseline, s.harness)))},
        "harness": {"stats": side_stats(s.harness), "fixed_by_recite": s.harness_fixed,
                    "usage": s.harness_usage.model_dump(mode="json"), "seconds": s.harness_seconds},
        "baseline": {"stats": side_stats(s.baseline), "usage": s.baseline_usage.model_dump(mode="json"),
                     "seconds": s.baseline_seconds, "error": s.baseline_error,
                     "summary": report["summary"] if report else None,
                     "criteria": report["criteria"] if report else None},
        "run_usage": s.run_usage.model_dump(mode="json"), "collect_seconds": s.collect_seconds,
        "gateway": {"allowed": s.gateway_allowed, "blocked": s.gateway_blocked},
        "plants": [asdict(p) for side in ("harness", "baseline") for p in s.plants[side]],
        "baseline_claims": [_checked_json(c) for c in s.baseline],
        "harness_not_passing": [_checked_json(c) for c in s.harness if c.status != "pass"],
        "hand_check": [asdict(h) for h in s.hand],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sources", default=",".join(SOURCE_IDS), help="source ids from plan.json (default S-001 to S-005)")
    p.add_argument("--max-pages", type=int, help="read at most this many planned pages per source (a cheap smoke)")
    p.add_argument("--budget", type=float, default=2.0, help="run budget in USD, enforced by the gateway (default 2.0)")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help=f"baseline output cap (default {MAX_TOKENS})")
    p.add_argument("--max-claims", type=int, default=MAX_CLAIMS, help=f"claims the prompt asks for (default {MAX_CLAIMS})")
    p.add_argument("--per-kind", type=int, default=PER_KIND, help=f"plants per error kind per side (default {PER_KIND})")
    p.add_argument("--fake", action="store_true", help="simulated fake models (pages are still fetched)")
    p.add_argument("--plan", default=str(PLAN_PATH), help="plan.json path")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="run folder root (keep it short on Windows)")
    p.add_argument("--keep", action="store_true", help="keep the run folder for --reuse")
    p.add_argument("--reuse", metavar="DATA_DIR", help="recompute from the newest kept run there, no model calls")
    p.add_argument("--out", help="results.md path (default data/baseline/results.md, stdout when simulated)")
    return p


async def _live(args, plan: dict, source_ids: list[str], made: list[Path]):
    data_dir = Path(args.data_dir)
    ctx = build_context(plan, source_ids, data_dir=data_dir, budget=args.budget, fake=args.fake,
                        max_pages=args.max_pages)
    made.append(data_dir / "runs" / ctx.run_id)
    mode = "FAKE models (simulated)" if ctx.settings.fake_models else "live Bedrock through the gateway"
    print(f"run {ctx.run_id}: {', '.join(source_ids)} ({mode}), budget ${args.budget:.2f}, folder {made[-1]}")
    collect_seconds = await run_stage(ctx, "collect")
    for s in ctx.state.sources:
        print(f"  {s.source_id} {s.status}, {s.stats.pages} page(s)" + (f": {s.error}" if s.error else ""))
    print("extract (harness), then the one baseline call ...")
    raw = await compare(ctx, source_ids, max_tokens=args.max_tokens, max_claims=args.max_claims,
                        collect_seconds=collect_seconds)
    return ctx.state, raw


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    made: list[Path] = []
    try:
        if args.reuse:
            state, raw = load_kept(Path(args.reuse))
        else:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            source_ids = [x.strip() for x in args.sources.split(",") if x.strip()]
            state, raw = asyncio.run(_live(args, plan, source_ids, made))
        summary = analyse(state, raw, per_kind=args.per_kind)
        md, js = render_markdown(summary), render_json(summary)
        out = Path(args.out) if args.out else (None if summary.simulated else DEFAULT_OUT)
        if out is None:
            print(md)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md, encoding="utf-8")
            out.with_suffix(".json").write_text(js, encoding="utf-8")
            print(f"wrote {out} and {out.with_suffix('.json').name}")
    except (OSError, ValueError, KeyError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        for folder in made:
            if args.keep:
                print(f"kept the run under {folder}")
            else:
                shutil.rmtree(folder, ignore_errors=True)
    u = summary.run_usage
    print(headline(summary))
    print(f"Spent ${u.cost_usd:.4f} on {u.model_calls} model call(s): harness {_money(summary.harness_usage)}, "
          f"baseline {_money(summary.baseline_usage)}" + (" (simulated)" if summary.simulated else ""))
    return 0 if summary.baseline_error is None else 1


if __name__ == "__main__":
    sys.exit(main())
