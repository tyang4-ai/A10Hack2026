"""B-pdfpage: stored PDF pages as PNG with the cited quotes highlighted (docimage.page_image and its route).

The PDFs here are generated in the test (simulated statements), ingested the way collect does it.
"""
from __future__ import annotations

import asyncio

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.axelrod import docimage
from backend.axelrod.api import create_app
from backend.axelrod.contract import m
from backend.axelrod.fetch import ingest as ing
from backend.axelrod.gateway import FetchResult
from backend.axelrod.runtime import RunContext
from backend.axelrod.store import Store
from backend.axelrod.store.vault import register_replay, register_run
from backend.axelrod.verify.text import find_quote

P = "/api/v1"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PDF_URL = "https://corporate.example.com/ir/annual-report-2025.pdf"
HTML_URL = "https://corporate.example.com/ir/press-release.html"
QUOTE = "Revenue 22,363"
MISSING = "Operating margin 99.9%"

# Page 2 is a table: label and two year columns, each cell placed on its own, like a real statement.
TABLE = [("in millions of EUR", "FY2025", "FY2024"), ("Revenue", "22,363", "21,680"),
         ("Cost of sales", "(13,402)", "(13,180)"), ("Gross profit", "8,961", "8,500")]


@pytest.fixture(autouse=True)
def _fresh_caches():
    docimage._images.clear()
    docimage._pdfs.clear()
    yield
    docimage._images.clear()
    docimage._pdfs.clear()


def make_pdf() -> bytes:
    """Two pages (simulated): a cover and a small income statement table."""
    doc = pymupdf.open()
    font = pymupdf.Font("helv")
    cover = doc.new_page()
    w = pymupdf.TextWriter(cover.rect)
    w.append((72, 90), "Annual Report FY2025 (simulated)", font=font, fontsize=14)
    w.write_text(cover)
    page = doc.new_page()
    w = pymupdf.TextWriter(page.rect)
    w.append((72, 90), "Consolidated statements of income", font=font, fontsize=12)
    for row, cells in enumerate(TABLE):
        y = 130 + row * 20
        for x, cell in zip((72, 300, 420), cells):
            w.append((x, y), cell, font=font, fontsize=11)
    w.write_text(page)
    data = doc.tobytes()
    doc.close()
    return data


def source(sid: str, url: str, kind: str) -> m.Source:
    return m.Source(
        source_id=sid, title=f"Simulated {kind} {sid}", url=url, domain="corporate.example.com", kind=kind,
        data_types=["other"], access=m.SourceAccess(), tier="priority", rank=1,
        scores=m.SourceScores(reliability=0.9, relevance=0.9), rationale="test", need_ids=["N-01"],
        criteria=["rotic_level"], vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved",
        status="queued")


def item(item_id: str, doc_id: str, page: int, quote: str, span: tuple[int, int], status: str) -> m.DataItem:
    return m.DataItem(
        id=item_id, category="financial_strength", criteria=["rotic_level"], statement=f"{item_id} (test)",
        citations=[m.Citation(doc_id=doc_id, quote=quote,
                              locator=m.TextLocator(page=page, char_start=span[0], char_end=span[1]))],
        source_id="S-001", source_tier="priority", extracted_by="agent",
        verification=m.Verification(status=status))


async def ingest_run(store: Store) -> tuple[RunContext, m.Document, m.Document]:
    """A run with a PDF (DOC-001) and an HTML page (DOC-002), stored by ingest, plus three items on PDF
    page 2: D-001 pass with offsets, D-002 fail whose quote is nowhere, D-003 fixed found by quote only."""
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
    state.sources = [source("S-001", PDF_URL, "annual_report"), source("S-002", HTML_URL, "company_ir")]
    ctx = RunContext.create(state, store, store.vault, store.settings, gateway=object())  # type: ignore[arg-type]
    pdf = await ing.ingest(ctx, state.sources[0], FetchResult(PDF_URL, PDF_URL, 200, "application/pdf",
                                                              make_pdf(), "REQ-0001"))
    html = await ing.ingest(ctx, state.sources[1], FetchResult(
        HTML_URL, HTML_URL, 200, "text/html", b"<html><body><p>Revenue grew (simulated).</p></body></html>",
        "REQ-0002"))
    text = state.pages[pdf.doc_id][1]
    span = find_quote(text, QUOTE)
    assert span is not None, text
    state.items["D-001"] = item("D-001", pdf.doc_id, 2, QUOTE, span, "pass")
    state.items["D-002"] = item("D-002", pdf.doc_id, 2, MISSING, (0, 0), "fail")
    state.items["D-003"] = item("D-003", pdf.doc_id, 2, "Gross profit 8,961", (0, 0), "fixed")
    return ctx, pdf, html


class Rt:
    """The Runtime surface page_image uses."""

    def __init__(self, store: Store) -> None:
        self.vault = store.vault


def quote_area(pdf_bytes: bytes, needle: str) -> pymupdf.Rect:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        hits = doc[1].search_for(needle)
    assert hits, needle
    return hits[0]


def tint(png: bytes, rect: pymupdf.Rect, zoom: float) -> tuple[int, int, int]:
    """Per channel count of tinted pixels inside rect: (green-ish, red-ish, amber-ish)."""
    pix = pymupdf.Pixmap(png)
    green = red = amber = 0
    for y in range(int(rect.y0 * zoom) + 1, int(rect.y1 * zoom)):
        for x in range(int(rect.x0 * zoom) + 1, int(rect.x1 * zoom)):
            r, g, b = pix.pixel(x, y)[:3]
            green += g > r + 25 and g > b + 15
            red += r > g + 60 and r > b + 60
            amber += r > b + 80 and g > b + 40 and r >= g
    return green, red, amber


async def test_page_two_renders_with_the_quote_highlighted(settings):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    png = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-001")
    assert png[:8] == PNG_SIG and len(png) < docimage.MAX_PNG_BYTES
    pix = pymupdf.Pixmap(png)
    assert pix.width == 952 and abs(pix.height - 842 * 1.6) <= 1           # A4 at 1.6x, under the cap
    zoom = pix.width / 595
    data = store.vault.get_bytes(ctx.state.run.run_id, pdf.vault_paths.raw)
    green, red, _ = tint(png, quote_area(data, "22,363"), zoom)
    assert green > 50 and red == 0
    # The row next to it is not cited by D-001, so it stays white and black.
    assert tint(png, quote_area(data, "21,680"), zoom) == (0, 0, 0)


async def test_all_items_view_uses_status_colours_and_finds_a_quote_without_offsets(settings):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    png = await docimage.page_image(Rt(store), ctx.state, pdf, 2, None)
    data = store.vault.get_bytes(ctx.state.run.run_id, pdf.vault_paths.raw)
    zoom = pymupdf.Pixmap(png).width / 595
    assert tint(png, quote_area(data, "22,363"), zoom)[0] > 50                 # D-001 pass: green
    assert tint(png, quote_area(data, "8,961"), zoom)[2] > 50                  # D-003 fixed: amber, by quote search


async def test_quote_not_found_still_gives_the_plain_page(settings):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    png = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-002")
    data = store.vault.get_bytes(ctx.state.run.run_id, pdf.vault_paths.raw)
    assert png[:8] == PNG_SIG
    assert png == docimage.render_page(data, 2, ctx.state.pages[pdf.doc_id][1], [])
    unknown = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-999")
    assert unknown == png


async def test_html_document_has_no_pdf(settings):
    store = Store(settings)
    ctx, _, html = await ingest_run(store)
    with pytest.raises(docimage.NoPdf):
        await docimage.page_image(Rt(store), ctx.state, html, 1, None)


async def test_replay_without_the_pdf_file_has_no_pdf(settings, tmp_path):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    replay = ctx.state.model_copy(deep=True)
    replay.run.run_id, replay.run.mode = "run_20261108_replay", "replay"
    register_run(replay.run.run_id, replay.run.company)
    folder = tmp_path / "replay_vault"
    folder.mkdir()
    register_replay(replay.run.run_id, folder)          # exports keep text only, no PDF
    doc = replay.documents[0].model_copy(update={"vault_paths": m.DocVaultPaths(
        raw=pdf.vault_paths.raw.replace(ctx.state.run.run_id, replay.run.run_id), text="t", meta="x")})
    with pytest.raises(docimage.NoPdf, match="no stored PDF"):
        await docimage.page_image(Rt(store), replay, doc, 2, None)


async def test_a_path_outside_the_run_or_a_changed_file_is_refused(settings):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    other = store.create_run(m.CreateRunRequest(company="Other Company AG"))
    foreign = pdf.model_copy(update={"vault_paths": pdf.vault_paths.model_copy(
        update={"raw": pdf.vault_paths.raw.replace(f"runs/{ctx.state.run.run_id}/siemens-healthineers",
                                                   f"runs/{other.run.run_id}/other-company")})})
    with pytest.raises(docimage.NoPdf, match="outside"):
        await docimage.page_image(Rt(store), ctx.state, foreign, 2, None)
    store.vault.put_bytes(ctx.state.run.run_id, pdf.vault_paths.raw, make_pdf() + b"\n% changed")
    with pytest.raises(docimage.NoPdf, match="hash"):
        await docimage.page_image(Rt(store), ctx.state, pdf, 2, None)


async def test_page_outside_the_pdf_has_no_pdf(settings):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    with pytest.raises(docimage.NoPdf, match="pages 1 to 2"):
        await docimage.page_image(Rt(store), ctx.state, pdf, 3, None)


async def test_second_call_comes_from_the_cache(settings, monkeypatch):
    store = Store(settings)
    ctx, pdf, _ = await ingest_run(store)
    calls = []
    real = docimage.render_page

    def counting(*args):
        calls.append(args[1])
        return real(*args)

    monkeypatch.setattr(docimage, "render_page", counting)
    first = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-001")
    again = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-001")
    assert again is first and calls == [2]
    ctx.state.items["D-001"].verification.status = "fail"           # a new status is a new image
    red = await docimage.page_image(Rt(store), ctx.state, pdf, 2, "D-001")
    assert red != first and calls == [2, 2]
    docimage.forget_run(ctx.state.run.run_id)
    assert len(docimage._images) == 0 and len(docimage._pdfs) == 0


def test_lru_evicts_the_oldest_and_caps_bytes():
    lru = docimage.Lru(2, max_bytes=10)
    lru.put("a", b"1234")
    lru.put("b", b"1234")
    assert lru.get("a") == b"1234"                  # a is now the newest
    lru.put("c", b"1234")
    assert lru.get("b") is None and lru.get("a") and lru.get("c")
    lru.put("d", b"123456789")
    assert len(lru) == 1 and lru.get("d")


def test_quote_search_survives_line_breaks_and_hyphens():
    words = [(0, 0, 10, 5, "Revenue", 0, 0, 0), (12, 0, 20, 5, "in-", 0, 0, 1), (0, 6, 10, 11, "creased", 0, 1, 0),
             (12, 6, 20, 11, "sharply", 0, 1, 1)]
    spans = [None] * len(words)
    hits = docimage.quote_boxes(words, spans, "Revenue increased\n sharply", None)
    assert [i for i, _ in hits] == [0, 1, 2, 3]
    assert docimage.line_rects(words, hits) == [(0, 0, 20, 5), (0, 6, 20, 11)]
    assert docimage.quote_boxes(words, spans, "nothing like it", None) == []


def test_route_answers_png_and_404_envelope(settings):
    with TestClient(create_app(settings)) as client:
        rt = client.app.state.rt
        ctx, pdf, html = asyncio.run(ingest_run(rt.store))
        run_id = ctx.state.run.run_id
        res = client.get(f"{P}/runs/{run_id}/documents/{pdf.doc_id}/pages/2/image", params={"item_id": "D-001"})
        assert res.status_code == 200 and res.headers["content-type"] == "image/png"
        assert res.content[:8] == PNG_SIG
        for url in (f"{P}/runs/{run_id}/documents/{html.doc_id}/pages/1/image",
                    f"{P}/runs/{run_id}/documents/{pdf.doc_id}/pages/3/image"):
            res = client.get(url)
            assert res.status_code == 404
            assert res.json()["error"]["code"] == "not_found"
