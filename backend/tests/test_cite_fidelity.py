"""B-fix-cite: the citation invariant, on stored runs and on items extracted again from their stored pages.

The invariant. A text citation whose quote_exact passes stores exactly page[char_start:char_end], and a unit
or period header sits on its evidence's page (a release's cover-page period aside). A failing quote keeps
the agent's text on purpose: its diff is in the failed check, and its item is never pass or fixed.

Stored runs are read only, never written:
- backend/data/replay/run_20261108_pin2ve, the G5 run (in the repo),
- backend/.data/r0b and backend/.data/r0, the local rehearsal runs (not committed: tests that need them skip).
They are recorded, so their counts cannot change. Run with -s to see them.

The replay tests extract again, offline: a fake extractor (simulated, no Bedrock) answers each page with
what the live model answered in the recorded run (its transcript in 00-Meta/agents/), and the real extract
stage builds and checks the items on the run's real stored pages.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from backend.axelrod import bedrock
from backend.axelrod.agents import extractor as ex
from backend.axelrod.agents.base import register_fake
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import extract as st
from backend.axelrod.store import Store
from backend.axelrod.verify import checks

REPO = Path(__file__).resolve().parents[2]
RUNS = {
    "pin2ve": REPO / "backend" / "data" / "replay" / "run_20261108_pin2ve",
    "r0b": REPO / "backend" / ".data" / "r0b" / "runs" / "run_20261108_whhj0d",
    "r0": REPO / "backend" / ".data" / "r0" / "runs" / "run_20261108_0bsu0o",
}
# What the recorded runs hold (before B-fix-cite): passing quotes that differ from their span, and headers
# left on another page than their evidence.
RECORDED_BROKEN = {
    "pin2ve": set(),
    "r0b": {"D-135 evidence", "D-159 evidence"},
    "r0": {"D-108 period_context", "D-109 period_context", "D-110 period_context"},
}
RECORDED_OFF_PAGE = {"pin2ve": 0, "r0b": 4, "r0": 13}


@pytest.fixture(autouse=True)
def _restore_fakes():
    saved = dict(bedrock._FAKES)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


def stored(name: str) -> dict:
    path = RUNS[name] / "state.json"
    if not path.exists():
        pytest.skip(f"stored run {name} is not on this machine ({path})")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@dataclass
class Walk:
    citations: int = 0
    broken: list[str] = field(default_factory=list)     # passing quotes that differ from their span
    agent_text: int = 0                                  # failing quotes that keep the agent's text (allowed)
    off_page: list[str] = field(default_factory=list)   # headers on another page than their evidence

    def line(self, name: str) -> str:
        return (f"{name}: {self.citations} text citations, {len(self.broken)} passing quote(s) differ from their "
                f"span, {len(self.off_page)} header(s) off the evidence page, {self.agent_text} failing quote(s) "
                f"keep the agent's text")


def walk(items, pages: dict[str, list[str]], sources: dict[str, m.Source]) -> Walk:
    """Every text citation of every data item against the invariant."""
    w = Walk()
    for it in items:
        if it.kind != "data":
            continue
        qe = [c for c in it.verification.checks if c.check == "quote_exact"]
        text_cits = [c for c in it.citations if isinstance(c.locator, m.TextLocator)]
        ev_pages = checks.evidence_pages(it)
        src = sources.get(it.source_id)
        for k, c in enumerate(text_cits):
            w.citations += 1
            passed = k < len(qe) and qe[k].result == "pass"
            if checks.span_text(pages, c.doc_id, c.locator) != c.quote:
                if passed or it.verification.status in ("pass", "fixed"):
                    w.broken.append(f"{it.id} {c.role}")
                else:
                    w.agent_text += 1
            allowed = checks.context_pages(c, ev_pages, src)
            if allowed is not None and c.locator.page not in allowed:
                w.off_page.append(f"{it.id} {c.role}")
    return w


def walk_stored(s: dict) -> Walk:
    items = [TypeAdapter(m.Item).validate_python(v) for v in s["items"].values()]
    sources = {x["source_id"]: m.Source.model_validate(x) for x in s["sources"]}
    return walk(items, s["pages"], sources)


@pytest.mark.parametrize("name", list(RUNS))
def test_stored_runs_before_the_fix(name: str) -> None:
    """The recorded runs as they are: the breaks the G3 verifier found, and none in the G5 run."""
    w = walk_stored(stored(name))
    print("\nrecorded " + w.line(name))
    assert set(w.broken) == RECORDED_BROKEN[name]
    assert len(w.off_page) == RECORDED_OFF_PAGE[name]


# ---------------------------------------------------------------------------
# Replay: the recorded extractor answers through today's extract stage
# ---------------------------------------------------------------------------


def recorded_answers(run_dir: Path) -> dict[tuple[str, int], dict]:
    """(source id, page) to the extractor's last answer, from the run's transcripts."""
    out: dict[tuple[str, int], dict] = {}
    for f in run_dir.glob("**/00-Meta/agents/extractor-*.jsonl"):
        task, answer = None, None
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "spawned":
                task = rec.get("task")
            elif rec.get("kind") == "response" and isinstance(rec.get("tool_input"), dict):
                answer = rec["tool_input"]
        mm = re.fullmatch(r"Extract (S-\d{3}) page (\d+)", task or "")
        if mm and answer is not None:
            out[(mm.group(1), int(mm.group(2)))] = answer
    return out


def replay_fake(answers: dict[tuple[str, int], dict], sources: list[m.Source]):
    """A fake extractor (simulated) that answers a page's prompt with the recorded answer for that page."""
    by_title = {x.title: x.source_id for x in sources}

    def fn(system: str, user: str, schema: dict) -> dict:
        title = re.search(r"^Source: (.*) \([^()]*\)\.$", user, re.M).group(1)
        page = int(re.search(r"^This is (?:PDF page|text section) (\d+) of", user, re.M).group(1))
        return answers.get((by_title[title], page), {"items": []})

    return fn


async def replay(settings, name: str, only: set[str] | None = None) -> tuple[RunContext, dict]:
    """A fresh run with the stored run's plan, sources, documents and pages, extracted again."""
    s = stored(name)
    sources = [m.Source.model_validate(x) for x in s["sources"] if only is None or x["source_id"] in only]
    docs = [m.Document.model_validate(d) for d in s["documents"] if only is None or d["source_id"] in only]
    register_fake(ex.TOOL, replay_fake(recorded_answers(RUNS[name]), sources))
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=s["run"]["company"], ticker=s["run"]["ticker"]))
    state.run.stage = "extract"
    state.needs = [m.DataNeed.model_validate(n) for n in s["needs"]]
    state.extract_pages, state.need_pages, state.fiscal_note = s["extract_pages"], s["need_pages"], s["fiscal_note"]
    state.sources, state.documents = sources, docs
    state.pages = {d.doc_id: s["pages"][d.doc_id] for d in docs}
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    await st.run(ctx)
    return ctx, s


def _key(it: dict | m.DataItem) -> tuple:
    if isinstance(it, dict):
        v = it.get("value") or {}
        return it["source_id"], it["statement"], v.get("as_reported"), v.get("period")
    v = it.value
    return it.source_id, it.statement, v.as_reported if v else None, v.period if v else None


def status_moves(s: dict, items: list[m.DataItem], only: set[str] | None = None) -> set[tuple]:
    """Item keys whose status differs between the recorded run and the replay."""
    old = Counter((_key(v), v["verification"]["status"]) for v in s["items"].values()
                  if v["kind"] == "data" and (only is None or v["source_id"] in only))
    new = Counter((_key(i), i.verification.status) for i in items)
    return {k for k, _ in (old - new) + (new - old)}


async def test_replay_g5_run_keeps_the_invariant(settings) -> None:
    """Every page of the G5 run, extracted again: the same items and statuses, and no citation breaks."""
    ctx, s = await replay(settings, "pin2ve")
    items = ctx.state.data_items()
    sources = ctx.state.sources_by_id()
    w = walk(items, ctx.state.pages, sources)
    print("\nreplayed " + w.line("pin2ve") + f", statuses {dict(Counter(i.verification.status for i in items))}")
    assert w.citations > 600 and not w.broken and not w.off_page
    recorded = Counter(v["verification"]["status"] for v in s["items"].values() if v["kind"] == "data")
    assert Counter(i.verification.status for i in items) == recorded
    assert not status_moves(s, items)


async def test_replay_r0b_release_moves_its_headers_with_the_evidence(settings) -> None:
    """R0b's release (S-002) extracted again: D-135 and D-159 (evidence found on page 6, headers left on page
    13) now take their headers from page 6 and are 'fixed' by code. Every other item keeps its status."""
    ctx, s = await replay(settings, "r0b", only={"S-002"})
    items = ctx.state.data_items()
    w = walk(items, ctx.state.pages, ctx.state.sources_by_id())
    print("\nreplayed " + w.line("r0b S-002"))
    assert not w.broken and not w.off_page
    moved = status_moves(s, items, only={"S-002"})
    assert {k[1] for k in moved} == {"Revenue was €23,630 million in fiscal year 2026.",
                                     "Revenue was €23,375 million in fiscal year 2025."}
    revenue = [i for i in items if i.statement in {k[1] for k in moved} and i.verification.status == "fixed"]
    assert len(revenue) == 2
    for it in revenue:
        assert {c.locator.page for c in it.citations} == {6}
        assert {c.role: c.quote for c in it.citations} == {
            "evidence": "Revenue \n23,630 \n23,375", "unit_context": "(in millions of €)", "period_context": "2026 \n2025"}
