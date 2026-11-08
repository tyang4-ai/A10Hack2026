"""PDF page images for the document viewer (v0.3.1). Owned by B-pdfpage.

The text viewer shows the extracted page text, where tables come out as jumbled lines. This renders the
stored PDF page itself as a PNG, with the cited quotes highlighted in their verification colour.

Where the boxes come from: a citation's char offsets point into the stored page text, which PyMuPDF wrote
at ingest (`fetch/pdf.py`, page n is PDF page index n-1, every page of the file, whatever extract_pages
says). PyMuPDF's word boxes for the same page are lined up with that text, so a span maps straight onto
the words that hold it and the box lands on the span the text viewer highlights. When the offsets don't
map (pypdf text, an empty locator) the quote itself is searched in the page's words, whitespace and
hyphenation ignored, and then piece by piece. No match means a plain page, never an error.

Security: the PDF is read through the run's own Document.vault_paths.raw, which the Vault keeps inside the
run's folder, and its hash must match Document.sha256. Nothing from the request becomes a path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ZOOM = 1.6                      # render scale (72 dpi points to pixels)
MAX_WIDTH_PX = 1400
MAX_HEIGHT_PX = 2200
MIN_ZOOM = 0.6                  # lowest scale the size cap may step down to
MAX_PNG_BYTES = 1_500_000       # far under API Gateway's 10 MB, and quick to load
IMAGE_CACHE_ENTRIES = 64
PDF_CACHE_ENTRIES = 3           # SHL's annual reports are 1.5 to 4 MB, others can be 30
PDF_CACHE_BYTES = 96 * 1024 * 1024
RENDER_SLOTS = 2                # renders at once, so a burst of clicks can't eat the memory
WORD_WINDOW = 400               # how far past the cursor a PyMuPDF word may sit in the stored text
MIN_PIECE_CHARS = 6             # a quote line shorter than this is too common to search on its own
SOFT_HYPHEN = chr(0xAD)
PAD = 1.0                       # points around each highlight box

# Same meaning as the text viewer's highlight colours: pass green, fixed amber, fail red.
COLORS: dict[str, tuple[float, float, float]] = {
    "pass": (0.16, 0.70, 0.35),
    "fixed": (0.98, 0.65, 0.05),
    "fail": (0.90, 0.18, 0.18),
    "pending": (0.45, 0.52, 0.62),
    "blocked": (0.45, 0.52, 0.62),
}
OPACITY = {"evidence": 0.5, "unit_context": 0.28, "period_context": 0.28}
STATUS_RANK = {"fail": 4, "fixed": 3, "blocked": 2, "pending": 1, "pass": 0}   # which mark wins a shared box

Rect4 = tuple[float, float, float, float]   # x0, y0, x1, y1 in PDF points


class NoPdf(LookupError):
    """The document has no stored PDF (HTML or JSON source, a replay, or the file is missing)."""


@dataclass(frozen=True)
class Mark:
    """One cited quote to box on the page. start and end index the stored page text (end exclusive)."""

    item_id: str
    start: int
    end: int
    quote: str
    status: str
    role: str = "evidence"


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


class Lru:
    """A small thread-safe LRU of bytes, capped by entry count and optionally by total size."""

    def __init__(self, entries: int, max_bytes: int | None = None) -> None:
        self.entries = entries
        self.max_bytes = max_bytes
        self._data: OrderedDict[Hashable, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: Hashable, value: bytes) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._data[key] = value
            self._size += len(value)
            while len(self._data) > self.entries or (
                    self.max_bytes is not None and self._size > self.max_bytes and len(self._data) > 1):
                _, dropped = self._data.popitem(last=False)
                self._size -= len(dropped)

    def drop(self, keep: Callable[[Hashable], bool]) -> None:
        """Remove every entry whose key keep() rejects."""
        with self._lock:
            for key in [k for k in self._data if not keep(k)]:
                self._size -= len(self._data.pop(key))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._size = 0

    def __len__(self) -> int:
        return len(self._data)


_images = Lru(IMAGE_CACHE_ENTRIES)
_pdfs = Lru(PDF_CACHE_ENTRIES, PDF_CACHE_BYTES)
_render_slots = threading.BoundedSemaphore(RENDER_SLOTS)


def forget_run(run_id: str) -> None:
    """Drop a run's cached PDFs and images (after the run is deleted). Keys start with the run id."""
    _images.drop(lambda k: k[0] != run_id)
    _pdfs.drop(lambda k: k[0] != run_id)


# ---------------------------------------------------------------------------
# Entry point (api.read_page_image)
# ---------------------------------------------------------------------------


async def page_image(rt: Any, state: Any, doc: Any, page: int, item_id: str | None) -> bytes:
    """PNG of PDF page `page` (1-based, the stored text page number) with item_id's quotes highlighted,
    or every quote cited on that page when item_id is None. Raises NoPdf when there is no stored PDF."""
    if doc.content_type != "pdf" or not doc.paginated:
        raise NoPdf(f"{doc.doc_id} is a {doc.content_type} document with no stored PDF, use the text page")
    run_id = state.run.run_id
    marks = page_marks(state, doc.doc_id, page, item_id)
    # Statuses change while a run verifies, so the marks are part of the key and a cached image never lies.
    key = (run_id, doc.doc_id, page, item_id or "", doc.sha256, tuple(marks))
    png = _images.get(key)
    if png is not None:
        return png
    data = await pdf_bytes(rt, run_id, doc)
    text = page_text(state, doc.doc_id, page)
    png = await asyncio.to_thread(render_page, data, page, text, marks)
    _images.put(key, png)
    return png


def page_marks(state: Any, doc_id: str, page: int, item_id: str | None) -> list[Mark]:
    """The text citations on this page, the same set ingest.document_page turns into highlights."""
    marks: set[Mark] = set()
    for item in state.items.values():
        if item_id is not None and item.id != item_id:
            continue
        for c in getattr(item, "citations", None) or []:
            if c.doc_id == doc_id and c.locator.type == "text" and c.locator.page == page:
                status = getattr(getattr(item, "verification", None), "status", "pending")
                marks.add(Mark(item.id, c.locator.char_start, c.locator.char_end, c.quote, status, c.role))
    return sorted(marks, key=lambda k: (k.start, k.end, k.item_id, k.role, k.quote))


def page_text(state: Any, doc_id: str, page: int) -> str | None:
    texts = state.pages.get(doc_id) or []
    return texts[page - 1] if 1 <= page <= len(texts) else None


async def pdf_bytes(rt: Any, run_id: str, doc: Any) -> bytes:
    """The stored PDF, from a small cache or the vault (S3 in aws mode), checked against the doc's hash."""
    key = (run_id, doc.doc_id, doc.sha256)
    data = _pdfs.get(key)
    if data is None:
        data = await asyncio.to_thread(read_pdf, rt.vault, run_id, doc)
        _pdfs.put(key, data)
    return data


def read_pdf(vault: Any, run_id: str, doc: Any) -> bytes:
    """Read Document.vault_paths.raw through the run's Vault. Every failure is NoPdf, so the viewer falls
    back to the text page instead of showing an error."""
    try:
        data = vault.get_bytes(run_id, doc.vault_paths.raw)
    except KeyError:
        raise NoPdf(f"{doc.doc_id} has no stored PDF in this run's vault (replays and exports keep text only)") \
            from None
    except ValueError:
        raise NoPdf(f"{doc.doc_id}'s stored file is outside this run's vault") from None
    except Exception as e:  # noqa: BLE001  an S3 hiccup still leaves the text page to show
        log.warning("reading %s for %s failed: %s", doc.vault_paths.raw, run_id, e)
        raise NoPdf(f"{doc.doc_id}'s stored PDF could not be read") from None
    if b"%PDF" not in data[:1024]:
        raise NoPdf(f"{doc.doc_id}'s stored file is not a PDF")
    if hashlib.sha256(data).hexdigest() != doc.sha256:
        raise NoPdf(f"{doc.doc_id}'s stored file does not match the document's hash")
    return data


# ---------------------------------------------------------------------------
# Rendering (sync, CPU bound: runs in a thread)
# ---------------------------------------------------------------------------


def render_page(data: bytes, page_no: int, text: str | None, marks: list[Mark]) -> bytes:
    """Open the PDF fresh (highlights are added as annotations, so a shared document would keep them),
    draw every mark, and return the page as a PNG."""
    import pymupdf

    with _render_slots:
        try:
            pdf = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:  # noqa: BLE001
            raise NoPdf(f"the stored file is not a readable PDF ({e})") from None
        with pdf:
            if not 1 <= page_no <= pdf.page_count:
                raise NoPdf(f"the stored PDF has pages 1 to {pdf.page_count}")
            pg = pdf[page_no - 1]
            if pg.rotation:
                try:
                    pg.remove_rotation()  # words and annotations then share the displayed coordinates
                except Exception:  # noqa: BLE001  a rotated page with odd boxes still renders, maybe offset
                    pass
            if marks:
                words = pg.get_text("words")
                spans = word_spans(words, text) if text else [None] * len(words)
                for rect, mark in unique_boxes(words, spans, marks).items():
                    draw(pg, [rect], mark)
            return to_png(pg)



def unique_boxes(words: list[tuple], spans: list[tuple[int, int] | None],
                 marks: list[Mark]) -> dict[Rect4, Mark]:
    """Each box drawn once. Twenty items citing the same '(in millions of €)' would stack twenty
    highlights into a black bar, so a shared box keeps the mark that matters most: evidence over
    context, then fail, fixed, blocked, pending, pass."""
    out: dict[Rect4, Mark] = {}
    for mark in marks:
        for r in mark_rects(words, spans, mark):
            key: Rect4 = (round(r[0], 1), round(r[1], 1), round(r[2], 1), round(r[3], 1))
            cur = out.get(key)
            if cur is None or _rank(mark) > _rank(cur):
                out[key] = mark
    return out


def _rank(mark: Mark) -> tuple[int, int]:
    return (mark.role == "evidence", STATUS_RANK.get(mark.status, 1))


def to_png(pg: Any) -> bytes:
    """Render at ZOOM, capped at MAX_WIDTH_PX by MAX_HEIGHT_PX, stepping down until under MAX_PNG_BYTES."""
    import pymupdf

    w, h = max(pg.rect.width, 1.0), max(pg.rect.height, 1.0)
    zoom = min(ZOOM, MAX_WIDTH_PX / w, MAX_HEIGHT_PX / h)
    while True:
        png = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, annots=True).tobytes("png")
        if len(png) <= MAX_PNG_BYTES or zoom <= MIN_ZOOM:
            return png
        zoom = max(MIN_ZOOM, zoom * 0.8)


def draw(pg: Any, rects: list[tuple[float, float, float, float]], mark: Mark) -> None:
    """One highlight annotation per line box. Highlights multiply onto the page, so the text stays sharp."""
    import pymupdf

    color = COLORS.get(mark.status, COLORS["pending"])
    for x0, y0, x1, y1 in rects:
        rect = pymupdf.Rect(x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
        if rect.is_empty:
            continue
        annot = pg.add_highlight_annot(rect)
        if annot is None:
            continue
        annot.set_colors(stroke=color)
        annot.set_opacity(OPACITY.get(mark.role, OPACITY["evidence"]))
        annot.set_info(title="Axelrod", content=f"{mark.item_id} {mark.status}")
        annot.update()


# ---------------------------------------------------------------------------
# Finding the quote (pure functions over PyMuPDF's word tuples)
# ---------------------------------------------------------------------------
# A word is (x0, y0, x1, y1, text, block_no, line_no, word_no).


def word_spans(words: list[tuple], text: str) -> list[tuple[int, int] | None]:
    """Where each word sits in the stored page text: walk both in order, each word searched a short way
    past the last one. A word not found nearby gets None and the walk carries on."""
    spans: list[tuple[int, int] | None] = []
    cursor = 0
    for w in words:
        s = w[4]
        i = text.find(s, cursor, cursor + len(s) + WORD_WINDOW) if s else -1
        if i < 0:
            spans.append(None)
            continue
        spans.append((i, i + len(s)))
        cursor = i + len(s)
    return spans


def mark_rects(words: list[tuple], spans: list[tuple[int, int] | None], mark: Mark) -> list[Rect4]:
    """Boxes for one mark: its offsets if they map onto words, else a search for its quote."""
    if mark.end > mark.start:
        boxes = boxes_for_span(words, spans, mark.start, mark.end)
        if boxes:
            return line_rects(words, boxes)
    hint = mark.start if mark.end > mark.start else None
    return line_rects(words, quote_boxes(words, spans, mark.quote, hint))


def boxes_for_span(words: list[tuple], spans: list[tuple[int, int] | None], start: int,
                   end: int) -> list[tuple[int, Rect4]]:
    """(word index, box) for every word overlapping [start, end). A word cut by the span gets a box
    trimmed in proportion, so '22,363' inside '€22,363m' is boxed tight."""
    out: list[tuple[int, Rect4]] = []
    for i, (w, sp) in enumerate(zip(words, spans)):
        if sp is None or sp[1] <= start or sp[0] >= end:
            continue
        ws, we = sp
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        n = max(we - ws, 1)
        lo, hi = max(start, ws) - ws, min(end, we) - ws
        out.append((i, (x0 + (x1 - x0) * lo / n, y0, x0 + (x1 - x0) * hi / n, y1)))
    return out


def squash(s: str) -> str:
    """Compare form: NFKC, lower case, no whitespace and no soft hyphens."""
    s = unicodedata.normalize("NFKC", s).replace(SOFT_HYPHEN, "").lower()
    return re.sub(r"\s+", "", s)




def quote_boxes(words: list[tuple], spans: list[tuple[int, int] | None], quote: str,
                hint: int | None) -> list[tuple[int, Rect4]]:
    """Search the quote in the page's words with whitespace ignored, then with hyphens ignored (a word
    split at a line end), then line by line (the union of the lines found). Several hits: the one nearest
    the hint offset. The boxes are whole words."""
    q = squash(quote)
    if not q:
        return []
    for dehyphen in (False, True):
        stream, owner = word_stream(words, dehyphen)
        target = q.replace("-", "") if dehyphen else q
        hit = nearest(stream, owner, spans, target, hint)
        if hit is not None:
            return [(i, word_box(words[i])) for i in hit]
    stream, owner = word_stream(words, False)
    found: set[int] = set()
    for piece in (squash(p) for p in re.split(r"[\r\n]+", quote)):
        if len(piece) >= MIN_PIECE_CHARS and piece != q:
            found.update(nearest(stream, owner, spans, piece, hint) or [])
    return [(i, word_box(words[i])) for i in sorted(found)]


def word_stream(words: list[tuple], dehyphen: bool) -> tuple[str, list[int]]:
    """Every word squashed and joined, plus the word index of each character."""
    parts: list[str] = []
    owner: list[int] = []
    for i, w in enumerate(words):
        s = squash(w[4])
        if dehyphen:
            s = s.replace("-", "")
        parts.append(s)
        owner.extend([i] * len(s))
    return "".join(parts), owner


def nearest(stream: str, owner: list[int], spans: list[tuple[int, int] | None], target: str,
            hint: int | None) -> list[int] | None:
    """Word indices of the occurrence of target nearest the hint offset (the first without a hint)."""
    if not target:
        return None
    best: tuple[int, int] | None = None
    best_gap = float("inf")
    i = stream.find(target)
    while i >= 0:
        sp = spans[owner[i]] if owner else None
        gap = 0.0 if hint is None else (abs(sp[0] - hint) if sp is not None else float("inf"))
        if best is None or gap < best_gap:
            best, best_gap = (i, i + len(target)), gap
        if hint is None:
            break
        i = stream.find(target, i + 1)
    if best is None:
        return None
    return sorted(set(owner[best[0]:best[1]]))


def word_box(w: tuple) -> Rect4:
    return (w[0], w[1], w[2], w[3])


def line_rects(words: list[tuple], boxes: list[tuple[int, Rect4]]) -> list[Rect4]:
    """One box per text line (PyMuPDF's block and line numbers): a quote's words on one line merge into
    a single band, and a quote over three lines or table cells gets three."""
    lines: dict[tuple[int, int], list[Rect4]] = {}
    for i, box in boxes:
        lines.setdefault((words[i][5], words[i][6]), []).append(box)
    out = [(min(b[0] for b in g), min(b[1] for b in g), max(b[2] for b in g), max(b[3] for b in g))
           for g in lines.values()]
    return sorted(out, key=lambda r: (r[1], r[0]))
