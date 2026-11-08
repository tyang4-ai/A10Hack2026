"""Exact quote matching: normalization, quote location with page hints, and near-miss diffs.

A quote matches only if it is the source text after one normalization: NFKC, whitespace runs
collapsed to one space, and PDF noise (soft hyphens, control characters) dropped. Nothing else.
Curly quotes, dashes and digits must match as they are, so an
agent cannot "almost" quote a source. When a quote does not match, near_miss finds the closest real
span so the repair stage can re-cite from the source, which always wins.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from array import array
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


_IDENT, _SPACE, _DROP, _MAP, _COMPLEX = range(5)
_KIND: dict[str, tuple[int, str]] = {}


def _kind(ch: str) -> tuple[int, str]:
    """How a lone character normalizes (memoized per character): itself, one collapsing space, nothing,
    a fixed string with no space or dropped char, or something the general loop must handle."""
    k = _KIND.get(ch)
    if k is None:
        nf = unicodedata.normalize("NFKC", ch)
        if unicodedata.combining(ch) or any(unicodedata.combining(c) for c in nf):
            k = (_COMPLEX, nf)
        elif ch.isspace() and len(nf) == 1 and nf.isspace() and not _dropped(nf):
            k = (_SPACE, " ")
        elif nf == ch and not ch.isspace() and not _dropped(ch):
            k = (_IDENT, ch)
        elif all(_dropped(c) for c in nf):
            k = (_DROP, "")
        elif not any(c.isspace() or _dropped(c) for c in nf):
            k = (_MAP, nf)
        else:
            k = (_COMPLEX, nf)
        _KIND[ch] = k
    return k


def _norm_map_fast(s: str) -> tuple[str, array, array] | None:
    """The same result as _norm_map_slow when every character is single-cluster and simple (no combining
    marks anywhere), built from runs instead of per character. None means use the slow loop."""
    kinds = {ch: _kind(ch) for ch in set(s)}
    if any(k[0] == _COMPLEX for k in kinds.values()):
        return None
    special = "".join(ch for ch, k in kinds.items() if k[0] != _IDENT)
    out: list[str] = []
    starts, ends = array("i"), array("i")
    last_space = False  # out[-1] == " "
    pos = 0
    if special:
        rx = re.compile("[" + "".join(f"\\U{ord(c):08x}" for c in special) + "]")
        hits = rx.finditer(s)
    else:
        hits = iter(())
    for mt in hits:
        i = mt.start()
        if i > pos:
            out.append(s[pos:i])
            starts.extend(range(pos, i))
            ends.extend(range(pos + 1, i + 1))
            last_space = False
        kind, nf = kinds[s[i]]
        if kind == _SPACE:
            if last_space:
                ends[-1] = i + 1
            else:
                out.append(" ")
                starts.append(i)
                ends.append(i + 1)
                last_space = True
        elif kind == _MAP:
            out.append(nf)
            starts.extend([i] * len(nf))
            ends.extend([i + 1] * len(nf))
            last_space = False
        pos = i + 1
    if pos < len(s):
        out.append(s[pos:])
        starts.extend(range(pos, len(s)))
        ends.extend(range(pos + 1, len(s) + 1))
    return "".join(out), starts, ends


_LONG = 600  # pages and long spans get their own cache, so a burst of short quotes never evicts a page


def _norm_map(s: str) -> tuple[str, array, array]:
    """Normalized text plus, for each output character, the original [start, end) it came from.
    Cached: every page is normalized once per run instead of once per quote (offsets kept as compact
    int arrays so a whole run's pages fit)."""
    return _norm_map_long(s) if len(s) >= _LONG else _norm_map_short(s)


def _norm_map_uncached(s: str) -> tuple[str, array, array]:
    fast = _norm_map_fast(s)
    if fast is not None:
        return fast
    text_, starts, ends = _norm_map_slow(s)
    return text_, array("i", starts), array("i", ends)


_norm_map_long = lru_cache(maxsize=4096)(_norm_map_uncached)
_norm_map_short = lru_cache(maxsize=16384)(_norm_map_uncached)


def _norm_map_slow(s: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """The reference loop: per character cluster NFKC, drops, whitespace collapse."""
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


_EPS = 1e-12  # far above float rounding of a ratio, far below the 1e-9 tie margin in closest_span


@lru_cache(maxsize=4096)
def _char_counts(npage: str) -> Counter[str]:
    return Counter(npage)


def _shared(cq: Counter[str], counts: Counter[str]) -> int:
    return sum(min(v, counts[c]) for c, v in cq.items() if c in counts)


def _ceiling(shared: int, size: int) -> float:
    """Highest ratio any span can reach when at most `shared` characters can match: ratio = 2M/(|q|+|span|)
    with M <= shared and M <= |span|, so ratio <= 2*shared/(|q|+shared). Used only to skip spans that
    provably cannot win, so the chosen span is the same as without the skip."""
    return 2.0 * shared / (size + shared) if shared else 0.0


def _char_masks(nq: str) -> dict[str, int]:
    masks: dict[str, int] = {}
    for i, c in enumerate(nq):
        masks[c] = masks.get(c, 0) | (1 << i)
    return masks


def _lcs_len(masks: dict[str, int], size: int, b: str) -> int:
    """Length of the longest common subsequence of the quote (as bit masks) and b, bit-parallel
    (Crochemore et al. 2001). SequenceMatcher's matching blocks are a common subsequence, so this bounds
    their total size, which makes _ceiling(lcs) a tighter upper bound than the shared characters."""
    full = (1 << size) - 1
    v = full
    for c in b:
        mc = masks.get(c)
        if mc:
            u = v & mc
            v = ((v + u) | (v - u)) & full
    return size - v.bit_count()


def _best_aligned(npage: str, nq: str, floor: float | None = None) -> tuple[float, int, int] | None:
    """Best candidate span on one page. With floor, a candidate that cannot beat floor + 1e-9 (another
    page's best, see closest_span) is skipped, and None comes back when no candidate can."""
    size = len(nq)
    cq = Counter(nq)
    if floor is not None and _ceiling(_shared(cq, _char_counts(npage)), size) <= floor + 1e-9 - _EPS:
        return None
    pad = max(3, size // 5)
    masks = _char_masks(nq) if floor is not None else {}
    tops: list[tuple[float, int, int]] = []
    for rank, cand in enumerate(_candidate_starts(npage, nq)):
        w0, w1 = max(0, cand - pad), min(len(npage), cand + size + pad)
        window = npage[w0:w1]  # the span _align picks lies inside this window
        top = _ceiling(_shared(cq, Counter(window)), size)
        if floor is not None and top > floor + 1e-9 - _EPS:
            top = min(top, _ceiling(_lcs_len(masks, size, window), size))
        if floor is None or top > floor + 1e-9 - _EPS:
            tops.append((top, rank, cand))
    # Most promising candidate first, so the rest are mostly skipped. The winner is still the highest
    # (ratio, -length gap), the earliest candidate on a tie, exactly as a plain loop in vote order picks it.
    best: tuple[tuple[float, int, int], tuple[float, int, int]] | None = None
    for top, rank, cand in sorted(tops, key=lambda t: (-t[0], t[1])):
        if best is not None and top < best[0][0] - _EPS:
            break
        span = _align(npage, nq, cand)
        if span is None:
            continue
        key = (span[0], -abs(span[2] - span[1] - size), -rank)
        if best is None or key > best[0]:
            best = (key, span)
    return None if best is None else best[1]


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
        span = _best_aligned(npage, nq, None if best is None else best[1]) if npage else None
        if span is not None and (best is None or span[0] > best[1] + 1e-9):
            best = (p, *span)
    if best is None:
        return None
    p, _, s, e = best
    ratio, span, diff = _finish(pages[p - 1], nq, s, e)
    return p, diff, span, ratio
