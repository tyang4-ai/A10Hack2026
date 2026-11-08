"""B-live: the revoke cascade, the live collect order and pacing, the _period_text fix, and an offline rehearsal of
scripts/demo_live.py against a real uvicorn.

Offline throughout. Model answers come from the FAKE_MODELS fakes (simulated), the Guardrails scan is a fake
(simulated), and every source body except the planted test page is a canned stand-in served by a patched HTTP
layer under the real Gateway. The planted page itself comes from the API's own /test-pages route. Every server a
test starts is stopped by it.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import itertools
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend.axelrod import bedrock, cascade, orchestrator
from backend.axelrod.agents import extractor
from backend.axelrod.api import create_app
from backend.axelrod.config import BACKEND_DIR, Settings
from backend.axelrod.contract import m
from backend.axelrod.fetch import http as fetch_http
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import collect
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import Store
from backend.axelrod.verify import checks

P = "/api/v1"
ROUTE = "/test-pages/injection.html"
FLAG = "ignore all previous instructions"


def _load_demo_live():
    script = BACKEND_DIR / "scripts" / "demo_live.py"
    spec = importlib.util.spec_from_file_location("demo_live", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_live"] = module  # dataclasses look their module up in sys.modules
    spec.loader.exec_module(module)
    return module


demo_live = _load_demo_live()


# ---------------------------------------------------------------------------
# _period_text: % change columns in a row of amounts
# ---------------------------------------------------------------------------

ROW = "Revenue 23,375 22,363 4.5% 5.9%"
HEADER = "2025 2024 Act. Comp."


def _value(as_reported: str, period: str) -> m.Value:
    pct = as_reported.endswith("%")
    number = float(as_reported.rstrip("%").replace(",", ""))
    return m.Value(number=number, as_reported=as_reported, unit="%" if pct else "EUR",
                   scale="one" if pct else "million", period=period)


@pytest.mark.parametrize("as_reported,period", [("4.5%", "FY2025"), ("5.9%", "FY2025"), ("4.5%", "FY2024")])
def test_percent_change_in_a_row_of_amounts_is_not_read_as_a_year_column(as_reported, period):
    v = _value(as_reported, period)
    with_header = checks._period_text(v, [ROW], [HEADER])
    inline = checks._period_text(v, [f"{HEADER} {ROW}"], [])
    for res in (with_header, inline):
        assert res.result == "pass", res.detail
        assert "column" not in res.detail                    # no claim that 4.5% sits in the 2025 or 2024 column


@pytest.mark.parametrize("as_reported,period,result", [("23,375", "FY2025", "pass"), ("22,363", "FY2024", "pass"),
                                                       ("22,363", "FY2025", "fail")])
def test_amounts_in_the_same_row_still_align_to_their_year(as_reported, period, result):
    res = checks._period_text(_value(as_reported, period), [ROW], [HEADER])
    assert res.result == result, res.detail
    inline = checks._period_text(_value(as_reported, period), [f"{HEADER} {ROW}"], [])
    assert inline.result == result, inline.detail


def test_a_row_of_percentages_alone_still_aligns_to_years():
    row, header = "Adjusted EBIT margin 17.1% 16.5%", "2025 2024"
    assert checks._period_text(_value("17.1%", "FY2025"), [row], [header]).result == "pass"
    wrong = checks._period_text(_value("16.5%", "FY2025"), [row], [header])
    assert wrong.result == "fail" and "header labels 2024" in wrong.detail


# ---------------------------------------------------------------------------
# Revoke cascade (over the mock's fully verified report, a fictional company)
# ---------------------------------------------------------------------------


def fixture_ctx(settings: Settings, bundle: dict, *, final: bool = True) -> RunContext:
    """A run holding the mock report: every source approved and extracted, policy built, the brief when final."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Fixture Co", ticker="FIX", created_via="api"))
    state.sources = [m.Source.model_validate(s) for s in copy.deepcopy(bundle["sources"])]
    for s in state.sources:
        s.approval = "approved" if s.tier == "priority" else "auto_approved"
        s.status = "extracted"
    report = m.AgentReport.model_validate(copy.deepcopy(bundle["report"]))
    state.items = {i.id: i for i in report.items}
    state.holes = list(report.holes)
    if final:
        state.rollup = report.rollup
        state.report_ready = True
        state.summary = m.HumanReport.model_validate(copy.deepcopy(bundle["summary"]))
    state.approval_request = m.ApprovalRequest(decisions=[
        m.SourceDecision(source_id=s.source_id, approval="approved") for s in state.sources if s.tier == "priority"])
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    return ctx


def revoke(ctx: RunContext, sid: str) -> tuple[cascade.CascadeResult, list[m.Event]]:
    """Gateway.revoke, then the cascade, the way the API route does it. Returns the events the cascade emitted."""
    ctx.gateway.revoke(sid, "test revoke")
    mark = ctx.state.run.last_seq
    res = cascade.revoke_cascade(ctx, sid)
    return res, [e for e in ctx.state.events if e.seq > mark]


def resolve(item: m.Item) -> m.CheckResult:
    return next(c for c in item.verification.checks if c.check == "citations_resolve")


def test_affected_walks_data_then_dependents_nearest_first(fixture_bundle):
    items = {i.id: i for i in m.AgentReport.model_validate(fixture_bundle["report"]).items}
    assert cascade.affected(items, "S-006") == ["D-037", "C-009"]
    assert cascade.affected(items, "S-005") == ["D-035", "D-036", "C-002", "E-003"]
    s002 = cascade.affected(items, "S-002")
    assert s002.index("D-022") < s002.index("C-001") < s002.index("E-001") and s002[-1] == "G-002"
    assert cascade.affected(items, "S-404") == []


def test_revoke_blocks_the_sources_items_and_their_dependents(settings, fixture_bundle):
    ctx = fixture_ctx(settings, fixture_bundle)
    before = {iid: it.model_copy(deep=True) for iid, it in ctx.state.items.items()}
    res, evs = revoke(ctx, "S-006")

    assert res.item_ids == ["D-037", "C-009"] and res.criteria == []
    d = ctx.state.items["D-037"]
    assert d.verification.status == "blocked"
    assert resolve(d).result == "blocked" and resolve(d).detail == "source revoked: S-006"
    c = ctx.state.items["C-009"]
    assert resolve(c).result == "blocked" and "D-037: source revoked: S-006" in resolve(c).detail
    assert c.verification.status == "fail"                   # it was failing already: fail outranks blocked
    for iid in ("D-037", "C-009"):
        cert = ctx.state.items[iid].certainty
        assert cert is not None and cert.band == "none" and cert.score is None
    other = [iid for iid in before if iid not in ("D-037", "C-009")]
    assert all(ctx.state.items[i].model_dump() == before[i].model_dump() for i in other)

    verified = [e for e in evs if e.type == "item.verified"]
    assert [e.data.item_id for e in verified] == ["D-037", "C-009"]
    assert verified[0].data.verification.status == "blocked" and "S-006" in verified[0].message
    updated = [e for e in evs if e.type == "item.updated"]
    if cascade.update_reason_allowed():                      # once the contract lists "source_revoked"
        assert [e.data.item.id for e in updated] == ["D-037", "C-009"]
        assert all(evs.index(u) < evs.index(v) for u, v in zip(updated, verified))
    else:
        assert updated == []
    # H-014 cites C-009, so the brief sentence is re-checked too.
    assert res.sentence_ids == ["H-014"]


def test_final_report_names_the_rollup_criteria_and_the_brief_follows(settings, fixture_bundle):
    ctx = fixture_ctx(settings, fixture_bundle)
    rollup_before = ctx.state.rollup.model_dump()
    passed_before = ctx.state.summary.stats.passed
    res, evs = revoke(ctx, "S-005")

    assert res.item_ids == ["D-035", "D-036", "C-002", "E-003"]
    e = ctx.state.items["E-003"]
    assert e.verification.status == "blocked"
    assert "C-002: source revoked: S-005" in resolve(e).detail
    assert f"The final rollup still counts {e.criterion} (0)" in resolve(e).detail
    assert res.criteria == [e.criterion]
    assert ctx.state.rollup.model_dump() == rollup_before   # finalize is not re-run
    assert res.sentence_ids == ["H-006"]
    h = next(s for s in cascade._sentences(ctx.state.summary) if s.id == "H-006")
    assert h.verification.status == "blocked" and h.certainty.band == "none"
    assert "source revoked: S-005" in resolve(h).detail
    assert ctx.state.summary.stats.passed == passed_before - 1
    sv = [x for x in evs if x.type == "summary.verified"]
    assert [x.data.sentence_id for x in sv] == ["H-006"]
    assert evs.index(sv[0]) > max(evs.index(x) for x in evs if x.type == "item.verified")


def test_a_chart_over_a_revoked_source_goes_blocked(settings, fixture_bundle):
    ctx = fixture_ctx(settings, fixture_bundle, final=False)
    res, _ = revoke(ctx, "S-002")
    g = ctx.state.items["G-002"]
    assert "G-002" in res.item_ids and g.verification.status == "blocked"
    assert resolve(g).detail == "C-001: source revoked: S-002"
    assert ctx.state.items["E-001"].verification.status == "blocked"
    assert "rollup" not in resolve(ctx.state.items["E-001"]).detail   # not final: no rollup note
    assert res.sentence_ids == [] and res.criteria == []


def test_pending_items_are_left_to_their_stage_and_a_second_pass_changes_nothing(settings, fixture_bundle):
    ctx = fixture_ctx(settings, fixture_bundle)
    ctx.state.items["D-036"].verification = m.Verification(status="pending")
    res, _ = revoke(ctx, "S-005")
    assert "D-036" not in res.item_ids and ctx.state.items["D-036"].verification.status == "pending"
    mark = ctx.state.run.last_seq
    again = cascade.revoke_cascade(ctx, "S-005")
    assert again.item_ids == [] and again.sentence_ids == [] and ctx.state.run.last_seq == mark


def test_revoke_route_runs_the_cascade(settings, fixture_bundle):
    ctx = fixture_ctx(settings, fixture_bundle)
    state = ctx.state
    state.run.status, state.run.stage = "done", "complete"
    ctx.store.save(state)
    run_id = state.run.run_id
    with TestClient(create_app(settings, store=ctx.store)) as c:
        r = c.post(f"{P}/runs/{run_id}/sources/S-006/revoke", json={"reason": "demo revoke"})
        assert r.status_code == 200, r.text
        rev = m.RevokeResponse.model_validate(r.json())
        assert rev.source.status == "revoked" and any(x.rule_id == "R-revoke-S-006" for x in rev.policy.rules)
        report = m.AgentReport.model_validate(c.get(f"{P}/runs/{run_id}/report").json())
        events = m.EventsPage.model_validate(c.get(f"{P}/runs/{run_id}/events", params={"limit": 2000}).json())
    d = next(i for i in report.items if i.id == "D-037")
    assert d.verification.status == "blocked" and resolve(d).detail == "source revoked: S-006"
    assert [e.data.item_id for e in events.events if e.type == "item.verified"] == ["D-037", "C-009"]


# ---------------------------------------------------------------------------
# collect: S-006 late, "next:" lines before every request, STAGE_PACING before collect
# ---------------------------------------------------------------------------

PDF_URL = "https://ir.example.com/annual-report-2025.pdf"
NEWS_URL = "https://news.example.com/2026/11/05/shl-results"
BLOG_URL = "https://blog.example.net/shl-up-8-percent"
BLOG_LINKS = ["https://example.com/one", "https://example.net/two", "https://example.org/three"]
PAGES = ["Annual Report FY2025 (simulated)\nTo our shareholders",
         "Consolidated statements of income\n(in millions of €)\nNote 2025 2024\nRevenue 22,363 21,680"]


def make_pdf(pages: list[str]) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    font = pymupdf.Font("helv")
    for text in pages:
        page = doc.new_page()
        writer = pymupdf.TextWriter(page.rect)
        y = 72
        for line in text.split("\n"):
            writer.append((72, y), line, font=font, fontsize=11)
            y += 16
        writer.write_text(page)
    data = doc.tobytes()
    doc.close()
    return data


def blog_html() -> bytes:
    links = "".join(f'<p>See <a href="{u}">this</a> for more on the outlook.</p>' for u in BLOG_LINKS)
    return (f"<html><head><title>Blog</title></head><body><article><h1>Why SHL is up</h1>"
            f"<p>Simulated blog words about the outlook, clean of any instructions.</p>{links}</article></body></html>"
            ).encode()


def news_html() -> bytes:
    return b"<html><body><article><h1>Results</h1><p>Simulated wire story about the results.</p></article></body></html>"


def source(sid: str, url: str, *, kind: str, tier: str, rank: int, title: str) -> m.Source:
    from urllib.parse import urlsplit

    return m.Source(source_id=sid, title=title, url=url, domain=urlsplit(url).hostname or "", kind=kind,
                    data_types=["other"], access=m.SourceAccess(), tier=tier, rank=rank,
                    scores=m.SourceScores(reliability=0.5, relevance=0.5), rationale="test", need_ids=["N-01"],
                    criteria=["rotic_level"], vault_folder="04-News-Coverage", proposed_by="planner",
                    approval="approved", status="queued")


def collect_ctx(settings: Settings, pacing: float = 0.0) -> RunContext:
    store = Store(settings.model_copy(update={"stage_pacing": pacing}))
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api"))
    state.sources = [
        source("S-009", BLOG_URL, kind="other", tier="tier_3", rank=1, title="Simulated blog post on SHL"),
        source("S-006", NEWS_URL, kind="news", tier="tier_2", rank=1,
               title="Reuters via Euronext: Siemens Healthineers begins carve-out of Diagnostics (simulated)"),
        source("S-001", PDF_URL, kind="annual_report", tier="priority", rank=1, title="Annual Report FY2025"),
    ]
    state.run.stage = "collect"
    ctx = RunContext.create(state, store, store.vault, store.settings)
    ctx.gateway.build_policy()
    return ctx


@pytest.fixture
def canned(monkeypatch) -> list[tuple[str, float]]:
    """get_once answers from canned bodies (simulated sources). Records (url, time) of every real send."""
    sent: list[tuple[str, float]] = []
    bodies = {PDF_URL: (make_pdf(PAGES), "application/pdf"), NEWS_URL: (news_html(), "text/html"),
              BLOG_URL: (blog_html(), "text/html; charset=utf-8")}

    async def fake_get_once(url: str, **kw):
        sent.append((url, time.monotonic()))
        if url not in bodies:
            return 404, {"content-type": "text/plain"}, b"not found"
        body, ctype = bodies[url]
        return 200, {"content-type": ctype}, body

    monkeypatch.setattr(fetch_http, "get_once", fake_get_once)
    bedrock.register_fake_scan(lambda text: FLAG in text.lower())
    yield sent
    bedrock.register_fake_scan(None)


def _t(e: m.Event) -> float:
    return datetime.fromisoformat(e.ts.replace("Z", "+00:00")).timestamp()


def test_next_line_names_the_source_and_its_host():
    s = source("S-006", "https://live.euronext.com/en/financial-news/x", kind="news", tier="tier_2", rank=1,
               title="Reuters via Euronext: Siemens Healthineers begins carve-out of its Diagnostics business")
    text = collect.next_source_text(s)
    assert text.startswith("next: S-006 Reuters via Euronext: ") and text.endswith("(live.euronext.com)")
    assert len(text) < 110
    s.status = "revoked"
    assert collect.next_source_text(s).endswith("(live.euronext.com, revoked: the gateway will block it)")


def test_s006_stays_late_in_the_shl_plan():
    sources = [plan_stage._fresh_source(x) for x in plan_stage.load_plan()["sources"]]
    for s in sources:
        s.approval = "approved" if s.tier == "priority" else "auto_approved"
    order = [s.source_id for s in collect.collect_order(sources)]
    by_id = {s.source_id: s for s in sources}
    pos = order.index("S-006")
    # Plan v2 adds priority archive sources (S-011 and up), so S-006 is no longer 8th. The real requirement:
    # every priority source is fetched before S-006, and S-009 then S-010 come after it.
    assert all(by_id[sid].tier == "priority" for sid in order[:pos])
    assert not any(by_id[sid].tier == "priority" for sid in order[pos + 1:])
    assert order.index("S-009") > pos and order.index("S-010") > order.index("S-009")


async def test_every_request_is_announced(settings, canned):
    ctx = collect_ctx(settings)
    await collect.run(ctx)
    evs = ctx.state.events
    logs = [e for e in evs if e.type == "agent.log" and e.data.text.startswith("next: ")]
    reqs = [e for e in evs if e.type == "gateway.request"]
    assert [e.data.text.split(" ")[1] for e in logs[:3]] == ["S-001", "S-006", "S-009"]
    assert sum(e.data.text.startswith("next: link found in S-009, ") for e in logs) == 3
    assert len(logs) == len(reqs) == 6                        # 3 sources and 3 links, each announced, then decided
    for log, req in zip(logs, reqs):
        assert log.seq < req.seq
    links = [r.data for r in reqs if r.data.reason.startswith("link found in page")]
    assert [x.target for x in links] == BLOG_LINKS and all(x.decision == "blocked" for x in links)
    assert all(x.rule_id is None and "Default deny" in x.reason for x in links)
    assert [u for u, _ in canned] == [PDF_URL, NEWS_URL, BLOG_URL]   # links never left the gateway


async def test_a_revoke_during_the_collect_pause_blocks_s006(settings, canned, monkeypatch):
    """STAGE_PACING holds collect after its run.status, so a revoke sent when collect shows blocks S-006."""
    monkeypatch.setattr(orchestrator, "STAGES", [("collect", "collect")])
    ctx = collect_ctx(settings, 0.4)
    task = asyncio.create_task(orchestrator.run_pipeline(ctx))
    deadline = time.monotonic() + 10
    while not any(e.type == "run.status" and e.data.stage == "collect" for e in ctx.state.events):
        assert time.monotonic() < deadline and not task.done()
        await asyncio.sleep(0.01)
    assert not any(e.type == "gateway.request" for e in ctx.state.events)     # still in the pause
    ctx.gateway.revoke("S-006", "presenter revoked it")
    cascade.revoke_cascade(ctx, "S-006")
    await task
    reqs = [e.data for e in ctx.state.events if e.type == "gateway.request" and e.data.source_id == "S-006"]
    assert [(r.decision, r.rule_id) for r in reqs] == [("blocked", "R-revoke-S-006")]
    s006 = ctx.state.sources_by_id()["S-006"]
    assert s006.status == "revoked" and s006.doc_ids == []
    assert not any(d.source_id == "S-006" for d in ctx.state.documents)   # never stored, so never extracted
    assert NEWS_URL not in [u for u, _ in canned]


# ---------------------------------------------------------------------------
# demo_live beat checks (pure)
# ---------------------------------------------------------------------------


def _ev(seq: int, type: str, data: dict, stage: str = "collect") -> dict:
    return {"seq": seq, "ts": f"2026-11-08T03:00:{seq:02d}.000Z", "type": type, "stage": stage, "data": data}


def _req(seq: int, decision: str, *, sid: str | None = "S-006", rule: str | None = None, action: str = "fetch",
         reason: str = "approved source: x", stage: str = "collect") -> dict:
    return _ev(seq, "gateway.request", {"action": action, "decision": decision, "source_id": sid, "rule_id": rule,
                                        "reason": reason, "target": "https://live.euronext.com/x",
                                        "http_status": 200, "bytes": 10, "tokens_in": None, "tokens_out": None,
                                        "cost_usd": None}, stage)


def test_revoke_beat_fails_when_the_source_was_fetched_first():
    events = [_req(1, "allowed", rule="R-allow-S-006"),
              _ev(2, "policy.updated", {"version": 3, "change": "Revoked S-006", "rule_ids": ["R-revoke-S-006"]})]
    beat = demo_live.beat_revoke(events, "S-006", demo_live.Actions())
    assert not beat.ok and "fetched before the revoke landed" in beat.detail
    events.append(_req(3, "blocked", rule="R-revoke-S-006", reason="S-006 was revoked mid-run"))
    assert demo_live.beat_revoke(events, "S-006", demo_live.Actions()).ok


def test_stop_beat_fails_when_anything_is_allowed_after_stop():
    events = [_ev(1, "run.stopped", {"reason": "x"}, "extract"),
              _req(2, "blocked", sid=None, action="model.invoke", reason="Kill switch", stage="extract"),
              _req(3, "allowed", sid=None, action="model.invoke", stage="extract")]
    beat = demo_live.beat_stop(events, demo_live.Actions(stop_after_seq=0))
    assert not beat.ok and "1 request(s) allowed afterwards" in beat.detail
    assert demo_live.beat_stop(events[:2], demo_live.Actions(stop_after_seq=0)).ok
    nothing = demo_live.beat_stop(events[:1], demo_live.Actions(stop_after_seq=0))
    assert not nothing.ok and "nothing was in flight" in nothing.detail


def test_presenter_commands_cover_every_beat():
    lines = demo_live.presenter_commands("https://api.example.com/api/v1", "run_20261108_abc123")
    text = "\n".join(lines)
    for cmd in ("run \"Siemens Healthineers\" --ticker SHL", "tail run_20261108_abc123",
                "approve run_20261108_abc123 --all", "revoke run_20261108_abc123 S-006", "stop run_20261108_abc123"):
        assert f"python -m backend.axelrod.cli {cmd}" in text


# ---------------------------------------------------------------------------
# demo_live end to end, offline: a real uvicorn, fake models and scan (simulated), canned source bodies
# ---------------------------------------------------------------------------


@contextmanager
def live_api(settings_for) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(settings_for(base)), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()
        assert not thread.is_alive(), "uvicorn did not stop"


def small_plan(tmp_path: Path) -> Path:
    """plan.json with two extract pages per PDF, so the canned PDFs stay tiny."""
    plan = plan_stage.load_plan()
    plan["extract_pages"] = {sid: [[1, 2]] for sid in ("S-001", "S-002", "S-003", "S-004", "S-005")}
    plan["need_pages"] = {}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def canned_bodies(plan: dict) -> dict[str, tuple[bytes, str]]:
    """Simulated bodies for every planned URL except the planted page (the API serves that one itself)."""
    pdf = make_pdf(PAGES)
    bodies: dict[str, tuple[bytes, str]] = {}
    for s in plan["sources"]:
        if s["kind"] == "xbrl_facts":
            bodies[s["url"]] = (json.dumps({"cik": 1, "entityName": "Peer (simulated)", "facts": {}}).encode(),
                                "application/json")
        elif s["source_id"] == "S-006":
            bodies[s["url"]] = (news_html(), "text/html")
        elif s["source_id"] == "S-009":
            bodies[s["url"]] = (blog_html(), "text/html; charset=utf-8")
        elif s["url"].lower().endswith(".pdf") or s["kind"] in ("annual_report", "company_ir"):
            bodies[s["url"]] = (pdf, "application/pdf")
    return bodies


@pytest.fixture
def rehearsal_world(tmp_path, monkeypatch):
    """Canned bodies for every planned URL except the planted page, which the API serves itself. A slow fake
    extractor, so model calls are in flight when STOP lands."""
    plan_file = small_plan(tmp_path)
    monkeypatch.setattr(plan_stage, "PLAN_FILE", plan_file)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    bodies = canned_bodies(plan)
    real_get_once = fetch_http.get_once
    sent: list[str] = []

    async def get_once(url: str, **kw):
        sent.append(url)
        if url.startswith("http://127.0.0.1:"):
            return await real_get_once(url, **kw)        # the planted page, from the API's own route
        if url in bodies:
            body, ctype = bodies[url]
            return 200, {"content-type": ctype}, body
        return 404, {"content-type": "text/plain"}, b"not found"

    calls = itertools.count()

    async def slow_extract(system: str, user: str, schema: dict) -> dict:
        # Like real calls, they don't all take the same time: the first lands fast, the rest are still in flight.
        await asyncio.sleep(0.2 if next(calls) == 0 else 1.5)
        return extractor.fake_extract(system, user, schema)

    monkeypatch.setattr(fetch_http, "get_once", get_once)
    bedrock.register_fake(extractor.TOOL, slow_extract)
    bedrock.register_fake_scan(lambda text: FLAG in text.lower())
    yield sent
    bedrock.register_fake(extractor.TOOL, extractor.fake_extract)
    bedrock.register_fake_scan(None)


# DEMO_PACING is gone, so demo_live's --pacing-check (a gap between "next: S-006" and its request) has nothing
# left to measure. The default rehearsal revokes when collect starts, which STAGE_PACING leaves time for.
@pytest.mark.parametrize("pacing_check", [False])
def test_demo_live_passes_every_beat_offline(settings, rehearsal_world, pacing_check):
    pacing = 0.5
    prices = {settings.model_small: {"in": 1.0, "out": 5.0}, settings.model_large: {"in": 3.0, "out": 15.0}}

    def settings_for(base: str) -> Settings:
        return settings.model_copy(update={"stage_pacing": pacing, "test_page_url": base + ROUTE,
                                           "max_concurrency_small": 2, "prices": prices})

    opts = demo_live.Options(pacing_check=pacing_check, min_gap_s=0.4, timeout_s=90, settle_s=30, poll_s=0.05,
                             approve_wait_s=30)
    lines: list[str] = []
    with live_api(settings_for) as base:
        client = demo_live.Client(base + P, None, timeout=15)
        reh = demo_live.rehearse(client, opts, say=lines.append)
        demo_live.report(reh, base + P, say=lines.append)
        rescored = demo_live.check_run(client, reh.run["run_id"], opts)     # --check: score only, send nothing
    out = "\n".join(lines)
    assert reh.passed, out
    assert rescored.passed and [b.ok for b in rescored.beats] == [b.ok for b in reh.beats]
    assert [b.key for b in reh.beats] == ["launch", "traffic", "link_blocked", "revoke", "flagged", "stop"] + (
        ["pacing"] if pacing_check else [])
    assert out.rstrip().endswith("RESULT PASS")
    assert f"python -m backend.axelrod.cli revoke {reh.run['run_id']} S-006" in out
    assert reh.run["status"] == "stopped" and reh.run["stage"] == "extract"
    sent = rehearsal_world
    assert not any("example." in u for u in sent)               # blocked links never left the gateway
    assert not any("euronext" in u for u in sent)               # S-006 revoked before its request


# ---------------------------------------------------------------------------
# demo_live --webapp (G6b): the webapp's calls, cache hits, one goon stopped, timing (simulated event streams)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 8, 11, 0, 0)
PRIORITY = ["S-001", "S-002", "S-007", "S-008", "S-003", "S-004", "S-005", "S-011", "S-012", "S-013", "S-014",
            "S-015", "S-016"]
GOON = "extractor-04"


class Stream:
    """A webapp run's event stream, built in order. Seconds are from run.created, the way the server stamps."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, t: float, type: str, data: dict, *, stage: str = "collect", agent_id: str | None = None) -> dict:
        ts = (T0 + timedelta(seconds=t)).isoformat(timespec="milliseconds") + "Z"
        e = {"seq": len(self.events) + 1, "ts": ts, "run_id": "run_x", "type": type, "stage": stage,
             "agent_id": agent_id, "data": data}
        self.events.append(e)
        return e

    def req(self, t: float, decision: str, *, action: str = "fetch", sid: str | None = None, rule: str | None = None,
            reason: str = "approved source: allowed", cache: str | None = None, cached_at: str | None = None,
            agent_id: str = "fetcher-01", stage: str = "collect", role: str = "fetcher") -> dict:
        model = action == "model.invoke"
        data = {"request_id": f"REQ-{len(self.events) + 1:04d}", "role": role, "action": action,
                "target": "us.anthropic.claude-haiku-4-5" if model else f"https://example.test/{sid}",
                "decision": decision, "rule_id": rule, "reason": reason, "source_id": sid, "latency_ms": 20,
                "http_status": 200 if decision == "allowed" else None, "bytes": None if model else 1000,
                "tokens_in": 900 if model else None, "tokens_out": 120 if model else None,
                "cost_usd": 0.001 if model else (0.0 if cache == "hit" else None), "cache": cache,
                "cached_at": cached_at}
        return self.add(t, "gateway.request", data, stage=stage, agent_id=agent_id)


def good_stream() -> tuple[Stream, list[dict], list[dict]]:
    """Every webapp beat as it should go. Returns the stream, the documents and the plan's sources."""
    s = Stream()
    s.add(0, "run.created", {"run": {"created_via": "ui", "cache_mode": "documents"}}, stage="plan")
    s.add(0.1, "agent.spawned", {"agent": {"agent_id": "bobby", "role": "orchestrator"}}, stage="plan",
          agent_id="bobby")
    s.add(4, "plan.ready", {"needs": 7, "sources": 16}, stage="plan")
    s.add(8, "agent.decision", {"agent_id": "bobby", "stage": "plan", "decision": "proceed", "summary": "ok"},
          stage="plan", agent_id="bobby")
    approved = ["S-002", "S-001"] + PRIORITY[2:-1]
    s.add(14, "approval.committed", {"approved": approved, "rejected": ["S-016"],
                                     "auto_approved": ["S-006", "S-009", "S-010"]}, stage="approve")
    s.add(14, "policy.updated", {"version": 2, "change": "Approval committed",
                                 "rule_ids": ["R-sys-models"] + [f"R-allow-{x}" for x in approved] + ["R-reject-S-016"]},
          stage="approve")
    t = 15.0
    for i, sid in enumerate(approved):
        s.add(t, "agent.log", {"text": f"next: {sid} Some report (example.test)"}, agent_id="fetcher-01")
        hit = i != 2                                         # one live miss, the rest from the cache
        s.req(t + 1.0, "allowed", sid=sid, cache="hit" if hit else "miss",
              cached_at="2026-08-01T09:00:00.000Z" if hit else None)
        s.add(t + 1.2, "doc.stored", {"doc_id": f"DOC-{i + 1:03d}", "source_id": sid, "pages": 2,
                                      "vault_path": "x", "scan": {"verdict": "not_scanned"}})
        if i == 0:
            s.add(t + 1.3, "policy.updated", {"version": 3, "change": "Revoked S-006",
                                              "rule_ids": ["R-revoke-S-006"]})
        t += 1.3
    s.add(t, "agent.log", {"text": "next: S-006 Reuters via Euronext (live.euronext.com, revoked)"},
          agent_id="fetcher-01")
    s.req(t + 1.0, "blocked", sid="S-006", rule="R-revoke-S-006", reason="approved source: S-006 was revoked")
    s.add(t + 1.1, "agent.log", {"text": "next: S-009 Blog (simplywall.st)"}, agent_id="fetcher-01")
    s.req(t + 2.0, "allowed", sid="S-009", cache="hit", cached_at="2026-11-08T07:00:00.000Z")
    s.add(t + 2.1, "agent.log", {"text": "next: link found in S-009, not a planned source: https://example.com/one"},
          agent_id="fetcher-01")
    s.req(t + 3.0, "blocked", reason="link found in page: No rule allows example.com/one. Default deny.")
    s.req(t + 4.0, "allowed", sid="S-010", reason="approved source: R-allow-S-010")
    s.req(t + 4.5, "flagged", sid="S-010", reason="Guardrails intervened on page 1")
    s.add(t + 4.6, "doc.stored", {"doc_id": "DOC-099", "source_id": "S-010", "pages": 1, "vault_path": "x",
                                  "scan": {"verdict": "flagged"}})
    s.req(44, "allowed", action="model.invoke", agent_id="extractor-01", stage="extract", role="extractor",
          reason="Role 'extractor' may call this model.")
    s.add(45, "policy.updated", {"version": 4, "change": f"Stopped agent {GOON}", "rule_ids": [f"R-stop-{GOON}"]},
          stage="extract")
    s.add(45, "agent.status", {"agent_id": GOON, "status": "stopped", "task": "x", "usage": {},
                               "reason": "Demo: the presenter stopped one goon"}, stage="extract", agent_id=GOON)
    s.req(46, "blocked", action="model.invoke", agent_id=GOON, stage="extract", role="extractor",
          rule=f"R-stop-{GOON}", reason="Stopped by the analyst: Demo")
    s.req(47, "allowed", action="model.invoke", agent_id="extractor-02", stage="extract", role="extractor",
          reason="Role 'extractor' may call this model.")
    s.add(48, "run.stopped", {"reason": "Demo: the presenter pressed STOP"}, stage="extract")
    s.req(49, "blocked", action="model.invoke", agent_id="extractor-03", stage="extract", role="extractor",
          reason="Kill switch: run stopped while the call was in flight. Result discarded.")
    docs = [{"doc_id": "DOC-099", "source_id": "S-010", "quarantined": True, "scan": {"verdict": "flagged"},
             "origin": "fetched"},
            {"doc_id": "DOC-001", "source_id": "S-002", "quarantined": False, "scan": {"verdict": "not_scanned"},
             "origin": "archive"}]
    sources = [{"source_id": sid, "tier": "priority", "rank": n} for n, sid in enumerate(PRIORITY, start=1)]
    sources += [{"source_id": "S-006", "tier": "tier_2", "rank": 1}, {"source_id": "S-009", "tier": "tier_3", "rank": 1},
                {"source_id": "S-010", "tier": "tier_3", "rank": 2}]
    return s, docs, sources


def good_acts(**kw):
    base = dict(run_id="run_x", order=("S-002", "S-001"), goon_id=GOON, goon_was="queued", goon_after_seq=1,
                stop_after_seq=1, marks={"stop": 48.3})
    base.update(kw)
    return demo_live.Actions(**base)


def webapp_beats(events: list[dict], docs: list[dict], sources: list[dict], acts=None, **opts) -> dict:
    o = demo_live.Options(webapp=True, **opts)
    return {b.key: b for b in demo_live.check_webapp_beats(events, docs, sources, acts or good_acts(), o)}


def test_webapp_beats_pass_on_a_good_stream():
    s, docs, sources = good_stream()
    beats = webapp_beats(s.events, docs, sources)
    assert list(beats) == ["launch", "approval", "traffic", "cache", "link_blocked", "revoke", "flagged", "goon",
                           "stop", "timing"]
    assert all(b.ok and b.observed for b in beats.values()), {k: b.detail for k, b in beats.items() if not b.ok}
    assert "12 hit, 1 miss" in beats["cache"].detail and "cache_mode documents" in beats["cache"].detail
    assert "blocked by R-stop-extractor-04" in beats["goon"].detail
    tl = demo_live.timeline(s.events, demo_live.Options(webapp=True))
    assert tl["stop"] == 48 and tl["goon"] == 45 and tl["goon_blocked"] == 46 and tl["extract"] == 44


def test_webapp_cache_beat_fails_without_a_hit():
    s, docs, sources = good_stream()
    for e in s.events:
        if e["type"] == "gateway.request" and e["data"]["cache"] == "hit":
            e["data"]["cache"], e["data"]["cached_at"] = "miss", None
    beat = webapp_beats(s.events, docs, sources)["cache"]
    assert not beat.ok and "no fetch was a cache hit" in beat.detail


def test_webapp_goon_beat_fails_when_the_stopped_goon_gets_a_later_call():
    s, docs, sources = good_stream()
    s.req(47.5, "allowed", action="model.invoke", agent_id=GOON, stage="extract", role="extractor")
    beat = webapp_beats(s.events, docs, sources)["goon"]
    assert not beat.ok and "1 request(s) of it allowed afterwards" in beat.detail


def test_webapp_goon_beat_not_observed_is_not_a_fail():
    s, docs, sources = good_stream()
    s.events = [e for e in s.events if not (e["type"] == "gateway.request" and e["agent_id"] == GOON)]
    beat = webapp_beats(s.events, docs, sources)["goon"]
    assert beat.ok and not beat.observed and beat.verdict == "N/OBS" and "made no further call" in beat.detail


def test_webapp_goon_beat_fails_when_no_other_goon_keeps_going():
    s, docs, sources = good_stream()
    s.events = [e for e in s.events if e["agent_id"] != "extractor-02"]
    beat = webapp_beats(s.events, docs, sources)["goon"]
    assert not beat.ok and "no other goon got an allowed model call before STOP" in beat.detail


def test_webapp_timing_fails_over_55_seconds():
    s, docs, sources = good_stream()
    assert webapp_beats(s.events, docs, sources)["timing"].ok
    late = webapp_beats(s.events, docs, sources, good_acts(marks={"stop": 56.2}))["timing"]
    assert not late.ok and "over the 55 s limit" in late.detail
    # --check has no script clock: the server's run.created to run.stopped decides
    server = webapp_beats(s.events, docs, sources, good_acts(marks={}), max_seconds=45)["timing"]
    assert not server.ok and "48.0 s (server clock)" in server.detail


def test_webapp_approval_beat_checks_order_and_rejection():
    s, docs, sources = good_stream()
    first = [e for e in s.events if e["type"] == "gateway.request"][:2]
    first[0]["data"]["source_id"], first[1]["data"]["source_id"] = "S-001", "S-002"
    beat = webapp_beats(s.events, docs, sources)["approval"]
    assert not beat.ok and "S-002 was fetched after S-001" in beat.detail
    s2, _, _ = good_stream()
    s2.req(30, "blocked", sid="S-016", rule="R-reject-S-016")
    beat = webapp_beats(s2.events, docs, sources)["approval"]
    assert not beat.ok and "S-016 was requested 1 time(s)" in beat.detail


def test_webapp_decisions_move_one_up_and_reject_one():
    sources = [{"source_id": sid, "tier": "priority", "rank": n} for n, sid in enumerate(PRIORITY, start=1)]
    sources += [{"source_id": "S-006", "tier": "tier_2", "rank": 1}]
    decisions, order = demo_live.webapp_decisions(sources, demo_live.Options(webapp=True))
    assert order == ("S-002", "S-001")
    assert [d["source_id"] for d in decisions][:3] == ["S-002", "S-001", "S-007"]
    assert [d["rank"] for d in decisions] == list(range(1, len(PRIORITY) + 1))
    assert [d["source_id"] for d in decisions if d["approval"] == "rejected"] == ["S-016"]
    assert not any(d["source_id"] in ("S-006", "S-009", "S-010") for d in decisions)   # left to auto-approval


def test_the_shipped_plan_allows_the_presenters_approval():
    sources = json.loads(demo_live.PLAN_FILE.read_text(encoding="utf-8"))["sources"]
    decisions, order = demo_live.webapp_decisions(sources, demo_live.Options(webapp=True))
    assert order == ("S-002", demo_live.plan_passed("S-002"))
    assert sum(d["approval"] == "rejected" for d in decisions) == 1


@pytest.mark.parametrize("sid", ["S-006", "S-009", "S-010"])
def test_the_script_refuses_to_touch_the_block_sources(sid, capsys):
    for kw in ({"reject": sid}, {"move_up": sid}):
        with pytest.raises(ValueError, match="Refusing"):
            demo_live.validate_webapp(demo_live.Options(webapp=True, **kw))
    # Refused before any call: nothing listens on port 9, and nothing is sent to it.
    assert demo_live.main(["--webapp", "--reject", sid.lower(), "--api", "http://127.0.0.1:9"]) == 2
    assert f"REFUSED  Refusing to reject {sid}" in capsys.readouterr().out


def test_pacing_advice_moves_stop_to_about_fifty_seconds():
    s, _, _ = good_stream()
    opts = demo_live.Options(webapp=True, pacing_hint=2.0)
    advice = demo_live.advise_pacing(s.events, good_acts(marks={"stop": 60.0}), opts)
    assert advice.used == pytest.approx(0.98, abs=0.01)          # 1.0 s announcement to request, minus 20 ms
    assert advice.revoke_pos == 13 and advice.fetches == 16      # 12 priority (S-016 rejected), S-006, S-009, link, S-010
    assert advice.suggested == pytest.approx(0.98 - 10 / 16, abs=0.01)
    floor = demo_live.advise_pacing(s.events, good_acts(marks={"stop": 90.0}),
                                    demo_live.Options(webapp=True, pacing_check=True, min_gap_s=1.0))
    assert floor.suggested == 1.0 and "can't land" in floor.note


def test_pick_goon_prefers_the_next_queued_extractor():
    agents = [{"agent_id": "bobby", "role": "orchestrator", "model_id": "m", "status": "working"},
              {"agent_id": "extractor-01", "role": "extractor", "model_id": "m", "status": "working",
               "started_at": "2026-11-08T11:00:01.000Z"},
              {"agent_id": "extractor-02", "role": "extractor", "model_id": None, "status": "queued"},
              {"agent_id": "extractor-03", "role": "extractor", "model_id": "m", "status": "working",
               "started_at": "2026-11-08T11:00:00.000Z"},
              {"agent_id": "extractor-04", "role": "extractor", "model_id": "m", "status": "queued"},
              {"agent_id": "extractor-05", "role": "extractor", "model_id": "m", "status": "done"}]
    picks = [a["agent_id"] for a in demo_live.pick_goon(agents, {"extractor-01"})]
    assert picks == ["extractor-04", "extractor-03"]


def test_webapp_report_prints_the_table_pacing_and_steps():
    s, docs, sources = good_stream()
    opts = demo_live.Options(webapp=True, pacing_hint=2.0)
    reh = demo_live.Rehearsal(actions=good_acts(), run={"run_id": "run_x", "status": "stopped", "stage": "extract",
                                                       "usage": {"model_calls": 3, "cost_usd": 0.003}},
                              events=s.events, documents=docs)
    reh.beats = demo_live.check_webapp_beats(s.events, docs, sources, reh.actions, opts)
    lines: list[str] = []
    demo_live.report_webapp(reh, "https://api.example.com/api/v1", opts, say=lines.append)
    out = "\n".join(lines)
    assert "DEMO_PACING on the server: unknown" in out and "--pacing hint: 2 s" in out
    assert "Suggested DEMO_PACING for STOP at about 50 s:" in out and "S-006 is fetch 13" in out
    assert any(line.split()[:1] == ["STOP"] and line.split()[-2:] == ["50", "48.0"] for line in lines), out
    assert "drag S-002 above S-001, reject S-016" in out and "R-stop-<agent id>" in out
    assert out.rstrip().endswith("RESULT PASS")


def test_demo_live_webapp_passes_every_beat_offline(settings, rehearsal_world):
    """The webapp's calls against a real uvicorn: cached documents (simulated, prefilled), fake models."""
    from backend.axelrod.cache import DocCache

    pacing = 0.5                                       # STAGE_PACING: time to revoke as collect shows
    prices = {settings.model_small: {"in": 1.0, "out": 5.0}, settings.model_large: {"in": 3.0, "out": 15.0}}
    plan = json.loads(plan_stage.PLAN_FILE.read_text(encoding="utf-8"))
    cached = settings.model_copy(update={"cache_documents": True})
    skip = {s["url"] for s in plan["sources"] if s["kind"] == "xbrl_facts"}     # these two stay live misses
    for url, (body, ctype) in canned_bodies(plan).items():
        if url not in skip:
            DocCache(cached).put(url, body, ctype)

    def settings_for(base: str) -> Settings:
        return cached.model_copy(update={"stage_pacing": pacing, "test_page_url": base + ROUTE,
                                         "max_concurrency_small": 2, "prices": prices})

    opts = demo_live.Options(webapp=True, timeout_s=90, settle_s=30, poll_s=0.05, approve_wait_s=30,
                             stop_gap_s=2.0, pacing_hint=pacing)
    lines: list[str] = []
    with live_api(settings_for) as base:
        client = demo_live.Client(base + P, None, timeout=15)
        reh = demo_live.rehearse_webapp(client, opts, say=lines.append)
        demo_live.report_webapp(reh, base + P, opts, say=lines.append)
        rescored = demo_live.check_run(client, reh.run["run_id"], opts)    # --webapp --check: score only
    out = "\n".join(lines)
    print(out)                                                    # pytest -rP shows the rehearsal's own report
    beats = {b.key: b for b in reh.beats}
    assert reh.passed, out
    assert list(beats) == ["launch", "approval", "traffic", "cache", "link_blocked", "revoke", "flagged", "goon",
                           "stop", "timing"]
    assert rescored.passed, "\n".join(f"{b.verdict} {b.key}: {b.detail}" for b in rescored.beats)
    # misses: the two SEC JSON sources and the planted page (served live by the API itself)
    assert "created via ui" in beats["launch"].detail and " hit, 3 miss" in beats["cache"].detail
    assert "hit, 0 miss" not in beats["cache"].detail and " 0 hit" not in beats["cache"].detail
    assert reh.actions.goon_id and reh.actions.goon_id.startswith("extractor-")
    assert reh.run["status"] == "stopped" and reh.run["stage"] == "extract"
    assert "Webapp steps (for backend/DEMO.md):" in out and out.rstrip().endswith("RESULT PASS")
    sent = rehearsal_world
    s016 = next(s["url"] for s in plan["sources"] if s["source_id"] == "S-016")
    assert s016 not in sent                                       # rejected: never requested
    assert not any("example." in u for u in sent) and not any("euronext" in u for u in sent)
    assert sum(u in skip for u in sent) == 2                      # the cached sources never went out
    assert all(u in skip or u.startswith("http://127.0.0.1:") for u in sent)
