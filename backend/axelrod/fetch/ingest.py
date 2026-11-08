"""Ingest: a fetched source becomes a Document with exact page texts in RunState and its files in the vault
(raw bytes, <file>.md with page markers, <file>.<ext>.meta.json). Also builds DocumentPage views for the API.

The stored page text is the ground truth: extractors read it, the checker matches quotes against it and
code computes every offset into it. So it is stored once, exactly, and never rewritten.

Untrusted sources (news, forum, other, and anything in tier_3) start quarantined and are only released
when the Guardrails scan through the gateway comes back clean. Flagged, error and skipped (no guardrail
configured) all keep the whole document quarantined (fail closed), and RunState.agent_pages hides it.
Trusted sources are never scanned and never quarantined.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..contract import m
from ..ids import now_iso
from .html import decode_html, html_pages, split_pages
from .pdf import pdf_pages

if TYPE_CHECKING:
    from ..gateway import FetchResult
    from ..runtime import RunContext
    from ..store.state import RunState

UNTRUSTED_KINDS = frozenset({"news", "forum", "other"})
# Why an untrusted document stays quarantined when its scan was not clean (flagged keeps its own detail).
HELD_REASONS = {
    "skipped": "No guardrail configured, untrusted source quarantined",
    "error": "Scan error, untrusted source quarantined",
}
TARGET_CHARS = 3000
EXT = {"pdf": "pdf", "html": "html", "json": "json", "text": "txt"}
INDEX_RELPATH = "00-Meta/_document-index.json"


class IngestError(ValueError):
    """The fetched body could not be turned into page text. Collect marks the source failed."""


@dataclass
class Parsed:
    content_type: str            # "pdf" | "html" | "json" | "text"
    paginated: bool              # True only for real PDF pages
    pages: list[str]


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "document"


def doc_relpaths(source: m.Source, content_type: str) -> tuple[str, str, str]:
    """(raw, text, meta) relpaths per CONTRACT.md section 10. The Vault puts runs/<run_id>/<slug>/ in front."""
    ext = EXT[content_type]
    base = f"{source.vault_folder}/{source.source_id}_{slugify(source.title)}"
    return f"{base}.{ext}", f"{base}.md", f"{base}.{ext}.meta.json"


def is_untrusted(source: m.Source) -> bool:
    """Kinds and tiers whose text must pass the Guardrails scan before any agent reads it."""
    return source.kind in UNTRUSTED_KINDS or source.tier == "tier_3"


# ---------------------------------------------------------------------------
# Parsing (sync, CPU bound: callers run it in a thread)
# ---------------------------------------------------------------------------


def detect_type(body: bytes, content_type: str, url: str) -> str:
    """pdf, json, html or text, from the bytes first and the header second.

    raw.githubusercontent.com serves .html as text/plain, so HTML is also sniffed from the markup.
    """
    ct = (content_type or "").lower()
    head = body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"%PDF") or "application/pdf" in ct:
        return "pdf"
    if "json" in ct or head[:1] in (b"{", b"["):
        try:
            json.loads(body)
            return "json"
        except ValueError:
            pass
    low = head.lower()
    path = urlsplit(url).path.lower()
    if ("html" in ct or low.startswith((b"<!doctype html", b"<html")) or b"<html" in low
            or path.endswith((".html", ".htm"))):
        return "html"
    return "text"


def parse_body(body: bytes, content_type: str, url: str) -> Parsed:
    """Page texts for one fetched body. Raises IngestError when there is no text to store."""
    kind = detect_type(body, content_type, url)
    if kind == "pdf":
        pages = pdf_pages(body)
        if not pages:
            raise IngestError("the PDF has no pages")
        if not any(p.strip() for p in pages):
            raise IngestError("the PDF has no text layer")
        return Parsed("pdf", True, pages)
    if kind == "json":
        pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        return Parsed("json", False, [pretty])
    text = decode_html(body, content_type)
    pages = html_pages(text, target_chars=TARGET_CHARS) if kind == "html" else split_pages(text, target_chars=TARGET_CHARS)
    if not pages:
        raise IngestError(f"no text found in the {kind} body")
    return Parsed(kind, False, pages)


def markdown_text(pages: list[str]) -> str:
    """The .md copy: every page behind a <!-- page n --> marker, n 1-based."""
    return "".join(f"<!-- page {i} -->\n{text}\n\n" for i, text in enumerate(pages, start=1))


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


async def ingest(ctx: RunContext, source: m.Source, result: FetchResult, *,
                 agent: m.Agent | None = None) -> m.Document:
    """Store result's body for source and return the Document (pages go into ctx.state.pages).

    With an agent, untrusted sources are scanned page by page through ctx.gateway.scan, and only a clean
    verdict releases them. Without one they stay quarantined, because nothing has cleared them.
    """
    parsed = await asyncio.to_thread(parse_body, result.body, result.content_type, result.final_url or result.url)
    untrusted = is_untrusted(source)
    if untrusted:
        scan = (held_scan(await scan_pages(ctx, agent, source, result.url, parsed.pages)) if agent is not None
                else m.ScanResult(scanner="none", verdict="skipped",
                                  detail="Untrusted source, not scanned: quarantined"))
    else:
        scan = m.ScanResult(scanner="none", verdict="skipped", detail=f"Trusted source kind ({source.kind}), not scanned")
    quarantined = untrusted and (agent is None or scan.verdict != "clean")

    doc_id = ctx.next_id("DOC")
    run_id = ctx.state.run.run_id
    raw_rel, text_rel, meta_rel = doc_relpaths(source, parsed.content_type)
    raw_path = await asyncio.to_thread(ctx.vault.put_bytes, run_id, raw_rel, result.body)
    text_path = await asyncio.to_thread(ctx.vault.put_text, run_id, text_rel, markdown_text(parsed.pages))
    doc = m.Document(
        doc_id=doc_id, source_id=source.source_id, title=source.title, url=source.url,
        content_type=parsed.content_type, paginated=parsed.paginated, pages=len(parsed.pages),
        sha256=hashlib.sha256(result.body).hexdigest(), bytes=len(result.body), fetched_at=now_iso(),
        vault_paths=m.DocVaultPaths(raw=raw_path, text=text_path, meta=""), quarantined=quarantined, scan=scan,
    )
    doc.vault_paths.meta = await asyncio.to_thread(ctx.vault.put_text, run_id, meta_rel, meta_json(doc, result))

    ctx.state.pages[doc_id] = parsed.pages
    ctx.state.documents.append(doc)
    held = state_source(ctx.state, source.source_id) or source
    if doc_id not in held.doc_ids:
        held.doc_ids.append(doc_id)
    held.stats = m.SourceStats(bytes=len(result.body), pages=len(parsed.pages), data_items=held.stats.data_items,
                               data_items_verified=held.stats.data_items_verified)
    return doc


async def scan_pages(ctx: RunContext, agent: m.Agent, source: m.Source, url: str, pages: list[str]) -> m.ScanResult:
    """Guardrails tripwire on every page. Stops at the first flagged page. Any scan error counts as error."""
    from ..runtime import StopRequested

    for n, text in enumerate(pages, start=1):
        ctx.check_stop()
        try:
            result = await ctx.gateway.scan(agent, url, source.source_id, text)
        except StopRequested:
            raise
        except Exception as exc:  # noqa: BLE001  fail closed: an unscanned page is never released
            return m.ScanResult(scanner="bedrock_guardrails", verdict="error", detail=f"page {n}: scan failed: {exc}")
        if result.verdict in ("flagged", "error"):
            return m.ScanResult(scanner=result.scanner, verdict=result.verdict,
                                detail=f"page {n} of {len(pages)}: {result.detail or result.verdict}")
        if result.verdict == "skipped":
            return result  # no guardrail configured: every page would say the same
    return m.ScanResult(scanner="bedrock_guardrails", verdict="clean",
                        detail=f"{len(pages)} page(s) scanned, no prompt attack detected")


def held_scan(scan: m.ScanResult) -> m.ScanResult:
    """The scan with a plain quarantine reason when its verdict is skipped or error (fail closed).

    A misconfigured deploy (no GUARDRAIL_ID) must never release an untrusted page, so skipped holds it too.
    """
    reason = HELD_REASONS.get(scan.verdict)
    if reason is None:
        return scan
    detail = scan.detail or ""
    if detail and detail.lower() not in reason.lower():
        reason = f"{reason} ({detail})"
    return scan.model_copy(update={"detail": reason})


def meta_json(doc: m.Document, result: FetchResult) -> str:
    """The <file>.meta.json sidecar: the Document plus what the fetch saw."""
    meta = doc.model_dump(mode="json")
    meta["vault_paths"].pop("meta", None)
    meta["fetch"] = {"request_id": result.request_id, "url": result.url, "final_url": result.final_url,
                     "http_status": result.http_status, "content_type": result.content_type}
    return json.dumps(meta, indent=2, ensure_ascii=False)


def state_source(state: RunState, source_id: str) -> m.Source | None:
    return next((s for s in state.sources if s.source_id == source_id), None)


def document_index(state: RunState) -> str:
    """00-Meta/_document-index.json: doc id to its raw file, same shape as the fixture."""
    return json.dumps({d.doc_id: d.vault_paths.raw for d in state.documents}, indent=2)


# ---------------------------------------------------------------------------
# API view
# ---------------------------------------------------------------------------


def document_page(state: RunState, doc_id: str, page: int) -> m.DocumentPage:
    """One page with highlights from the items citing it.

    Raises KeyError for an unknown document and IndexError for a page outside 1..pages (both LookupError,
    which the API turns into 404).
    """
    doc = next((d for d in state.documents if d.doc_id == doc_id), None)
    if doc is None or doc_id not in state.pages:
        raise KeyError(f"{doc_id} has not been fetched in this run")
    texts = state.pages[doc_id]
    if not 1 <= page <= len(texts):
        raise IndexError(f"{doc_id} has pages 1 to {len(texts)}")
    highlights = [
        m.Highlight(item_id=item.id, char_start=c.locator.char_start, char_end=c.locator.char_end,
                    status=item.verification.status, role=c.role)
        for item in state.items.values()
        for c in getattr(item, "citations", None) or []
        if c.doc_id == doc_id and c.locator.type == "text" and c.locator.page == page
    ]
    highlights.sort(key=lambda h: (h.char_start, h.char_end, h.item_id))
    return m.DocumentPage(doc_id=doc_id, page=page, pages=len(texts), content_type=doc.content_type,
                          text=texts[page - 1], highlights=highlights)
