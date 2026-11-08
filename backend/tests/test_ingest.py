"""B-ingest: PDF and HTML page text, ingest into the vault and RunState, the collect stage, DocumentPage views.

Offline: a fake gateway (fetch gives FetchResult, scan gives ScanResult), the real local Vault in the test's
temp DATA_DIR, and a minimal context whose emit validates every event against the contract union. One test
runs the real Store, RunContext and Gateway with only the HTTP call and the guardrail faked. The injection
page under tests/data/ingest/ is a simulated fixture, and its "flagged" scan is simulated too, not Bedrock.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel, TypeAdapter

from backend.axelrod.config import get_settings
from backend.axelrod.contract import m
from backend.axelrod.fetch import ingest as ing
from backend.axelrod.fetch.html import html_links, html_pages, split_pages
from backend.axelrod.fetch.pdf import pdf_pages
from backend.axelrod.gateway import Blocked, FetchResult
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import StopRequested
from backend.axelrod.stages import collect
from backend.axelrod.store.state import RunState
from backend.axelrod.store.vault import Vault, register_run

DATA = Path(__file__).parent / "data" / "ingest"
INJECTION = DATA / "planted_injection_simulated.html"
EVENT = TypeAdapter(m.Event)
UNIT = "(in millions of €)"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGateway:
    """fetch answers from a URL table (anything else is a default deny). scan flags planted injections."""

    def __init__(self, responses: dict[str, FetchResult | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.fetches: list[tuple[str, str, str | None, str]] = []
        self.scans: list[tuple[str, str, str]] = []
        self.n = 0

    async def fetch(self, agent: m.Agent, url: str, *, source_id: str | None = None,
                    why: str = "approved source") -> FetchResult:
        self.n += 1
        self.fetches.append((agent.agent_id, url, source_id, why))
        hit = self.responses.get(url)
        if hit is None:
            raise Blocked(f"REQ-{self.n:04d}", None, f"No rule allows {urlsplit(url).hostname}. Default deny.")
        if isinstance(hit, Exception):
            raise hit
        return hit

    async def scan(self, agent: m.Agent, url: str, source_id: str, text: str) -> m.ScanResult:
        self.scans.append((url, source_id, text))
        if "ignore all previous instructions" in text.lower():
            return m.ScanResult(scanner="bedrock_guardrails", verdict="flagged", detail="PROMPT_ATTACK (simulated fake scan)")
        return m.ScanResult(scanner="bedrock_guardrails", verdict="clean", detail="fake scan: clean")


@dataclass
class FakeCtx:
    """The RunContext surface collect and ingest use. emit validates against the contract event union."""

    state: RunState
    gateway: FakeGateway
    vault: Vault
    stop_after_fetches: int | None = None

    def emit(self, type: str, data: dict | BaseModel, *, message: str, stage: str | None = None,
             agent_id: str | None = None, level: str = "info") -> m.Event:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        event = EVENT.validate_python({
            "seq": len(self.state.events) + 1, "ts": now_iso(), "run_id": self.state.run.run_id, "type": type,
            "stage": stage or self.state.run.stage, "agent_id": agent_id, "level": level, "message": message,
            "data": json.loads(json.dumps(payload)),
        })
        self.state.events.append(event)
        return event

    def next_id(self, prefix: str) -> str:
        return self.state.ids.next(prefix)

    def check_stop(self) -> None:
        if self.stop_after_fetches is not None and len(self.gateway.fetches) >= self.stop_after_fetches:
            raise StopRequested("kill switch on (test)")

    def events(self, type: str) -> list[m.Event]:
        return [e for e in self.state.events if e.type == type]

    def read(self, path: str) -> bytes:
        return self.vault.get_bytes(self.state.run.run_id, path)

    def text(self, path: str) -> str:
        return self.vault.get_text(self.state.run.run_id, path)


def make_run() -> m.Run:
    now = now_iso()
    return m.Run(run_id="run_20261107_test01", company="Siemens Healthineers AG", ticker="SHL", status="running",
                 stage="collect", created_at=now, updated_at=now, counts=m.RunCounts(), usage=m.Usage())


def make_source(sid: str, url: str, *, kind: str = "annual_report", tier: str = "priority", rank: int = 1,
                folder: str = "01-Annual-Reports", title: str | None = None, approval: str = "approved") -> m.Source:
    return m.Source(
        source_id=sid, title=title or f"Test source {sid}", url=url, domain=urlsplit(url).hostname or "", kind=kind,
        data_types=["other"], access=m.SourceAccess(), tier=tier, rank=rank,
        scores=m.SourceScores(reliability=0.5, relevance=0.5), rationale="test", need_ids=["N-01"],
        criteria=["rotic_level"], vault_folder=folder, proposed_by="planner", approval=approval, status="queued",
    )


def fetched(url: str, body: bytes, content_type: str, status: int = 200) -> FetchResult:
    return FetchResult(url=url, final_url=url, http_status=status, content_type=content_type, body=body,
                       request_id="REQ-0001")


def make_ctx(sources: list[m.Source], responses: dict) -> FakeCtx:
    """A fake context over the real local Vault (conftest's temp DATA_DIR)."""
    run = make_run()
    register_run(run.run_id, run.company)
    return FakeCtx(state=RunState(run=run, sources=sources), gateway=FakeGateway(responses),
                   vault=Vault(get_settings()))


# ---------------------------------------------------------------------------
# Generated inputs
# ---------------------------------------------------------------------------


def make_pdf(pages: list[str]) -> bytes:
    """A small PDF made with PyMuPDF. TextWriter with the helv Font keeps the euro sign extractable."""
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


STATEMENT_PAGES = [
    "Annual Report FY2025 (simulated)\nTo our shareholders",
    f"Consolidated statements of income\n{UNIT}\nRevenue 22,363 21,680\nCost of sales (13,402) (13,180)",
    "Consolidated statements of financial position\nTotal assets 51,234 49,876",
]


def long_html(paragraphs: int = 30) -> tuple[str, list[str]]:
    paras = [f"Paragraph {i}: Siemens Healthineers grew imaging revenue in the fictional year. " * 3
             for i in range(paragraphs)]
    body = "".join(f"<p>{p.strip()}</p>" for p in paras)
    return f"<html><head><title>T</title></head><body><article><h1>Results</h1>{body}</article></body></html>", paras


# ---------------------------------------------------------------------------
# pdf.py and html.py
# ---------------------------------------------------------------------------


def test_pdf_pages_every_page_in_order():
    pages = pdf_pages(make_pdf(STATEMENT_PAGES))
    assert len(pages) == 3
    assert UNIT in pages[1]  # page 2 is index 1
    assert "Revenue 22,363" in pages[1]
    assert "Total assets" in pages[2] and UNIT not in pages[2]


def test_pdf_pages_falls_back_to_pypdf(monkeypatch):
    data = make_pdf(STATEMENT_PAGES)
    monkeypatch.setitem(sys.modules, "pymupdf", None)  # import pymupdf now raises ImportError
    pages = pdf_pages(data)
    assert len(pages) == 3
    assert UNIT in pages[1]


def test_pdf_pages_rejects_garbage():
    with pytest.raises(ValueError):
        pdf_pages(b"this is not a pdf")


def test_html_pages_split_on_paragraphs():
    html, paras = long_html(30)
    pages = html_pages(html)
    assert len(pages) > 1
    assert all(0 < len(p) <= 3000 for p in pages)
    joined = "\n".join(pages)
    for p in paras:  # every paragraph survives whole and in order
        assert p.strip() in joined
    assert joined.index("Paragraph 3:") < joined.index("Paragraph 29:")


def test_split_pages_cuts_an_oversize_paragraph():
    text = "word " * 2000
    pages = split_pages(text, target_chars=3000)
    assert len(pages) >= 4
    assert all(len(p) <= 3000 for p in pages)
    assert sum(p.count("word") for p in pages) == 2000


def test_html_short_page_has_text():
    pages = html_pages(INJECTION.read_bytes())
    assert len(pages) == 1
    assert "€12,345 million" in pages[0]
    assert "Ignore all previous instructions" in pages[0]


def test_html_links_absolute_and_deduped():
    links = html_links(INJECTION.read_bytes(), "https://forum.example.net/t/1")
    assert links == [
        "https://example.org/collect?from=fixture",
        "https://attacker.example.net/payload",
        "https://forum.example.net/forum/thread-2",
        "https://example.com/third",
        "https://forum.example.net/t/1",
    ]


# ---------------------------------------------------------------------------
# ingest.py
# ---------------------------------------------------------------------------


async def test_ingest_pdf_stores_raw_markdown_meta_and_pages():
    url = "https://corporate.example.com/ir/annual-report-2025.pdf"
    src = make_source("S-001", url, title="Siemens Healthineers Annual Report FY2025")
    ctx = make_ctx([src], {})
    body = make_pdf(STATEMENT_PAGES)
    doc = await ing.ingest(ctx, src, fetched(url, body, "application/pdf"))

    m.Document.model_validate(doc.model_dump())
    assert doc.doc_id == "DOC-001" and doc.source_id == "S-001"
    assert doc.content_type == "pdf" and doc.paginated is True and doc.pages == 3
    assert doc.sha256 == hashlib.sha256(body).hexdigest() and doc.bytes == len(body)
    assert doc.quarantined is False and doc.scan.verdict == "skipped"
    base = "runs/run_20261107_test01/siemens-healthineers/01-Annual-Reports/S-001_siemens-healthineers-annual-report-fy2025"
    assert doc.vault_paths == m.DocVaultPaths(raw=f"{base}.pdf", text=f"{base}.md", meta=f"{base}.pdf.meta.json")
    assert ctx.read(doc.vault_paths.raw) == body
    md = ctx.text(doc.vault_paths.text)
    assert "<!-- page 1 -->" in md and "<!-- page 3 -->" in md
    assert md.index("<!-- page 2 -->") < md.index(UNIT) < md.index("<!-- page 3 -->")
    meta = json.loads(ctx.text(doc.vault_paths.meta))
    assert meta["doc_id"] == "DOC-001" and meta["sha256"] == doc.sha256 and meta["fetch"]["http_status"] == 200
    assert ctx.state.pages["DOC-001"] == pdf_pages(body)
    assert ctx.state.documents == [doc]
    assert ctx.state.sources[0].doc_ids == ["DOC-001"]
    assert ctx.state.sources[0].stats.pages == 3 and ctx.state.sources[0].stats.bytes == len(body)
    assert ctx.gateway.scans == []  # a trusted kind is never scanned


async def test_ingest_json_is_one_pretty_page():
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001932393.json"
    src = make_source("S-004", url, kind="xbrl_facts", folder="08-Competitors", title="GE HealthCare company facts")
    ctx = make_ctx([src], {})
    body = json.dumps({"cik": 1932393, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"val": 1}]}}}}}).encode()
    doc = await ing.ingest(ctx, src, fetched(url, body, "application/json"))
    assert doc.content_type == "json" and doc.paginated is False and doc.pages == 1
    assert doc.vault_paths.raw.endswith(".json") and doc.vault_paths.meta.endswith(".json.meta.json")
    page = ctx.state.pages[doc.doc_id][0]
    assert page == json.dumps(json.loads(body), indent=2, ensure_ascii=False)


async def test_ingest_sniffs_html_served_as_text_plain():
    url = "https://raw.githubusercontent.com/owner/repo/branch/page.html"
    src = make_source("S-010", url, kind="forum", tier="tier_3", folder="10-Demand-Evidence")
    ctx = make_ctx([src], {})
    body = b"<!doctype html><html><body><article><p>Plain words about revenue.</p></article></body></html>"
    doc = await ing.ingest(ctx, src, fetched(url, body, "text/plain; charset=utf-8"), agent=_agent())
    assert doc.content_type == "html" and doc.paginated is False
    assert ctx.state.pages[doc.doc_id] == ["Plain words about revenue."]


def test_parse_body_rejects_empty_html():
    with pytest.raises(ing.IngestError):
        ing.parse_body(b"<html><body></body></html>", "text/html", "https://x.example/a")


def test_doc_relpaths_follow_the_vault_layout():
    src = make_source("S-006", NEWS_URL, kind="news", tier="tier_2", folder="04-News-Coverage",
                      title="Reuters: Siemens Healthineers FY2026 results (€ 23.4 bn)")
    assert ing.doc_relpaths(src, "html") == (
        "04-News-Coverage/S-006_reuters-siemens-healthineers-fy2026-results-23-4-bn.html",
        "04-News-Coverage/S-006_reuters-siemens-healthineers-fy2026-results-23-4-bn.md",
        "04-News-Coverage/S-006_reuters-siemens-healthineers-fy2026-results-23-4-bn.html.meta.json",
    )


def _agent() -> m.Agent:
    return m.Agent(agent_id="fetcher-01", role="fetcher", status="working", task="test")


# ---------------------------------------------------------------------------
# collect stage
# ---------------------------------------------------------------------------


PDF_URL = "https://corporate.example.com/ir/annual-report-2025.pdf"
NEWS_URL = "https://news.example.com/2026/11/05/shl-results"
FORUM_URL = "https://forum.example.net/t/1"


def _three_sources() -> list[m.Source]:
    # Listed out of order on purpose: collect sorts by tier, then rank.
    return [
        make_source("S-009", FORUM_URL, kind="forum", tier="tier_3", rank=1, folder="10-Demand-Evidence"),
        make_source("S-006", NEWS_URL, kind="news", tier="tier_2", rank=1, folder="04-News-Coverage",
                    approval="auto_approved"),
        make_source("S-001", PDF_URL, title="Annual Report FY2025"),
        make_source("S-003", "https://rejected.example.com/x", rank=2, approval="rejected"),
    ]


async def test_collect_fetches_in_tier_then_rank_order_and_emits_contract_events():
    news_html, _ = long_html(12)
    ctx = make_ctx(_three_sources(), {
        PDF_URL: fetched(PDF_URL, make_pdf(STATEMENT_PAGES), "application/pdf"),
        NEWS_URL: fetched(NEWS_URL, news_html.encode(), "text/html; charset=utf-8"),
        FORUM_URL: fetched(FORUM_URL, b"<html><body><article><p>Clean forum words, no links.</p></article></body></html>",
                           "text/html"),
    })
    await collect.run(ctx)

    assert [f[1] for f in ctx.gateway.fetches] == [PDF_URL, NEWS_URL, FORUM_URL]  # S-003 rejected: never requested
    assert all(f[0] == "fetcher-01" and f[3] == "approved source" for f in ctx.gateway.fetches)
    by_id = {s.source_id: s for s in ctx.state.sources}
    assert by_id["S-001"].status == "fetched" and by_id["S-006"].status == "fetched" and by_id["S-009"].status == "fetched"
    assert by_id["S-003"].status == "queued" and by_id["S-003"].doc_ids == []
    assert [d.doc_id for d in ctx.state.documents] == ["DOC-001", "DOC-002", "DOC-003"]
    # Scans: every page of the news source and the forum page, never the annual report.
    scanned = [s[1] for s in ctx.gateway.scans]
    assert "S-001" not in scanned and scanned.count("S-006") == len(ctx.state.pages["DOC-002"]) and "S-009" in scanned
    assert ctx.state.documents[1].scan.verdict == "clean" and not ctx.state.documents[1].quarantined
    stored = ctx.events("doc.stored")
    assert [e.data.doc_id for e in stored] == ["DOC-001", "DOC-002", "DOC-003"]
    statuses = [(e.data.source_id, e.data.status) for e in ctx.events("source.status")]
    assert statuses == [("S-001", "fetching"), ("S-001", "fetched"), ("S-006", "fetching"), ("S-006", "fetched"),
                        ("S-009", "fetching"), ("S-009", "fetched")]
    assert ctx.events("agent.spawned")[0].data.agent.role == "fetcher"
    assert ctx.events("agent.status")[-1].data.status == "done"
    assert ctx.state.agents["fetcher-01"].status == "done"
    assert all(e.stage == "collect" for e in ctx.state.events)
    index = json.loads(ctx.text("00-Meta/_document-index.json"))
    assert list(index) == ["DOC-001", "DOC-002", "DOC-003"]


async def test_planted_injection_is_quarantined_and_links_are_requested_through_the_gateway():
    page = "https://raw.githubusercontent.com/owner/repo/backend/injection.html"
    src = make_source("S-008", page, kind="other", tier="tier_3", folder="10-Demand-Evidence",
                      title="Axelrod test page: planted prompt injection (simulated)")
    ctx = make_ctx([src], {page: fetched(page, INJECTION.read_bytes(), "text/plain; charset=utf-8")})
    await collect.run(ctx)

    doc = ctx.state.documents[0]
    assert doc.quarantined is True and doc.scan.verdict == "flagged"
    assert ctx.state.sources[0].status == "quarantined"
    assert ctx.events("doc.stored")[0].data.scan.verdict == "flagged"
    assert ctx.events("source.status")[-1].data.status == "quarantined"
    assert json.loads(ctx.text(doc.vault_paths.meta))["quarantined"] is True
    # Stored for the analyst, but held back from agents.
    assert ctx.state.pages[doc.doc_id] and "Ignore all previous instructions" in ctx.state.pages[doc.doc_id][0]
    # Up to 3 off-page links, other domains first, each one a gateway.fetch with the link reason.
    links = [f for f in ctx.gateway.fetches if f[3] == "link found in page"]
    assert [f[1] for f in links] == ["https://example.org/collect?from=fixture", "https://attacker.example.net/payload",
                                     "https://example.com/third"]
    assert all(f[2] is None for f in links)
    assert "blocked by the gateway" in ctx.events("agent.log")[-1].message


async def test_quarantined_document_is_absent_from_agent_pages():
    page = "https://forum.example.net/t/1"
    src = make_source("S-008", page, kind="forum", tier="tier_3", folder="10-Demand-Evidence")
    ctx = make_ctx([src], {page: fetched(page, INJECTION.read_bytes(), "text/html")})
    await collect.run(ctx)
    doc_id = ctx.state.documents[0].doc_id
    try:
        visible = ctx.state.agent_pages(doc_id)
    except NotImplementedError:
        pytest.skip("RunState.agent_pages is still a stub (B-store-api)")
    assert visible == []


async def test_blocked_failed_and_bad_bodies_are_recorded_and_collect_carries_on():
    s1 = make_source("S-001", "https://a.example.com/r.pdf", rank=1)
    s2 = make_source("S-002", "https://b.example.com/r.pdf", rank=2)
    s3 = make_source("S-003", "https://c.example.com/r.pdf", rank=3)
    s4 = make_source("S-004", "https://d.example.com/r.pdf", rank=4)
    s5 = make_source("S-005", "https://e.example.com/r.pdf", rank=5)
    ctx = make_ctx([s1, s2, s3, s4, s5], {
        s1.url: Blocked("REQ-0001", "R-reject-S-001", "Rejected at approval"),
        s2.url: fetched(s2.url, b"not found", "text/html", status=404),
        s3.url: fetched(s3.url, b"%PDF-1.7 truncated garbage", "application/pdf"),
        s4.url: RuntimeError("connection reset"),
        s5.url: fetched(s5.url, make_pdf(STATEMENT_PAGES), "application/pdf"),
    })
    await collect.run(ctx)
    by_id = {s.source_id: s for s in ctx.state.sources}
    assert by_id["S-001"].status == "blocked" and by_id["S-001"].error == "Rejected at approval"
    assert by_id["S-002"].status == "failed" and by_id["S-002"].error == "HTTP 404"
    assert by_id["S-003"].status == "failed" and "could not read" in by_id["S-003"].error
    assert by_id["S-004"].status == "failed" and "connection reset" in by_id["S-004"].error
    assert by_id["S-005"].status == "fetched"
    assert [d.source_id for d in ctx.state.documents] == ["S-005"]


async def test_revoked_source_is_still_requested_and_stays_revoked():
    src = make_source("S-006", NEWS_URL, kind="news", tier="tier_2")
    src.status = "revoked"
    ctx = make_ctx([src], {NEWS_URL: Blocked("REQ-0007", "R-revoke-S-006", "Source revoked mid-run")})
    await collect.run(ctx)
    assert [f[1] for f in ctx.gateway.fetches] == [NEWS_URL]  # the block shows in the traffic table
    assert ctx.state.sources[0].status == "revoked"
    assert [e.data.status for e in ctx.events("source.status")] == ["revoked"]


async def test_stop_ends_collect_and_marks_the_fetcher_stopped():
    s1 = make_source("S-001", "https://a.example.com/r.pdf", rank=1)
    s2 = make_source("S-002", "https://b.example.com/r.pdf", rank=2)
    pdf = make_pdf(STATEMENT_PAGES)
    ctx = make_ctx([s1, s2], {s1.url: fetched(s1.url, pdf, "application/pdf"),
                              s2.url: fetched(s2.url, pdf, "application/pdf")})
    ctx.stop_after_fetches = 1
    with pytest.raises(StopRequested):
        await collect.run(ctx)
    assert [f[1] for f in ctx.gateway.fetches] == [s1.url]
    assert ctx.state.agents["fetcher-01"].status == "stopped"
    assert ctx.events("agent.status")[-1].data.status == "stopped"


async def test_scan_error_keeps_the_document_quarantined():
    src = make_source("S-006", NEWS_URL, kind="news", tier="tier_2", folder="04-News-Coverage")
    ctx = make_ctx([src], {NEWS_URL: fetched(NEWS_URL, b"<html><body><p>Fine words.</p></body></html>", "text/html")})

    async def broken_scan(*args, **kwargs):
        raise RuntimeError("guardrail throttled")

    ctx.gateway.scan = broken_scan  # type: ignore[method-assign]
    await collect.run(ctx)
    assert ctx.state.documents[0].quarantined is True and ctx.state.documents[0].scan.verdict == "error"
    assert ctx.state.sources[0].status == "quarantined"
    assert ctx.state.documents[0].scan.detail.startswith("Scan error, untrusted source quarantined")


async def test_skipped_scan_quarantines_untrusted_sources_and_never_touches_trusted_ones():
    """B-g2fix finding 1: no guardrail configured means skipped, and skipped never releases an untrusted page."""
    news_html, _ = long_html(4)
    ctx = make_ctx(_three_sources(), {
        PDF_URL: fetched(PDF_URL, make_pdf(STATEMENT_PAGES), "application/pdf"),
        NEWS_URL: fetched(NEWS_URL, news_html.encode(), "text/html; charset=utf-8"),
        FORUM_URL: fetched(FORUM_URL, b"<html><body><article><p>Clean forum words.</p></article></body></html>",
                           "text/html"),
    })

    async def skipped_scan(agent, url, source_id, text):
        ctx.gateway.scans.append((url, source_id, text))
        return m.ScanResult(scanner="none", verdict="skipped", detail="No guardrail configured")

    ctx.gateway.scan = skipped_scan  # type: ignore[method-assign]
    await collect.run(ctx)
    docs = {d.source_id: d for d in ctx.state.documents}
    by_id = {s.source_id: s for s in ctx.state.sources}
    for sid in ("S-006", "S-009"):               # news (tier_2) and forum (tier_3): untrusted
        assert docs[sid].quarantined is True and docs[sid].scan.verdict == "skipped", sid
        assert docs[sid].scan.detail == "No guardrail configured, untrusted source quarantined"
        assert by_id[sid].status == "quarantined" and "No guardrail configured" in by_id[sid].error
        assert ctx.state.agent_pages(docs[sid].doc_id) == []
    assert docs["S-001"].quarantined is False and by_id["S-001"].status == "fetched"
    assert ctx.state.agent_pages(docs["S-001"].doc_id) == ctx.state.pages[docs["S-001"].doc_id]
    assert "S-001" not in [s[1] for s in ctx.gateway.scans]   # trusted: never scanned


async def test_ingest_without_an_agent_keeps_untrusted_sources_quarantined():
    src = make_source("S-009", FORUM_URL, kind="forum", tier="tier_3", folder="10-Demand-Evidence")
    ctx = make_ctx([src], {})
    body = b"<html><body><article><p>Clean forum words.</p></article></body></html>"
    doc = await ing.ingest(ctx, src, fetched(FORUM_URL, body, "text/html"))
    assert doc.quarantined is True and doc.scan.verdict == "skipped" and "quarantined" in doc.scan.detail
    assert ctx.gateway.scans == []


def test_held_scan_reasons():
    clean = m.ScanResult(scanner="bedrock_guardrails", verdict="clean", detail="2 page(s) scanned")
    flagged = m.ScanResult(scanner="bedrock_guardrails", verdict="flagged", detail="page 1 of 1: PROMPT_ATTACK")
    assert ing.held_scan(clean) == clean and ing.held_scan(flagged) == flagged
    fake = ing.held_scan(m.ScanResult(scanner="none", verdict="skipped", detail="FAKE_MODELS=1: guardrail not called"))
    assert fake.verdict == "skipped"
    assert fake.detail == "No guardrail configured, untrusted source quarantined (FAKE_MODELS=1: guardrail not called)"
    bare = ing.held_scan(m.ScanResult(scanner="none", verdict="skipped"))
    assert bare.detail == "No guardrail configured, untrusted source quarantined"


# ---------------------------------------------------------------------------
# document_page
# ---------------------------------------------------------------------------


async def test_document_page_highlights_from_citing_items():
    src = make_source("S-001", PDF_URL)
    ctx = make_ctx([src], {})
    doc = await ing.ingest(ctx, src, fetched(PDF_URL, make_pdf(STATEMENT_PAGES), "application/pdf"))
    text = ctx.state.pages[doc.doc_id][1]
    q_start = text.index("22,363")
    u_start = text.index(UNIT)
    item = m.DataItem(
        id="D-001", category="compounder", criteria=["secular_growth"], statement="Revenue was 22,363",
        citations=[
            m.Citation(doc_id=doc.doc_id, locator=m.TextLocator(page=2, char_start=q_start, char_end=q_start + 6),
                       quote="22,363"),
            m.Citation(doc_id=doc.doc_id, locator=m.TextLocator(page=2, char_start=u_start, char_end=u_start + len(UNIT)),
                       quote=UNIT, role="unit_context"),
        ],
        source_id="S-001", source_tier="priority", extracted_by="agent", verification=m.Verification(status="pass"),
    )
    ctx.state.items["D-001"] = item
    view = ing.document_page(ctx.state, doc.doc_id, 2)
    assert view.text == text and view.pages == 3 and view.content_type == "pdf"
    assert [(h.item_id, h.role, h.status) for h in view.highlights] == [("D-001", "unit_context", "pass"),
                                                                         ("D-001", "evidence", "pass")]
    assert view.text[view.highlights[1].char_start:view.highlights[1].char_end] == "22,363"
    assert ing.document_page(ctx.state, doc.doc_id, 1).highlights == []
    with pytest.raises(LookupError):
        ing.document_page(ctx.state, doc.doc_id, 4)
    with pytest.raises(LookupError):
        ing.document_page(ctx.state, "DOC-999", 1)


def test_no_direct_network_imports():
    """All network access goes through gateway.py: these modules never import an HTTP or AWS client."""
    root = Path(__file__).resolve().parents[1] / "axelrod"
    for rel in ("fetch/pdf.py", "fetch/html.py", "fetch/ingest.py", "stages/collect.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for banned in ("import httpx", "import boto3", "import requests", "urllib.request", "fetch.http",
                       "import bedrock", "bedrock import", "bedrock-runtime"):
            assert banned not in src, f"{rel} mentions {banned}"


def test_ingest_one_parse_range():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import ingest_one

    assert ingest_one.parse_range("150-156") == (150, 156)
    assert ingest_one.parse_range("12") == (12, 12)
    for bad in ("0-3", "5-2", "a-b", "1-2-3"):
        with pytest.raises(ValueError):
            ingest_one.parse_range(bad)


async def test_gateway_error_marks_the_source_failed_not_blocked():
    src = make_source("S-001", PDF_URL)
    ctx = make_ctx([src], {PDF_URL: Blocked("REQ-0001", None, "gateway error: ConnectTimeout: timed out")})
    await collect.run(ctx)
    assert ctx.state.sources[0].status == "failed"
    assert "ConnectTimeout" in ctx.state.sources[0].error


async def test_real_gateway_flags_the_planted_page_and_blocks_its_links(settings, monkeypatch):
    """Real Store, RunContext and Gateway. Only the HTTP call and the guardrail are faked (simulated)."""
    from backend.axelrod import bedrock
    from backend.axelrod.fetch import http
    from backend.axelrod.runtime import RunContext
    from backend.axelrod.store import Store

    page = "https://raw.githubusercontent.com/owner/repo/backend/backend/data/test_pages/injection.html"
    body = INJECTION.read_bytes()
    let_through: list[str] = []

    async def fake_get_once(url: str, *, user_agent: str, **_: object) -> tuple[int, dict[str, str], bytes]:
        let_through.append(url)
        return 200, {"content-type": "text/plain; charset=utf-8"}, body

    monkeypatch.setattr(http, "get_once", fake_get_once)
    bedrock.register_fake_scan(lambda text: "ignore all previous instructions" in text.lower())
    try:
        store = Store(settings)
        state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
        state.run.stage = "collect"
        state.sources = [make_source("S-008", page, kind="other", tier="tier_3", folder="10-Demand-Evidence",
                                     approval="auto_approved",
                                     title="Axelrod test page: planted prompt injection (simulated)")]
        ctx = RunContext.create(state, store, store.vault, settings)
        ctx.gateway.build_policy()
        await collect.run(ctx)
    finally:
        bedrock.register_fake_scan(None)

    assert let_through == [page]  # nothing but the approved page left the gateway
    reqs = [e.data for e in state.events if e.type == "gateway.request"]
    assert (reqs[0].decision, reqs[0].target, reqs[0].source_id) == ("allowed", page, "S-008")
    assert [r.decision for r in reqs if r.target == page] == ["allowed", "flagged"]
    blocked = [r for r in reqs if r.decision == "blocked"]
    assert [r.target for r in blocked] == ["https://example.org/collect?from=fixture",
                                           "https://attacker.example.net/payload", "https://example.com/third"]
    assert all(r.reason.startswith("link found in page") and r.role == "fetcher" for r in blocked)
    doc = state.documents[0]
    assert doc.quarantined and doc.scan.verdict == "flagged"
    assert state.sources[0].status == "quarantined"
    assert state.agent_pages(doc.doc_id) == []
    assert doc.vault_paths.raw.startswith(f"runs/{state.run.run_id}/siemens-healthineers/10-Demand-Evidence/S-008_")
    assert store.vault.get_bytes(state.run.run_id, doc.vault_paths.raw) == body


async def test_planted_page_is_quarantined_with_guardrail_id_unset(settings, monkeypatch):
    """B-g2fix finding 1: a deploy with no GUARDRAIL_ID (and no fakes) must still hold the planted page back.

    Real Store, RunContext and Gateway, the real planted test page (simulated), only the HTTP call faked.
    Nothing reaches AWS: with no guardrail id the scan is skipped before any client is made.
    """
    from backend.axelrod import bedrock
    from backend.axelrod.fetch import http
    from backend.axelrod.runtime import RunContext
    from backend.axelrod.store import Store

    planted = Path(__file__).resolve().parents[1] / "data" / "test_pages" / "injection.html"
    page = "http://127.0.0.1:8000/test-pages/injection.html"
    body = planted.read_bytes()
    misconfigured = settings.model_copy(update={"fake_models": False, "guardrail_id": None,
                                                "guardrail_version": None})
    let_through: list[str] = []

    async def fake_get_once(url: str, *, user_agent: str, **_: object) -> tuple[int, dict[str, str], bytes]:
        let_through.append(url)
        return 200, {"content-type": "text/html; charset=utf-8", "x-axelrod-simulated": "true"}, body

    monkeypatch.setattr(http, "get_once", fake_get_once)
    bedrock.register_fake_scan(None)
    store = Store(misconfigured)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
    state.run.stage = "collect"
    state.sources = [make_source("S-010", page, kind="other", tier="tier_3", folder="10-Demand-Evidence",
                                 approval="auto_approved",
                                 title="Axelrod test page: planted prompt injection (simulated)")]
    ctx = RunContext.create(state, store, store.vault, misconfigured)
    ctx.gateway.build_policy()
    await collect.run(ctx)

    assert let_through == [page]
    doc = state.documents[0]
    assert doc.quarantined is True and doc.scan.verdict == "skipped"
    assert doc.scan.detail == "No guardrail configured, untrusted source quarantined"
    assert state.sources[0].status == "quarantined" and "No guardrail configured" in state.sources[0].error
    assert state.agent_pages(doc.doc_id) == []
    assert "Ignore all previous instructions" in "".join(state.pages[doc.doc_id])   # stored for the analyst only
    reqs = [e.data for e in state.events if e.type == "gateway.request"]
    assert [r.decision for r in reqs if r.target == page] == ["allowed"]           # no scan ran, so no flag
    assert [r.target for r in reqs if r.decision == "blocked"] == ["https://example.org/axelrod-test/shl-buyer-survey"]
