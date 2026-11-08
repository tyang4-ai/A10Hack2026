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
from datetime import datetime
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend.axelrod import bedrock, cascade
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
# collect: S-006 late, "next:" lines, pacing before every request
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


def collect_ctx(settings: Settings, pacing: float) -> RunContext:
    store = Store(settings.model_copy(update={"demo_pacing": pacing}))
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
    assert order.index("S-006") == 7 and order[-2:] == ["S-009", "S-010"]


async def test_every_request_is_announced_and_paced(settings, canned):
    pacing = 0.25
    ctx = collect_ctx(settings, pacing)
    await collect.run(ctx)
    evs = ctx.state.events
    logs = [e for e in evs if e.type == "agent.log" and e.data.text.startswith("next: ")]
    reqs = [e for e in evs if e.type == "gateway.request"]
    assert [e.data.text.split(" ")[1] for e in logs[:3]] == ["S-001", "S-006", "S-009"]
    assert sum(e.data.text.startswith("next: link found in S-009, ") for e in logs) == 3
    assert len(logs) == len(reqs) == 6                        # 3 sources and 3 links, each announced, then decided
    for log, req in zip(logs, reqs):
        assert log.seq < req.seq
        assert _t(req) - _t(log) >= pacing - 0.03, (log.data.text, req.data.target)
    links = [r.data for r in reqs if r.data.reason.startswith("link found in page")]
    assert [x.target for x in links] == BLOG_LINKS and all(x.decision == "blocked" for x in links)
    assert all(x.rule_id is None and "Default deny" in x.reason for x in links)
    assert [u for u, _ in canned] == [PDF_URL, NEWS_URL, BLOG_URL]   # links never left the gateway


async def test_a_revoke_during_the_pacing_wait_blocks_that_very_request(settings, canned):
    ctx = collect_ctx(settings, 0.4)
    task = asyncio.create_task(collect.run(ctx))
    deadline = time.monotonic() + 10
    while not any(e.type == "agent.log" and e.data.text.startswith("next: S-006 ") for e in ctx.state.events):
        assert time.monotonic() < deadline and not task.done()
        await asyncio.sleep(0.01)
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


@pytest.fixture
def rehearsal_world(tmp_path, monkeypatch):
    """Canned bodies for every planned URL except the planted page, which the API serves itself. A slow fake
    extractor, so model calls are in flight when STOP lands."""
    plan_file = small_plan(tmp_path)
    monkeypatch.setattr(plan_stage, "PLAN_FILE", plan_file)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("pacing_check", [False, True])
def test_demo_live_passes_every_beat_offline(settings, rehearsal_world, pacing_check):
    pacing = 0.6 if pacing_check else 0.1
    prices = {settings.model_small: {"in": 1.0, "out": 5.0}, settings.model_large: {"in": 3.0, "out": 15.0}}

    def settings_for(base: str) -> Settings:
        return settings.model_copy(update={"demo_pacing": pacing, "test_page_url": base + ROUTE,
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
