"""Exact quote matching: normalization, quote location with page hints, and near-miss diffs.

A quote matches only if it is the source text after one normalization: NFKC, whitespace runs
collapsed to one space, and PDF noise (soft hyphens, control characters) dropped. Nothing else.
Curly quotes, dashes and digits must match as they are, so an
agent cannot "almost" quote a source. When a quote does not match, near_miss finds the closest real
span so the repair stage can re-cite from the source, which always wins.
"""
from __future__ import annotations

import difflib
import unicodedata
from collections import Counter
from functools import lru_cache

from ..contract import m


def _clusters(s: str):
    """(start, end) of each base character plus its combining marks, so NFKC can run per cluster."""
    i, n = 0, len(s)
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(s[j]):
            j += 1
        yield i, j
        i = j


def _dropped(ch: str) -> bool:
    """PDF noise that carries no text: soft hyphens and control characters that are not whitespace
    (BEL and friends). Whitespace controls (tab, newline, CR, form feed) collapse to a space instead."""
    return ch == "­" or (unicodedata.category(ch) == "Cc" and not ch.isspace())


@lru_cache(maxsize=512)
def _norm_map(s: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """Normalized text plus, for each output character, the original [start, end) it came from."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, j in _clusters(s):
        for ch in unicodedata.normalize("NFKC", s[i:j]):
            if _dropped(ch):
                continue
            if ch.isspace():
                if out and out[-1] == " ":
                    ends[-1] = j  # extend the current whitespace run
                    continue
                ch = " "
            out.append(ch)
            starts.append(i)
            ends.append(j)
    return "".join(out), tuple(starts), tuple(ends)


def normalize(s: str) -> str:
    """NFKC, whitespace runs become one space, soft hyphens and non-whitespace control characters
    (U+0007 BEL in SHL PDFs) are dropped. Nothing else: quotes, dashes and digits stay as they are.

    NFKC runs per character cluster (a base character and its combining marks), which equals plain
    NFKC for ordinary text and lets find_quote map offsets back to the original page.
    """
    return _norm_map(s)[0]


def find_quote(page_text: str, quote: str) -> tuple[int, int] | None:
    """Offsets into the original page text, end exclusive. None when the quote is not on the page."""
    nq = normalize(quote)
    if not nq.strip():
        return None
    npage, starts, ends = _norm_map(page_text)
    i = npage.find(nq)
    while i != -1:
        s, e = starts[i], ends[i + len(nq) - 1]
        if normalize(page_text[s:e]) == nq:  # reject a match that cuts a ligature or cluster in half
            return s, e
        i = npage.find(nq, i + 1)
    return None


def _page_order(n_pages: int, page_hint: int) -> list[int]:
    """1-based pages, the hint first, then outward by distance."""
    return sorted(range(1, n_pages + 1), key=lambda p: (abs(p - page_hint), p))


def locate(pages: list[str], quote: str, page_hint: int) -> tuple[int, int, int] | None:
    """(page, start, end), hint page first. page is 1-based, offsets index the original page text."""
    for p in _page_order(len(pages), page_hint):
        hit = find_quote(pages[p - 1], quote)
        if hit is not None:
            return p, hit[0], hit[1]
    return None


def diff_segments(agent: str, source: str) -> list[m.DiffSegment]:
    """Character diff from the agent's text to the source's text, in contract segments."""
    sm = difflib.SequenceMatcher(None, agent, source, autojunk=False)
    return [m.DiffSegment(op=op, agent=agent[a0:a1], source=source[b0:b1])
            for op, a0, a1, b0, b1 in sm.get_opcodes()]


def changed_chars(diff: list[m.DiffSegment]) -> int:
    """How many characters differ, for a readable "differs by N characters" detail."""
    return sum(max(len(d.agent), len(d.source)) for d in diff if d.op != "equal")


def _candidate_starts(npage: str, nq: str, top: int = 6) -> list[int]:
    """Likely start positions of the quote on the page, voted by shared k-grams."""
    size = len(nq)
    k = min(size, max(2, min(8, size // 3)))
    votes: Counter[int] = Counter()
    for i in range(size - k + 1):
        gram = nq[i:i + k]
        p, hits = npage.find(gram), 0
        while p != -1 and hits < 40:
            votes[p - i] += 1
            hits += 1
            p = npage.find(gram, p + 1)
    if not votes:
        mt = difflib.SequenceMatcher(None, nq, npage, autojunk=False).find_longest_match(0, size, 0, len(npage))
        return [mt.b - mt.a] if mt.size else []
    pad = max(3, size // 5)
    chosen: list[int] = []
    for start, _ in votes.most_common():
        if all(abs(start - c) > pad for c in chosen):
            chosen.append(start)
        if len(chosen) >= top:
            break
    return chosen


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _align(npage: str, nq: str, cand: int) -> tuple[float, int, int] | None:
    """(ratio, start, end) of the span the quote aligns to near a candidate start, in normalized offsets."""
    size = len(nq)
    pad = max(3, size // 5)
    w0, w1 = max(0, cand - pad), min(len(npage), cand + size + pad)
    window = npage[w0:w1]
    blocks = [b for b in difflib.SequenceMatcher(None, nq, window, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return None
    big = [b for b in blocks if b.size >= 2] or blocks  # stray single-character matches skew the edges
    first, last = big[0], big[-1]
    s = w0 + max(0, first.b - first.a)
    e = w0 + min(len(window), last.b + last.size + (size - (last.a + last.size)))
    return (_ratio(nq, npage[s:e]), s, e) if e > s else None


def _nudge(npage: str, nq: str, s: int, e: int) -> tuple[float, int, int]:
    """Move both ends up to 2 characters for the best ratio, preferring the quote's length on ties."""
    best: tuple[tuple[float, int], int, int] | None = None
    for ds in range(-2, 3):
        for de in range(-2, 3):
            ss, ee = s + ds, e + de
            if ss < 0 or ee > len(npage) or ee <= ss:
                continue
            key = (_ratio(nq, npage[ss:ee]), -abs((ee - ss) - len(nq)))
            if best is None or key > best[0]:
                best = (key, ss, ee)
    (ratio, _), ss, ee = best
    return ratio, ss, ee


def _best_aligned(npage: str, nq: str) -> tuple[float, int, int] | None:
    best: tuple[float, int, int] | None = None
    for cand in _candidate_starts(npage, nq):
        span = _align(npage, nq, cand)
        if span is not None and (best is None or (span[0], -abs(span[2] - span[1] - len(nq)))
                                 > (best[0], -abs(best[2] - best[1] - len(nq)))):
            best = span
    return best


def _finish(page_text: str, nq: str, s: int, e: int) -> tuple[float, tuple[int, int], list[m.DiffSegment]]:
    """Nudge the winning span, map it back to original offsets and diff it."""
    npage, starts, ends = _norm_map(page_text)
    ratio, s, e = _nudge(npage, nq, s, e)
    os_, oe = starts[s], ends[e - 1]
    return ratio, (os_, oe), diff_segments(nq, normalize(page_text[os_:oe]))


def near_miss(page_text: str, quote: str) -> tuple[list[m.DiffSegment], tuple[int, int]]:
    """The diff and the closest real span (original offsets, end exclusive)."""
    nq = normalize(quote)
    npage = _norm_map(page_text)[0]
    best = _best_aligned(npage, nq) if nq and npage else None
    if best is None:
        return ([m.DiffSegment(op="delete", agent=nq, source="")] if nq else []), (0, 0)
    _, span, diff = _finish(page_text, nq, best[1], best[2])
    return diff, span


def closest_span(pages: list[str], quote: str,
                 page_hint: int) -> tuple[int, list[m.DiffSegment], tuple[int, int], float] | None:
    """Best near miss over every page: (page, diff, (start, end), similarity). Ties go to the hint.
    None when no page shares any text with the quote."""
    nq = normalize(quote)
    if not nq:
        return None
    best: tuple[int, float, int, int] | None = None
    for p in _page_order(len(pages), page_hint):
        npage = _norm_map(pages[p - 1])[0]
        span = _best_aligned(npage, nq) if npage else None
        if span is not None and (best is None or span[0] > best[1] + 1e-9):
            best = (p, *span)
    if best is None:
        return None
    p, _, s, e = best
    ratio, span, diff = _finish(pages[p - 1], nq, s, e)
    return p, diff, span, ratio
