"""B-speed: the fast quote search and normalization give exactly the results of the plain reference loops.

The reference functions below are the pre-change verify.text code (no caching, no pruning). The export
tests run on the R2 export in backend/.data/r2a_export and are skipped when it is missing.
"""
from __future__ import annotations

import glob
import json
import random
import time
from pathlib import Path

import pytest

from backend.axelrod.verify import text

EXPORT = Path(__file__).resolve().parents[1] / ".data" / "r2a_export"


# ---------------------------------------------------------------------------
# Reference (pre-change) search
# ---------------------------------------------------------------------------


def ref_best_aligned(npage: str, nq: str):
    best = None
    for cand in text._candidate_starts(npage, nq):
        span = text._align(npage, nq, cand)
        if span is not None and (best is None or (span[0], -abs(span[2] - span[1] - len(nq)))
                                 > (best[0], -abs(best[2] - best[1] - len(nq)))):
            best = span
    return best


def ref_closest_span(pages: list[str], quote: str, page_hint: int):
    nq = text.normalize(quote)
    if not nq:
        return None
    best = None
    for p in text._page_order(len(pages), page_hint):
        npage = text._norm_map_slow(pages[p - 1])[0]
        span = ref_best_aligned(npage, nq) if npage else None
        if span is not None and (best is None or span[0] > best[1] + 1e-9):
            best = (p, *span)
    if best is None:
        return None
    p, _, s, e = best
    ratio, span, diff = text._finish(pages[p - 1], nq, s, e)
    return p, diff, span, ratio


def ref_near_miss(page_text: str, quote: str):
    nq = text.normalize(quote)
    npage = text._norm_map_slow(page_text)[0]
    best = ref_best_aligned(npage, nq) if nq and npage else None
    if best is None:
        return ([text.m.DiffSegment(op="delete", agent=nq, source="")] if nq else []), (0, 0)
    _, span, diff = text._finish(page_text, nq, best[1], best[2])
    return diff, span


def same_map(s: str) -> bool:
    a = text._norm_map_slow(s)
    b = text._norm_map(s)
    return a[0] == b[0] and list(a[1]) == list(b[1]) and list(a[2]) == list(b[2])


# ---------------------------------------------------------------------------
# Synthetic
# ---------------------------------------------------------------------------


EDGE = ["", " ", "  a  b  ", "\x07 \x07 x", "ﬁnance­ x", "é cafe", "¨x", "\t\n a　 b ",
        "x  y", " \x07\x07 ", "Revenue\n\n 21,714 – 22,363 €", "­", "ﬃ ﬂ", "à́b  c"]


def test_norm_map_matches_reference_on_edge_cases():
    for s in EDGE:
        assert same_map(s), repr(s)


def test_norm_map_matches_reference_on_random_text():
    rng = random.Random(7)
    alphabet = "ab 1,.\n\t\x07­ ﬁ€–　"
    for _ in range(500):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        assert same_map(s), repr(s)


def test_lcs_bound_matches_dynamic_programming():
    def lcs(a: str, b: str) -> int:
        prev = [0] * (len(b) + 1)
        for x in a:
            cur = [0]
            for j, y in enumerate(b):
                cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
            prev = cur
        return prev[-1]
    rng = random.Random(3)
    for _ in range(300):
        a = "".join(rng.choice("ab c1,") for _ in range(rng.randint(1, 40)))
        b = "".join(rng.choice("ab c1,.") for _ in range(rng.randint(0, 50)))
        assert text._lcs_len(text._char_masks(a), len(a), b) == lcs(a, b)


def test_closest_span_matches_reference_on_synthetic_pages():
    rng = random.Random(11)
    words = ["revenue", "EUR", "million", "2023", "2024", "adjusted", "EBIT", "margin", "21,714", "22,363",
             "(in", "millions)", "fiscal", "year", "net", "debt", "Diagnostics", "Imaging", "%", "–"]
    pages = [" ".join(rng.choice(words) for _ in range(rng.randint(30, 200))) for _ in range(25)]
    for _ in range(60):
        p = rng.randrange(len(pages))
        ws = pages[p].split(" ")
        i = rng.randrange(max(1, len(ws) - 8))
        quote = " ".join(ws[i:i + rng.randint(2, 12)])
        quote = quote.replace("1", "7", 1) if rng.random() < 0.5 else quote + " extra"
        hint = rng.randint(1, len(pages))
        assert text.closest_span(pages, quote, hint) == ref_closest_span(pages, quote, hint)
        assert text.near_miss(pages[p], quote) == ref_near_miss(pages[p], quote)


# ---------------------------------------------------------------------------
# The R2 export (skipped without it)
# ---------------------------------------------------------------------------


def _export():
    if not (EXPORT / "state.json").exists():
        pytest.skip("r2a export not present")
    raw = json.loads((EXPORT / "state.json").read_text(encoding="utf-8"))
    quotes: list[tuple[str, int, str]] = []
    titles = {s["source_id"]: s for s in raw["sources"]}
    for f in sorted(glob.glob(str(EXPORT / "vault" / "00-Meta" / "agents" / "extractor-*.jsonl"))):
        lines = [json.loads(x) for x in Path(f).read_text(encoding="utf-8").splitlines()]
        task = lines[0].get("task", "") if lines else ""
        parts = task.split()
        if len(parts) != 4 or parts[0] != "Extract" or parts[1] not in titles:
            continue
        src = titles[parts[1]]
        docs = [d for d in src["doc_ids"] if d in raw["pages"]]
        if not docs:
            continue
        for x in lines:
            ti = x.get("tool_input") if x.get("kind") == "response" else None
            if isinstance(ti, dict):
                for it in ti.get("items", []):
                    if isinstance(it, dict) and isinstance(it.get("quote"), str):
                        quotes.append((docs[0], int(parts[3]), it["quote"]))
    return raw, quotes


def test_norm_map_matches_reference_on_every_export_page():
    raw, quotes = _export()
    for pages in raw["pages"].values():
        for s in pages:
            assert same_map(s)
    for _, _, q in quotes:
        assert same_map(q)


def test_export_quote_search_matches_reference():
    """Every recorded quote that is not verbatim on its page: near_miss on the page and closest_span over
    the document (quotes up to 120 characters, so the slow reference stays quick) match the reference."""
    raw, quotes = _export()
    checked = 0
    for doc_id, page, q in quotes:
        pages = raw["pages"][doc_id]
        if not 1 <= page <= len(pages) or text.find_quote(pages[page - 1], q) is not None:
            continue
        assert text.near_miss(pages[page - 1], q) == ref_near_miss(pages[page - 1], q)
        if len(text.normalize(q)) <= 120:
            assert text.closest_span(pages, q, page) == ref_closest_span(pages, q, page)
        checked += 1
    assert checked > 0


def test_export_closest_span_is_fast():
    """Timing guard, generous: every not-verbatim recorded quote searched over its whole document. The
    pre-change code took about 45 s for these on a laptop (one 1,100-character quote alone took 43 s)."""
    raw, quotes = _export()
    t0 = time.perf_counter()
    for doc_id, page, q in quotes:
        pages = raw["pages"][doc_id]
        if 1 <= page <= len(pages) and text.find_quote(pages[page - 1], q) is None:
            text.closest_span(pages, q, page)
    assert time.perf_counter() - t0 < 30
