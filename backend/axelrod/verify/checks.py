"""Code checks for every item kind and brief sentence. Each check function returns list[m.CheckResult].

items is RunState.items (id -> item) and sources is RunState.sources_by_id() (id -> Source).

Every check is a pure function over contract models. Nothing here mutates an item: a fix the checker
finds (a quote found on another page) comes back in CheckResult.closest_locator, and
with_fixed_locators() applies it. Every failure says why in CheckResult.detail, in plain words that
the repair stage and the UI can show as they are.

Checks per kind (in this order):
  data           quote_exact (one per text citation, in citation order), json_value (one per json
                 citation), value_in_quote, unit_context, period_match, numbers_traced (the statement),
                 citations_resolve (the source)
  conclusion     citations_resolve, computation (when it has computations, including the year-ends an
                 average balance uses), numbers_traced
  extrapolation  citations_resolve, threshold_rule (numeric rubric thresholds only), numbers_traced
  chart          citations_resolve, chart_values
  sentence       citations_resolve, then copy_exact (copied) or numbers_traced (derived)
  tier rules     check_tier_rule / check_tier_rules: tier 2 and tier 3 data items, tier-3-only conclusions

What numbers_traced accepts: a cited data item's value (after scale, rounded half up to the shown
decimals), a cited computation's display, the criterion's rubric numbers, the BOFFIN scale (-1, 0, +1)
for scores, rollup numbers for "rollup", chart points for charts, and the numbers in the verified
quote of a cited data item that has no value. A data item's own statement may also use any number in
its evidence quote. A range value ("4.0% and 5.0%") vouches for both bounds. Numbers in words are traced
like digits when they state an amount ("twenty percent", "three billion", "one third"). A count in words
("three years", "five data items") is exempt. Years, fiscal-year labels, dates and ids are never counted
as numbers. A negative
value traces only when the text shows it as negative: a minus, brackets, or a word next to it that
carries the sign ("a loss of", "fell by", "outflow"). A cost or outflow line ("Income tax expenses",
printed −646) may be stated as its size, since the line's name carries the sign.

Quote fidelity: a text citation's quote is the exact characters at its span. quote_exact compares after
normalization, so with_fixed_locators(item, checks, pages_by_doc=...) also writes the exact span text
into every passing quote. A unit or period header counts only on its evidence's page (a release may give
its period on its cover page), so quote_exact looks for a header there and nowhere else.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ..contract import RUBRIC, m
from . import compute, text
from .numbers import (SCALES, WORD_FACTOR, NumToken, half_up, period_key, period_mentions, scan_numbers,
                      to_base)

PASSING = ("pass", "fixed")
_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _cr(check: str, result: str, detail: str, **kw) -> m.CheckResult:
    return m.CheckResult(check=check, method="code", result=result, detail=detail, **kw)


def _dec(x) -> Decimal:
    """Contract floats go through repr, so 5.85 stays 5.85."""
    if isinstance(x, Decimal):
        return x
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def _fmt(x: Decimal) -> str:
    q = x.quantize(Decimal("0.000001")) if abs(x) < 1000 else x.quantize(Decimal(1))
    return format(q.normalize(), "f")


def _short(s: str, n: int = 90) -> str:
    s = text.normalize(s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _score(s: int) -> str:
    return f"{s:+d}" if s else "0"


def base_value(item: m.DataItem) -> Decimal | None:
    """A data item's value in base units, percentages as fractions. This is what expressions read."""
    v = item.value
    if v is None:
        return None
    b = to_base(_dec(v.number), v.scale)
    return b / 100 if v.unit == "%" else b


def status_from(checks: list[m.CheckResult]) -> str:
    """Any fail: fail, any blocked: blocked, else pass. No checks at all: pending, never a pass."""
    if not checks:
        return "pending"
    results = {c.result for c in checks}
    if "fail" in results:
        return "fail"
    if "blocked" in results:
        return "blocked"
    return "pass"


def json_pointer_get(doc, pointer: str):
    """RFC 6901: "" is the whole document, "~1" is "/" and "~0" is "~". Raises KeyError, IndexError, ValueError."""
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise ValueError("a JSON pointer starts with '/'")
    cur = doc
    for raw in pointer[1:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not re.fullmatch(r"0|[1-9]\d*", tok):
                raise ValueError(f"'{tok}' is not an array index")
            if int(tok) >= len(cur):
                raise IndexError(f"index {tok} is past the end of a list of {len(cur)}")
            cur = cur[int(tok)]
        elif isinstance(cur, dict):
            if tok not in cur:
                raise KeyError(f"no key '{tok}'")
            cur = cur[tok]
        else:
            raise ValueError(f"cannot step into a {type(cur).__name__} with '{tok}'")
    return cur


def span_text(pages_by_doc: dict[str, list[str]] | None, doc_id: str, loc: m.TextLocator) -> str | None:
    """The exact stored characters at a text locator, or None when the page or offsets do not exist."""
    pages = (pages_by_doc or {}).get(doc_id) or []
    if not 1 <= loc.page <= len(pages):
        return None
    page = pages[loc.page - 1]
    if not 0 <= loc.char_start < loc.char_end <= len(page):
        return None
    return page[loc.char_start:loc.char_end]


def with_fixed_locators(item: m.DataItem, checks: list[m.CheckResult], *,
                        pages_by_doc: dict[str, list[str]] | None = None) -> m.DataItem:
    """A copy of item with the locators that passing quote_exact checks corrected (quote found on
    another page or at other offsets). quote_exact results come one per text citation, in order.

    With pages_by_doc, every passing quote also becomes the exact characters at its span, so
    quote == page[char_start:char_end] holds (quote_exact compares after normalization, so a quote
    written "Revenue 23,630 23,375" passes on "Revenue \\n23,630 \\n23,375"). A failing quote keeps the
    agent's text: its diff is in the failed check. Without pages_by_doc only the locators move."""
    text_idx = [i for i, c in enumerate(item.citations) if isinstance(c.locator, m.TextLocator)]
    qe = [c for c in checks if c.check == "quote_exact"]
    if len(qe) != len(text_idx):
        return item
    new = item.model_copy(deep=True)
    for i, c in zip(text_idx, qe):
        if c.result != "pass":
            continue
        cit = new.citations[i]
        update: dict = {}
        if c.closest_locator is not None:
            update["locator"] = c.closest_locator
        real = span_text(pages_by_doc, cit.doc_id, update.get("locator", cit.locator)) if pages_by_doc else None
        if real is not None and real != cit.quote and text.normalize(real) == text.normalize(cit.quote):
            update["quote"] = real
        if update:
            new.citations[i] = cit.model_copy(update=update)
    return new


_RELEASE = re.compile(r"release|quarterly|interim|half[- ]year|results", re.IGNORECASE)


def is_release(src: m.Source | None) -> bool:
    """A results release (company IR, quarterly statement) names its period once, on its cover page, so a
    period header on page 1 may label a value on a later page. An annual report heads every table itself."""
    return src is not None and (src.kind == "company_ir" or bool(_RELEASE.search(src.title)))


def context_pages(cit: m.Citation, evidence_pages: dict[str, list[int]] | None,
                  src: m.Source | None) -> list[int] | None:
    """The pages a unit or period header may sit on: its evidence's pages in the same document, plus page 1
    for a release's period. None for an evidence quote, or an item with no text evidence (no restriction)."""
    if cit.role == "evidence" or not evidence_pages:
        return None
    pages = list(evidence_pages.get(cit.doc_id, []))
    if cit.role == "period_context" and is_release(src) and 1 not in pages and pages:
        pages.append(1)
    return pages


def _source_issue(src: m.Source | None) -> str | None:
    if src is None:
        return None
    if src.status == "revoked":
        return f"source revoked: {src.source_id}"
    if src.status == "quarantined":
        return f"source quarantined: {src.source_id}"
    if src.approval == "rejected":
        return f"source rejected: {src.source_id}"
    return None


def _issue_behind(it, items: dict, sources: dict | None, seen: set[str] | None = None) -> str | None:
    """First revoked, quarantined or rejected source behind an item, walking E -> C -> D and G -> refs."""
    if not sources:
        return None
    seen = set() if seen is None else seen
    if it.id in seen:
        return None
    seen.add(it.id)
    if it.kind == "data":
        return _source_issue(sources.get(it.source_id))
    refs = it.data_ids if it.kind == "conclusion" else it.conclusion_ids if it.kind == "extrapolation" else it.refs
    for r in refs:
        sub = items.get(r.split(":")[0])
        if sub is not None:
            issue = _issue_behind(sub, items, sources, seen)
            if issue:
                return issue
    return None


def _resolve(refs: list[str], items: dict, sources: dict | None, *, kinds: tuple[str, ...],
             holes: list[m.Hole] = (), allow_rollup: bool = False) -> m.CheckResult:
    """citations_resolve: every id exists, has the right kind, passed, and its sources still stand."""
    if not refs:
        return _cr("citations_resolve", "fail", "Cites nothing")
    hole_items = {h.hole_id: h.item_id for h in holes}
    about_gap = {hole_items[r] for r in refs if r in hole_items}
    problems: list[str] = []
    lost: list[str] = []
    waiting: list[str] = []
    for r in refs:
        if (allow_rollup and r == "rollup") or r in hole_items:
            continue
        if r.startswith("HOLE-"):
            problems.append(f"{r} is not a declared hole")
            continue
        base, _, name = r.partition(":")
        it = items.get(base)
        if it is None:
            problems.append(f"{r} does not exist")
            continue
        if it.kind not in kinds:
            problems.append(f"{r} is a {it.kind} item, not a {' or '.join(kinds)} item")
            continue
        if name and (it.kind != "conclusion" or name not in {c.name for c in it.computations}):
            problems.append(f"{r} names no computation of {base}")
            continue
        issue = _issue_behind(it, items, sources)
        if issue:
            lost.append(f"{r}: {issue}")
            continue
        st = it.verification.status
        if st in PASSING or (st == "fail" and base in about_gap):  # a sentence about a declared gap
            continue
        (problems if st == "fail" else waiting).append(f"{r} is {st}")
    if problems:
        return _cr("citations_resolve", "fail", "; ".join(problems))
    if lost:
        return _cr("citations_resolve", "blocked", "; ".join(lost))
    if waiting:
        return _cr("citations_resolve", "blocked", "Not verified yet: " + "; ".join(waiting))
    return _cr("citations_resolve", "pass", f"All {len(refs)} cited id(s) exist and passed")


# ---------------------------------------------------------------------------
# numbers_traced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cand:
    """A number a text may use: as shown or reported (number) and in base units when known (base).
    sign_free: a negative candidate that may still be shown without a minus, because its source writes a
    spaced dash ("– 658", a minus or a range dash) or it is a cost or outflow line (see _cost_line)."""
    number: Decimal
    base: Decimal | None
    pct: bool
    label: str
    sign_free: bool = False


_SCORE_CANDS = [_Cand(Decimal(n), None, False, "BOFFIN scale") for n in (-1, 0, 1)]
_SPACED_DASH = re.compile(r"\s*[-−‒–]\s")
# A cost or outflow line. Statements print these negative and prose states their size: "Income tax
# expenses −646" is "income tax expenses of €646 million". Income before taxes is not a cost line.
_COST_LINE = re.compile(r"\b(?:expenses?|expenditures?|costs?|paid|payments?|repayments?|additions|purchases?|"
                        r"acquisitions?|dividends|amorti[sz]ation|depreciation|impairments?|charges?|outflows?|"
                        r"write-?downs?|tax(?:es)?)\b", re.IGNORECASE)
_PRE_TAX = re.compile(r"\bbefore\s+(?:income\s+)?tax(?:es)?\b|\bpre-?tax\b", re.IGNORECASE)


def _cost_line(statement: str) -> bool:
    """The item's own statement names a cost or outflow line, so its sign is the line's, not the text's."""
    return bool(_COST_LINE.search(_PRE_TAX.sub(" ", statement or "")))


# Numbers written as words. A count ("three years", "five data items", "two of three judges") is exempt, like a
# year, so code's gap summary ("thirteen gaps") has nothing to trace. An amount is traced like digits: a number
# next to a scale word ("three billion"), a percent ("twenty percent", "two percentage points"), a currency
# ("five euros", "€ twenty") or a fraction ("one third", "two-thirds", "a quarter of", "half of").
_W_NUMS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                      "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_W_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_W_FRACS = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fifth": 5, "fifths": 5,
            "sixth": 6, "sixths": 6, "seventh": 7, "sevenths": 7, "eighth": 8, "eighths": 8, "ninth": 9,
            "ninths": 9, "tenth": 10, "tenths": 10}


def _w_alt(words) -> str:
    return "(?:" + "|".join(sorted(words, key=len, reverse=True)) + r")\b"


_W_HY = r"(?:[-‐‑]|\s+)"
_W_99 = rf"(?:{_w_alt(_W_TENS)}(?:{_W_HY}{_w_alt([w for w, n in _W_NUMS.items() if 1 <= n <= 9])})?|{_w_alt(_W_NUMS)})"
_W_999 = rf"(?:{_W_99}\s+hundred\b(?:\s+(?:and\s+)?{_W_99})?|{_W_99})"
_W_CARD = (rf"(?:{_W_999}(?:\s+point(?:\s+{_w_alt([w for w, n in _W_NUMS.items() if n <= 9])})+)?"
           r"|(?:half\s+)?a(?=\s+(?:thousand|million|billion|trillion)\b))")
_W_AMOUNT = re.compile(
    r"(?<![\w.,‐‑-])"
    r"(?:(?P<cur1>[€$£¥]|(?:EUR|USD|GBP|CHF|JPY)\b)\s?)?"
    rf"(?P<card>{_W_CARD})"
    r"(?:\s+(?P<word>thousand|million|billion|trillion)s?\b)?"
    r"(?:(?P<pct>\s?%|\s+(?:percent|per\s+cent)\b)|\s+(?P<pts>(?:percentage|basis)\s+points?)\b)?"
    r"(?:\s+(?P<cur3>euros?|dollars?|pounds?|francs?|yen|cents?)\b)?",
    re.IGNORECASE)
_W_FRACTION = re.compile(
    rf"(?<![\w.,‐‑-])(?:(?P<n>{_w_alt(['a', 'an', *[w for w, n in _W_NUMS.items() if 1 <= n <= 10]])})"
    rf"(?:\s+|[-‐‑]))?(?P<d>{_w_alt(_W_FRACS)})(?P<of>\s+of\b)?",
    re.IGNORECASE)
_W_ORDINAL_BEFORE = re.compile(r"\b(?:first|second|last|next|1st|2nd)\s*$", re.IGNORECASE)
_W_PERIOD_AFTER = re.compile(r"\s+(?:the\s+)?(?:fiscal|financial|FY|year|(?:19|20)\d{2})", re.IGNORECASE)
_W_CUR = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY", "euro": "EUR", "dollar": "USD", "pound": "GBP",
          "franc": "CHF", "yen": "JPY", "cent": None}


@dataclass(frozen=True)
class _Said:
    """A number as a text says it. Digits and word amounts have one reading. A fraction in words has two, a
    percent and a ratio ("one third" shows as 33% or 0.33), rounded in reads and exact in exact."""
    tok: NumToken                    # what a failure names (raw text, span)
    reads: tuple[NumToken, ...]      # what a candidate must show at the text's precision
    exact: tuple[NumToken, ...]      # the same, unrounded, for when the text vouches for numbers (a quote)


def _card_value(s: str) -> tuple[Decimal, int]:
    """'twenty-five' (25, 0), 'one point two' (1.2, 1), 'three hundred and five' (305, 0), 'a' (1, 0),
    'half a' (0.5, 1)."""
    words = [w for w in re.split(r"[\s‐‑-]+", s.lower()) if w and w != "and"]
    if words == ["a"]:
        return Decimal(1), 0
    if words == ["half", "a"]:
        return Decimal("0.5"), 1
    if "point" in words:
        i = words.index("point")
        whole, _ = _card_value(" ".join(words[:i]))
        digits = "".join(str(_W_NUMS[w]) for w in words[i + 1:])
        return Decimal(f"{whole}.{digits}"), len(digits)
    n = 0
    for w in words:
        n = n * 100 if w == "hundred" else n + _W_TENS.get(w, _W_NUMS.get(w, 0))
    return Decimal(n), 0


def _word_tok(s: str, start: int, end: int, core: tuple[int, int], number: Decimal, places: int, *, pct: bool,
              word: str | None = None, currency: str | None = None) -> NumToken:
    return NumToken(raw=s[start:end], start=start, end=end, core=s[core[0]:core[1]], core_start=core[0],
                    core_end=core[1], number=number, places=places, pct=pct, word=word, currency=currency)


def _word_amounts(s: str) -> list[_Said]:
    """Cardinals in words that state an amount. A bare cardinal is a count and is left out."""
    out: list[_Said] = []
    for mm in _W_AMOUNT.finditer(s):
        if not (mm.group("word") or mm.group("pct") or mm.group("pts") or mm.group("cur1") or mm.group("cur3")):
            continue
        number, places = _card_value(mm.group("card"))
        cur = (mm.group("cur1") or "").strip().upper() or _W_CUR.get((mm.group("cur3") or "").lower().rstrip("s"))
        t = _word_tok(s, mm.start(), mm.end(), mm.span("card"), number, places, pct=bool(mm.group("pct")),
                      word=mm.group("word").lower() if mm.group("word") else None,
                      currency=_W_CUR.get(cur, cur) if cur else None)
        out.append(_Said(t, (t,), (t,)))
    return out


def _fraction_ok(s: str, mm: re.Match) -> bool:
    """A fraction, not an ordinal or a period: "one third", "two thirds", "a third of", "half of", "three quarters
    of revenue". Not "a third year", "the third quarter", "the first half of fiscal 2025", "three quarters"."""
    n, d, of = (mm.group("n") or "").lower(), mm.group("d").lower(), bool(mm.group("of"))
    num = 1 if n in ("", "a", "an") else _W_NUMS[n]
    if not n and (d != "half" or not of or _W_ORDINAL_BEFORE.search(s[:mm.start()])):
        return False
    if n in ("a", "an") and not of:
        return False
    if d.startswith("quarter") and (not of or _W_PERIOD_AFTER.match(s, mm.end())):
        return False
    return num < _W_FRACS[d] and (num == 1) == (not d.endswith("s"))


def _fractions(s: str) -> list[_Said]:
    out: list[_Said] = []
    for mm in _W_FRACTION.finditer(s):
        if not _fraction_ok(s, mm):
            continue
        num = 1 if (mm.group("n") or "").lower() in ("", "a", "an") else _W_NUMS[mm.group("n").lower()]
        f = Decimal(num) / Decimal(_W_FRACS[mm.group("d").lower()])
        end = mm.start("of") if mm.group("of") else mm.end()
        span = (mm.start(), end)

        def tok(x: Decimal, places: int, pct: bool) -> NumToken:
            return _word_tok(s, span[0], span[1], span, x, places, pct=pct)

        reads = (tok(Decimal(half_up(f * 100, 0)), 0, True), tok(Decimal(half_up(f, 2)), 2, False))
        out.append(_Said(reads[0], reads, (tok(f * 100, 0, True), tok(f, 2, False))))
    return out


def _said_in(norm: str) -> list[_Said]:
    """Every number a normalized text states, digits and words, in reading order. Years, labels and counts
    are left out."""
    said = [_Said(t, (t,), (t,)) for t in scan_numbers(norm)]
    for w in _word_amounts(norm) + _fractions(norm):
        if not any(w.tok.start < x.tok.end and x.tok.start < w.tok.end for x in said):
            said.append(w)
    return sorted(said, key=lambda x: x.tok.start)


def _text_cands(label: str, s: str | None, *, cost: bool = False) -> list[_Cand]:
    if not s:
        return []
    return [_Cand(t.number, t.value if t.word else None, t.pct, label, t.sign_ambiguous or cost)
            for x in _said_in(text.normalize(s)) for t in x.exact]


# A range as printed ("4.0% and 5.0%", "4% to 5%", "4–5%"). value.number is its first bound, and value_in_quote has
# found the whole range in the quote, so the other bound is the source's number too.
_RANGE_JOIN = re.compile(r"\s*(?:and|to|[-−‒–—])\s*", re.IGNORECASE)
_DASHES = "-−‒–—"


def _range_cands(iid: str, v: m.Value, *, cost: bool = False) -> list[_Cand]:
    """The other bound of a range value, or nothing when as_reported is not exactly two numbers joined."""
    a = text.normalize(v.as_reported or "").strip()
    a = re.sub(r"^(?:between|from)\s+", "", a, flags=re.IGNORECASE)
    toks = scan_numbers(a, include_labels=True)
    if len(toks) != 2 or a[toks[1].end:].strip():
        return []
    lo, hi = toks
    join = a[lo.end:hi.start]
    if not join.strip() and a[hi.start] in _DASHES:
        n = abs(hi.number)          # "4–5%": the scanner read the joining dash as hi's minus
    elif _RANGE_JOIN.fullmatch(join):
        n = hi.number               # "4.0% and 5.0%", "−4% to −2%": hi's sign as printed
    else:
        return []
    pct = v.unit == "%"
    return [_Cand(n, None if pct else to_base(n, v.scale), pct, iid, cost)]


def _value_cands(iid: str, v: m.Value, *, cost: bool = False) -> list[_Cand]:
    n, pct = _dec(v.number), v.unit == "%"
    spaced = bool(_SPACED_DASH.match(text.normalize(v.as_reported or "")))
    return [_Cand(n, None if pct else to_base(n, v.scale), pct, iid, spaced or cost)] + _range_cands(iid, v, cost=cost)


def _data_cands(it: m.DataItem, *, quotes: bool) -> list[_Cand]:
    cost = _cost_line(it.statement)
    out = _value_cands(it.id, it.value, cost=cost) if it.value is not None else []
    if quotes or it.value is None:
        for cit in it.citations:
            if cit.role == "evidence" and isinstance(cit.locator, m.TextLocator):
                out += _text_cands(f"{it.id} quote", cit.quote, cost=cost)
    return out


def _threshold_cands(criteria: list[str], rubric: dict) -> list[_Cand]:
    out: list[_Cand] = []
    for k in criteria:
        crit = rubric.get("criteria", {}).get(k) or {}
        for field in ("label", "plus_one", "minus_one", "zero"):
            out += _text_cands(f"{k} rubric", crit.get(field))
    return out


def _chart_cands(g: m.Chart) -> list[_Cand]:
    out = []
    for s in g.series:
        for p in s.points:
            y = _dec(p.y)
            out.append(_Cand(y, None, True, g.id) if g.unit == "%" else _Cand(y, y * SCALES[g.scale], False, g.id))
    return out


def _rollup_cands(r: m.Rollup) -> list[_Cand]:
    out: list[_Cand] = []

    def add(x, pct: bool = False) -> None:
        if x is not None:
            out.append(_Cand(_dec(x), None, pct, "rollup"))

    add(r.weighted_average)
    for cs in r.categories:
        for x in (cs.score, cs.weight, cs.scored, cs.in_scope):
            add(x)
    for x in (r.normalized_pct, r.percent_of_max_pct):
        add(x, True)
        add(x)
    for x in (r.scored, r.in_scope, r.framework_total, len(r.na_criteria)):
        add(x)
    return out


def _pool(refs: list[str], items: dict, *, rubric: dict, rollup: m.Rollup | None = None,
          seen: set[str] | None = None) -> list[_Cand]:
    """Every number the cited refs vouch for, walking E -> C -> D and G -> refs."""
    seen = set() if seen is None else seen
    out: list[_Cand] = []
    for r in refs:
        if r == "rollup":
            out += _rollup_cands(rollup) if rollup is not None else []
            continue
        base = r.split(":")[0]
        if base in seen or base not in items:
            continue
        seen.add(base)
        it = items[base]
        if it.kind == "data":
            out += _data_cands(it, quotes=False)
        elif it.kind == "conclusion":
            out += [c for comp in it.computations for c in _text_cands(f"{it.id}:{comp.name}", comp.display)]
            out += _threshold_cands(it.criteria, rubric)
            out += _pool(it.data_ids, items, rubric=rubric, seen=seen)
        elif it.kind == "extrapolation":
            out += _threshold_cands([it.criterion], rubric) + _SCORE_CANDS
            out += _pool(it.conclusion_ids, items, rubric=rubric, seen=seen)
        elif it.kind == "chart":
            out += _chart_cands(it)
            out += _pool(it.refs, items, rubric=rubric, seen=seen)
    return out


def _same_size(t: NumToken, c: _Cand) -> bool:
    """c shows as t's digits at t's precision, sign aside."""
    if c.pct != t.pct:
        return False
    if t.word:
        if c.base is None:
            return False
        x = abs(c.base) / WORD_FACTOR[t.word]
    else:
        x = abs(c.number)
    return Decimal(half_up(x, t.places)) == abs(t.number)


def _negative(t: NumToken, c: _Cand) -> bool:
    """c is a negative amount that must be shown as negative (a definite minus, not a cost line)."""
    v = c.base if t.word else c.number
    return v is not None and v < 0 and not c.sign_free


def _traced(t: NumToken, cands: list[_Cand], negative_shown: bool = True) -> str | None:
    """The label of the candidate that shows this number at its precision, or None. A negative candidate
    traces only when the text shows the number as negative (negative_shown)."""
    for c in cands:
        if _same_size(t, c) and (negative_shown or not _negative(t, c)):
            return c.label
    return None


# Words that carry a negative amount's sign in prose, so the number itself may be written without a minus:
# losses, outflows and costs ("a loss of €1,710 million", "an outflow of €1.7 billion") and moves down ("a
# decline of 4.2%", "fell by 4.2%"). A move down to a level ("fell to €1,710 million") names a level, not a
# negative amount.
_NEG_WORDS = re.compile(r"\b(?:loss(?:es)?|outflows?|deficits?|shortfalls?|negative|minus|expenses?|expenditures?|"
                        r"costs?|paid|payments?|repaid|repayments?|spent|spending|charges?|impairments?|"
                        r"write-?downs?|additions|purchases?|acquisitions?)\b", re.IGNORECASE)
_DOWN_WORDS = re.compile(r"\b(?:declin(?:e|ed|es|ing)|decreas(?:e|ed|es|ing)|fell|fall(?:s|ing)?|drop(?:s|ped)?|"
                         r"down|lower|reduc(?:ed|es|ing|tion)|shr[au]nk|contract(?:ed|ion))\b", re.IGNORECASE)
_TO_LEVEL = re.compile(r"\bto\s*$", re.IGNORECASE)
_NEG_AFTER = re.compile(r"\s*(?:[\w-]+\s+)?(?:outflows?|loss(?:es)?|deficit|shortfall|lower|less|declines?|"
                        r"decreases?|drop)\b", re.IGNORECASE)
# Where the words in front of a number stop belonging to it: a comma, colon or semicolon, or a sentence end
# ("approx. €5" is no sentence end).
_CLAUSE_STOP = re.compile(r"[,;:]|[.!?](?=\s+[A-Z\"“(]|\s*$)")
NEG_WORD_WINDOW = 5  # words right before the number that may carry its sign ("a negative free cash flow of")


def _shows_negative(s: str, t: NumToken, prev_end: int) -> bool:
    """The text shows t as negative: a minus or brackets, or a sign-carrying word right next to it (in the
    few words before it, since the previous number and the last clause break, or the word after it)."""
    if t.number < 0:
        return True
    lo = prev_end
    for mm in _CLAUSE_STOP.finditer(s, prev_end):  # the whole text, so "$" means its real end
        if mm.start() >= t.start:
            break
        lo = mm.end()
    before = " ".join(s[lo:t.start].split()[-NEG_WORD_WINDOW:])
    if _NEG_WORDS.search(before) or (_DOWN_WORDS.search(before) and not _TO_LEVEL.search(before)):
        return True
    return bool(_NEG_AFTER.match(s, t.end))


def _signed(c: _Cand) -> str:
    n = format(c.number.normalize(), ",f")
    return n + ("%" if c.pct else "")


def _sign_detail(t: NumToken, cands: list[_Cand]) -> str | None:
    """'1,710 is shown without its minus sign: D-123 is -1,710', when t matches a negative candidate by size."""
    c = next((c for c in cands if _same_size(t, c) and _negative(t, c)), None)
    if c is None:
        return None
    who = f"{c.label[:-len(' quote')]}'s quote shows" if c.label.endswith(" quote") else f"{c.label} is"
    return f"'{t.raw}' is shown without its minus sign: {who} {_signed(c)}"


def _numbers_traced(s: str, cands: list[_Cand], *, what: str, blocked_reason: str | None = None) -> m.CheckResult:
    """Every number the text states (digits, or an amount in words) must show a candidate at its precision."""
    norm = text.normalize(s)
    said = _said_in(norm)
    if not said:
        return _cr("numbers_traced", "pass", "No numbers to trace")
    untraced: list[NumToken] = []
    signs: list[str] = []
    other: list[str] = []
    prev = 0
    for x in said:
        t = x.tok
        neg = _shows_negative(norm, t, prev)
        prev = t.end
        if all(_traced(r, cands, neg) is None for r in x.reads):
            untraced.append(t)
            why = None if neg else next((d for r in x.reads if (d := _sign_detail(r, cands))), None)
            (signs if why else other).append(why or f"'{t.raw}'")
    if not untraced:
        return _cr("numbers_traced", "pass",
                   f"Every number in the {what} traces to a cited value or computation ({len(said)} checked)")
    if blocked_reason:
        return _cr("numbers_traced", "blocked", blocked_reason)
    parts = []
    if other:
        verb = "does" if len(other) == 1 else "do"
        parts.append(f"{_and(other)} {verb} not match any cited value, computation display or rubric threshold")
    parts += signs
    return _cr("numbers_traced", "fail", "; ".join(parts), unsupported_spans=[t.raw for t in untraced])


# ---------------------------------------------------------------------------
# Data items
# ---------------------------------------------------------------------------


def _quote_exact_on(cit: m.Citation, pages: list[str], allowed: list[int]) -> m.CheckResult:
    """quote_exact for a unit or period header, which counts only on the allowed pages (its evidence's page,
    and a release's cover page for a period). A header found only elsewhere fails and says where it is."""
    loc = cit.locator
    role = cit.role.replace("_", " ")
    allowed = [p for p in dict.fromkeys(allowed) if 1 <= p <= len(pages)]
    if not allowed:
        return _cr("quote_exact", "fail", f"{role.capitalize()} quote has no evidence quote in {cit.doc_id} to label")
    order = sorted(allowed, key=lambda p: (p != loc.page, abs(p - loc.page), p))
    where = "the evidence's " + (f"page {order[0]}" if len(order) == 1 else "pages " + _and([str(p) for p in order]))
    for p in order:
        hit = text.find_quote(pages[p - 1], cit.quote)
        if hit is None:
            continue
        s, e = hit
        if p == loc.page:
            detail = (f"Warning: {role} quote found verbatim on {cit.doc_id} page {p} at chars {s}-{e}, "
                      f"not at the cited chars {loc.char_start}-{loc.char_end}. Offsets corrected.")
        else:
            detail = (f"Warning: {role} quote found verbatim on {cit.doc_id} page {p}, not the cited page "
                      f"{loc.page}. Page corrected to {p}.")
        return _cr("quote_exact", "pass", detail, closest_locator=m.TextLocator(page=p, char_start=s, char_end=e))
    elsewhere = text.locate(pages, cit.quote, loc.page)
    sub = [pages[p - 1] for p in order]
    best = text.closest_span(sub, cit.quote, 1)
    if elsewhere is not None:
        head = (f"{role.capitalize()} quote is on {cit.doc_id} page {elsewhere[0]}, not on {where}. A header "
                f"counts only on the page of the value it labels")
    else:
        head = f"{role.capitalize()} quote not found on {where} in {cit.doc_id}"
    if best is None or best[3] == 0:
        return _cr("quote_exact", "fail", head + ", and no similar text exists there")
    k, diff, (s, e), _ = best
    p = order[k - 1]
    return _cr("quote_exact", "fail",
               f"{head}. The closest span there is on page {p} (chars {s}-{e}) and differs by "
               f"{text.changed_chars(diff)} character(s): '{_short(pages[p - 1][s:e])}'",
               diff=diff, closest_locator=m.TextLocator(page=p, char_start=s, char_end=e))


def _quote_exact(cit: m.Citation, pages_by_doc: dict[str, list[str]],
                 allowed: list[int] | None = None) -> m.CheckResult:
    """One quote against the stored pages. allowed (a header's pages, see context_pages) limits where a
    unit or period header may be found. None searches the whole document, hint page first."""
    pages = pages_by_doc.get(cit.doc_id)
    loc = cit.locator
    if not pages:
        return _cr("quote_exact", "blocked",
                   f"No stored text for {cit.doc_id} (missing or quarantined), so the quote could not be checked")
    nq = text.normalize(cit.quote)
    if not nq.strip():
        return _cr("quote_exact", "fail", "The quote is empty")
    role = cit.role.replace("_", " ")
    if 1 <= loc.page <= len(pages) and (allowed is None or loc.page in allowed):
        page = pages[loc.page - 1]
        if 0 <= loc.char_start < loc.char_end <= len(page) and text.normalize(page[loc.char_start:loc.char_end]) == nq:
            return _cr("quote_exact", "pass",
                       f"{role.capitalize()} quote found verbatim at {cit.doc_id} page {loc.page}, "
                       f"chars {loc.char_start}-{loc.char_end}")
    if allowed is not None:
        return _quote_exact_on(cit, pages, allowed)
    hit = text.locate(pages, cit.quote, loc.page)
    if hit is not None:
        p, s, e = hit
        fixed = m.TextLocator(page=p, char_start=s, char_end=e)
        if p == loc.page:
            detail = (f"Warning: {role} quote found verbatim on {cit.doc_id} page {p} at chars {s}-{e}, "
                      f"not at the cited chars {loc.char_start}-{loc.char_end}. Offsets corrected.")
        else:
            detail = (f"Warning: {role} quote found verbatim on {cit.doc_id} page {p}, not the cited page "
                      f"{loc.page}. Page corrected to {p}.")
        return _cr("quote_exact", "pass", detail, closest_locator=fixed)
    best = text.closest_span(pages, cit.quote, loc.page)
    if best is None or best[3] == 0:
        return _cr("quote_exact", "fail", f"Quote not found in {cit.doc_id}, and no similar text exists there")
    p, diff, (s, e), _ = best
    real = pages[p - 1][s:e]
    return _cr("quote_exact", "fail",
               f"Quote not found in {cit.doc_id}. The closest span is on page {p} (chars {s}-{e}) and differs by "
               f"{text.changed_chars(diff)} character(s): '{_short(real)}'",
               diff=diff, closest_locator=m.TextLocator(page=p, char_start=s, char_end=e))


def _json_number(v) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _dec(v)
    if isinstance(v, str):
        try:
            return Decimal(v.strip())
        except ArithmeticError:
            return None
    return None


def _json_value(item: m.DataItem, cit: m.Citation, pages_by_doc: dict[str, list[str]]):
    """(json_value result, parsed document or None)."""
    pages = pages_by_doc.get(cit.doc_id)
    ptr = cit.locator.pointer
    if not pages:
        return _cr("json_value", "blocked", f"No stored JSON for {cit.doc_id} (missing or quarantined)"), None
    try:
        doc = json.loads("".join(pages))
    except ValueError:
        return _cr("json_value", "blocked", f"{cit.doc_id} is not valid JSON, so the pointer could not be read"), None
    try:
        val = json_pointer_get(doc, ptr)
    except (KeyError, IndexError, ValueError) as e:
        return _cr("json_value", "fail", f"Pointer {ptr} does not resolve in {cit.doc_id}: {e}"), None
    num, qnum = _json_number(val), _json_number(cit.quote)
    quote_ok = str(val) == cit.quote or (num is not None and qnum is not None and num == qnum)
    if item.value is not None:
        want = _dec(item.value.number)
        if num is None:
            return _cr("json_value", "fail", f"{ptr} holds {val!r}, not a number"), doc
        if num != want:
            return _cr("json_value", "fail", f"{ptr} = {val}, but the value says {_fmt(want)}"), doc
    if not quote_ok:
        return _cr("json_value", "fail", f"The quote '{_short(cit.quote)}' differs from the value at {ptr} ({val})"), doc
    return _cr("json_value", "pass", f"{ptr} = {val}"), doc


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(T00:00:00)?")


def _end_date(s: str) -> date | None:
    """Period end from "2025-09-30", an instant, or an xBRL-JSON "start/end" (a midnight end is exclusive)."""
    mm = _ISO.match(s.split("/")[-1].strip())
    if not mm:
        return None
    try:
        d = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
    except ValueError:
        return None
    return d - timedelta(days=1) if mm.group(4) else d


def _json_dates(obj) -> list[date]:
    if not isinstance(obj, dict):
        return []
    found: list[date] = []
    for key in ("end", "instant", "endDate", "period", "date"):
        v = obj.get(key)
        if isinstance(v, str):
            d = _end_date(v)
            if d:
                found.append(d)
        elif isinstance(v, dict):
            found += _json_dates(v)
    found += _json_dates(obj.get("dimensions"))
    return found


def _json_period(v: m.Value, doc, pointer: str) -> m.CheckResult | None:
    if not v.period:
        return None
    years, _ = period_key(v.period)
    if not years:
        return _cr("period_match", "blocked", f"Period '{v.period}' names no year, so it could not be checked")
    try:
        parent = json_pointer_get(doc, pointer.rsplit("/", 1)[0])
    except (KeyError, IndexError, ValueError):
        parent = None
    dates = _json_dates(parent)
    if dates:
        d = dates[0]
        if d.year in years:
            return _cr("period_match", "pass", f"The fact's period ends {d.isoformat()}, which matches {v.period}")
        return _cr("period_match", "fail", f"The fact's period ends {d.isoformat()}, which does not match {v.period}")
    fy = parent.get("fy") if isinstance(parent, dict) else None
    if isinstance(fy, int):
        result = "pass" if fy in years else "fail"
        return _cr("period_match", result, f"The fact is labeled fy={fy}, the value says {v.period}")
    return _cr("period_match", "blocked", f"No period field sits next to {pointer}, so the period could not be checked")


def _value_hits(q: str, v: m.Value) -> list[NumToken]:
    """Tokens in a normalized quote that show as_reported (same digits, the whole number)."""
    a = text.normalize(v.as_reported).strip()
    a_toks = scan_numbers(a, include_labels=True)
    if not a_toks:
        return []
    at = a_toks[0]
    return [t for t in scan_numbers(q, include_labels=True)
            if t.core == at.core and t.core_start >= at.core_start and q.startswith(a, t.core_start - at.core_start)]


def _value_in_quote(v: m.Value, ev: list[str]) -> m.CheckResult:
    a = text.normalize(v.as_reported).strip()
    a_toks = scan_numbers(a, include_labels=True)
    if not a_toks:
        return _cr("value_in_quote", "fail", f"as_reported '{v.as_reported}' holds no number")
    want = _dec(v.number)
    if abs(a_toks[0].number) != abs(want):
        return _cr("value_in_quote", "fail",
                   f"as_reported '{v.as_reported}' reads as {_fmt(a_toks[0].number)}, but value.number is {_fmt(want)}")
    problems: list[str] = []
    for q in ev:
        for t in _value_hits(q, v):
            if t.pct and v.unit != "%":
                problems.append(f"The quote shows '{t.raw}', a percentage, but the unit is {v.unit}")
            elif t.number == want:
                return _cr("value_in_quote", "pass", f"'{v.as_reported}' appears in the quote and reads as {_fmt(want)}")
            elif t.sign_ambiguous and abs(t.number) == abs(want):
                return _cr("value_in_quote", "pass",
                           f"'{v.as_reported}' appears in the quote. The spaced dash before it can be a minus or a "
                           f"range dash, so {_fmt(want)} is accepted")
            else:
                problems.append(f"The quote shows '{t.raw}', which reads as {_fmt(t.number)}, "
                                f"but the value is {_fmt(want)}")
    if problems:
        return _cr("value_in_quote", "fail", problems[0])
    if any(a in q for q in ev):
        return _cr("value_in_quote", "fail", f"'{v.as_reported}' appears in the quote only inside a longer number")
    return _cr("value_in_quote", "fail", f"'{v.as_reported}' is not in the quote '{_short(ev[0])}'")


_SCALE_RX = {
    "thousand": re.compile(r"\bthousands?\b|\bTEUR\b|\bkEUR\b|\bT€|\bk€|€\s?k\b|'000", re.IGNORECASE),
    "million": re.compile(r"\bmillions?\b|\bmn\b|\bmio\b|\bMEUR\b|\bEUR\s?m\b|€\s?m\b|\bm€|\$\s?m\b|\bUSD\s?m\b",
                          re.IGNORECASE),
    "billion": re.compile(r"\bbillions?\b|\bbn\b|\bmrd\b|€\s?bn\b|\bEUR\s?bn\b", re.IGNORECASE),
}
_CUR_RX = {"EUR": re.compile(r"€|\bEUR"), "USD": re.compile(r"\$|\bUSD"), "GBP": re.compile(r"£|\bGBP"),
           "CHF": re.compile(r"\bCHF")}
# "(in millions of €, earnings per share in €)": per-share figures in the same table are in units.
_PER_SHARE_UNITS = re.compile(r"per[\s-]share(?:\s+\w+){0,2}\s+in\s+(?:€|\$|£|EUR|USD|GBP|CHF)(?!\s?(?:m|bn|k)\b)",
                              re.IGNORECASE)


def _scales_named(q: str, per_share_item: bool) -> set[str]:
    """Scales a unit header names. A per-share qualifier makes the header name "one" for per-share items."""
    if per_share_item and _PER_SHARE_UNITS.search(q):
        return {"one"}
    return {k for k, rx in _SCALE_RX.items() if rx.search(q)}


def _unit_context(item: m.DataItem, uc: list[str], ev: list[str]) -> m.CheckResult:
    v = item.value
    hits = [t for q in ev for t in _value_hits(q, v)]
    word = next((t.word for t in hits if t.word), None)
    if word:  # "€2,160 million": the scale is written next to the number itself
        if word == v.scale:
            return _cr("unit_context", "pass", f"Scale '{v.scale}' written next to the number in the quote")
        return _cr("unit_context", "fail", f"The quote writes '{word}' next to the number, but the value says {v.scale}")
    if not uc:
        if v.scale == "one":
            return _cr("unit_context", "pass", "No scale needed: the value is stated in units")
        return _cr("unit_context", "fail", f"The value has scale '{v.scale}', but no unit_context citation names it")
    per_share = bool(re.search(r"per[\s-]share", item.statement, re.IGNORECASE))
    named = set().union(*(_scales_named(q, per_share) for q in uc))
    header = " / ".join(f"'{_short(q, 60)}'" for q in uc)
    if not named:
        if v.scale != "one":
            return _cr("unit_context", "fail", f"The unit_context quote {header} names no scale, but the value says {v.scale}")
    elif v.scale not in named:
        return _cr("unit_context", "fail",
                   f"The unit_context quote {header} names {_and(sorted(named))}, but the value says {v.scale}")
    if v.unit in _CURRENCIES:
        curs = {c for c, rx in _CUR_RX.items() if any(rx.search(q) for q in uc)}
        if curs and v.unit not in curs:
            return _cr("unit_context", "fail",
                       f"The unit_context quote {header} names {_and(sorted(curs))}, but the unit is {v.unit}")
    return _cr("unit_context", "pass", f"Scale '{v.scale}' from {header}")


_ORD = {1: "first", 2: "second", 3: "third", 4: "fourth"}
# Clause boundaries inside an evidence sentence. "over" and "than" introduce a comparison base ("growth of
# 4.0% over fiscal year 2026"), so the base sits in a clause of its own, as in stages/extract.py.
_CLAUSE = re.compile(r"[;()\[\]]|,\s|\s(?:compared\s+(?:with|to)|versus|vs\.?|against|from|to|while|whereas|"
                     r"over|than)\s", re.IGNORECASE)
# The words before a comparison base. A sentence that opens with "Over fiscal year 2026, ..." means "during",
# so a word, number or bracket must come before the comparison word.
_BASE_LEFT = re.compile(r"(?<=[\w%)])\s+(?:over|than|versus|vs\.?|against|compared\s+(?:with|to)|relative\s+to|"
                        r"level\s+of)\s+(?:in\s+)?(?:the\s+)?(?:(?:Q[1-4]|(?:first|second|third|fourth)\s+quarter)"
                        r"\s+(?:of\s+)?(?:the\s+)?)?$", re.IGNORECASE)
_PRIOR_LABEL = re.compile(r"\b(?:prior|previous)[\s-]year\s?:", re.IGNORECASE)  # "(prior year: €1.00)"
# A nil table cell standing alone: a dash of any kind, "n/a", "n.a." or "n.m.".
_NIL = re.compile(r"(?<!\S)(?:[-‐‑‒–—−]|n/a|n\.a\.|n\.m\.)(?!\S)", re.IGNORECASE)
# A word of a row label. Letters glued to a number ("1M", "2.5x") belong to that cell, not to a label.
_LABEL_WORD = re.compile(r"(?<!\d)[^\W\d_]+")


def _has_quarter(q: str, n: int) -> bool:
    return bool(re.search(rf"\bQ{n}\b|\b{n}Q\b|\b{_ORD[n]}[\s-]quarter\b", q, re.IGNORECASE))


def _ordered_years(mentions: list[tuple[int, int, int]]) -> list[int]:
    out: list[int] = []
    for y, _, _ in mentions:
        if not out or out[-1] != y:
            out.append(y)
    return out


def _row_cells(q: str, h: NumToken, pct: bool, floor: int = 0) -> tuple[list[int] | None, list[int]] | None:
    """The value cells of the table row around hit h that hold the same kind of value as h (% or amount),
    as start offsets: (with nil cells, without them). The first is None when a nil cell sits between a %
    cell and an amount, so its column kind is unknown. None when h is not a table cell.

    The row runs from the last label word before h to the next label word after it, so numbers inside a
    row label ("Loan (US$1,742 million)") are not cells and the next row's cells are left out. A number
    with a scale word or currency ("€2,160 million", the "1M" of "EURIBOR 1M") is prose or a label, never
    an amount cell. A % cell in a row that also holds amounts is a change column, never a year column."""
    if h.word or h.currency:
        return None
    toks = scan_numbers(q)
    nils = [mm.span() for mm in _NIL.finditer(q) if not any(t.start < mm.end() and mm.start() < t.end for t in toks)]
    words = [mm.span() for mm in _LABEL_WORD.finditer(q) if not any(s <= mm.start() < e for s, e in nils)]
    left = max([e for s, e in words if e <= h.core_start] + [floor])
    right = min([s for s, e in words if s >= h.end], default=len(q))
    cells: list[tuple[int, bool | None]] = [(t.start, t.pct) for t in toks if left <= t.core_start
                                            and t.end <= right and not (t.word or t.currency)]
    cells += [(s, None) for s, e in nils if left <= s and e <= right]
    cells.sort()
    kinds = [kind for _, kind in cells]
    if pct and False in kinds:
        return None
    bare = [s for s, kind in cells if kind == pct]
    full: list[int] | None = []
    for i, (s, kind) in enumerate(cells):
        if kind is None:
            near = {next((k for k in reversed(kinds[:i]) if k is not None), None),
                    next((k for k in kinds[i + 1:] if k is not None), None)} - {None}
            if len(near) > 1:
                full = None
                break
            kind = near.pop() if near else pct
        if kind == pct:
            full.append(s)
    return full, bare


def _columns(header: list[int], q: str, hits: list[NumToken], pct: bool, year: int, label: str, *, floor: int = 0):
    """True if a hit sits in the column the header labels with year, a failure detail if it sits in
    another year's column, None when the row cannot be aligned.

    Nil cells count as columns when that lines the row up with the header, or with the header repeated
    (a "current" and a "non-current" pair of year columns under one cited header). Otherwise the cells
    without nils align from the right, so a leading note column does not shift them."""
    if len(header) < 2:
        return None
    bad: list[str] = []
    for h in hits:
        row = _row_cells(q, h, pct, floor)
        if row is None:
            continue
        full, bare = row
        if full is not None and len(full) != len(bare) and len(full) % len(header) == 0:
            starts, cols = full, header * (len(full) // len(header))
        elif len(bare) >= len(header):
            starts, cols = bare[-len(header):], header
        else:
            continue
        if h.start in starts:
            j = starts.index(h.start)
            if cols[j] == year:
                return True
            bad.append(f"'{h.raw}' sits in value column {j + 1} of the row, which the header labels "
                       f"{cols[j]}, not {label}")
    return bad[0] if bad else None


def _period_style(q: str, s: int, e: int) -> bool:
    """A mention that names a reporting period: "fiscal year 2025", "FY2025", "Sep 30, 2025", "2025/26" or a
    comparative "(2024: ...". A bare year in prose ("due 2027", "expires in 2028") may be anything."""
    seg = q[s:e]
    if not re.fullmatch(r"\d{4}", seg):
        return True
    return bool(re.match(r"\)?\s?:", q[e:e + 3]))


def _borrowed(q: str, s: int, e: int) -> str | None:
    """The phrase that makes a period mention belong to another value, or None: a comparison base
    ("over fiscal year 2026") or a bracketed comparative ("(2024: 9%)")."""
    seg = q[max(0, s - 60):s]
    base = _BASE_LEFT.search(seg)
    if base:
        return (seg[base.start():] + q[s:e]).strip()
    opener = re.search(r"[(\[]\s?$", q[:s])
    if opener and re.match(r"\s?\)?\s?:", q[e:e + 4]):
        return q[opener.start():e] + ":"
    return None


def _free_mentions(q: str) -> list[tuple[int, int, int]]:
    """Period mentions that may set the period of a value elsewhere in the quote (not borrowed)."""
    return [(y, s, e) for y, s, e in period_mentions(q) if _borrowed(q, s, e) is None]


def _clause_cuts(q: str) -> list[tuple[int, int]]:
    protected = ([(t.start, t.end) for t in scan_numbers(q, include_labels=True)]
                 + [(s, e) for _, s, e in period_mentions(q)])
    return [mm.span() for mm in _CLAUSE.finditer(q) if not any(s <= mm.start() < e for s, e in protected)]


def _own_clause(q: str, hits: list[NumToken], year: int, label: str, latest: int | None):
    """What the value's own clause says about its period. True when some appearance of the value sits in a
    clause that names exactly year. A failure detail when every appearance sits in a clause that names one
    other period: a comparative ("(2024: 9%)", "(prior year: €1.00)") or "in fiscal year 2024". None when some
    appearance sits in a clause that names no period (or several), so the headers decide. latest is the latest
    year of the context, which a "prior year:" label counts back from."""
    cuts = _clause_cuts(q)
    ments = period_mentions(q)
    verdicts: list[bool | str | None] = []
    for h in hits:
        left = max([ce for cs, ce in cuts if ce <= h.start], default=0)
        right = min([cs for cs, ce in cuts if cs >= h.end], default=len(q))
        inside = [(y, s, e) for y, s, e in ments if s >= left and e <= right]
        if len({y for y, _, _ in inside}) >= 2:  # a header and its row ("Fiscal year 2025 2024 Revenue ...")
            verdicts.append(None)
            continue
        ys = {y for y, s, e in inside if _period_style(q, s, e)}
        prior = _PRIOR_LABEL.search(q[left:right]) if not ys else None
        if prior and latest is None:
            verdicts.append(f"'{h.raw}' sits next to '{prior.group(0)}' in the quote, a prior-year comparative, "
                            f"and no year in the context says which year that is, so it is not {label}")
            continue
        if prior:
            ys = {latest - 1}
        if len(ys) != 1:
            verdicts.append(None)
            continue
        y = next(iter(ys))
        what = f"'{prior.group(0)}' (a prior-year comparative, so {y})" if prior else str(y)
        verdicts.append(True if y == year else f"'{h.raw}' sits next to {what} in the quote, not {label}")
    return _first_verdict(verdicts)


def _first_verdict(verdicts: list):
    """Any True wins, then any None (no verdict), then the first failure detail."""
    if True in verdicts:
        return True
    if not verdicts or None in verdicts:
        return None
    return verdicts[0]


def _own_verdict(v: m.Value, ev: list[str], pc: list[str], year: int, label: str):
    """_own_clause over every evidence quote that holds the value: any True wins, then any None, then a failure."""
    ctx_years = ([y for p in pc for y, _, _ in period_mentions(p)] if pc
                 else [y for q in ev for y, _, _ in _free_mentions(q)])
    latest = max(ctx_years, default=None)
    verdicts = []
    for q in ev:
        hits = _value_hits(q, v)
        if hits:
            verdicts.append(_own_clause(q, hits, year, label, latest))
    return _first_verdict(verdicts)


def _merge(a, b):
    if a is True or b is True:
        return True
    return a if isinstance(a, str) else b


def _period_text(v: m.Value, ev: list[str], pc: list[str]) -> m.CheckResult | None:
    """period_match for a text value. The value's own clause in the evidence decides first: a comparative
    ("8% (2024: 9%)") or "in fiscal year 2024" next to the value wins over any header, so an agent-supplied
    header cannot mask it. Headers apply only when the clause names no period: then the period_context quote
    (or the evidence itself) must name the period, and a table value must sit in that year's column."""
    if not v.period:
        return None
    years, quarter = period_key(v.period)
    label = v.period
    if not years:
        return _cr("period_match", "blocked", f"Period '{label}' names no year, so it could not be checked")
    year = next(iter(years)) if len(years) == 1 else None
    if year is not None:
        own = _own_verdict(v, ev, pc, year, label)
        # A year named nowhere in the quotes fails below as "does not mention", the more basic failure.
        if isinstance(own, str) and any(y == year for q in ev + pc for y, _, _ in period_mentions(q)):
            return _cr("period_match", "fail", own)
        if own is True:
            if quarter and not any(_has_quarter(q, quarter) for q in ev + pc):
                return _cr("period_match", "fail", f"The evidence quote does not mention Q{quarter}")
            return _cr("period_match", "pass", f"The value's own clause in the evidence quote places it in {label}")
    ctx = pc or ev
    where = "period_context quote" if pc else "evidence quote"
    # Without a header, a comparison base or another value's comparative never sets this value's period.
    free = year is not None and not pc
    found = {y for q in ctx for y, _, _ in (_free_mentions(q) if free else period_mentions(q))}
    if not years <= found:
        lent = next((_borrowed(q, s, e) for q in ctx for y, s, e in period_mentions(q)
                     if free and y == year and _borrowed(q, s, e)), None)
        if lent:
            return _cr("period_match", "fail",
                       f"The {where} does not mention {label} except in '{lent}', a comparison base or another "
                       f"value's comparative, so it is not the period of '{v.as_reported}'")
        named = f" (it names {_and([str(y) for y in sorted(found)])})" if found else ""
        return _cr("period_match", "fail", f"The {where} does not mention {label}{named}")
    if quarter and not any(_has_quarter(q, quarter) for q in ctx):
        return _cr("period_match", "fail", f"The {where} does not mention Q{quarter}")
    if year is None:
        return _cr("period_match", "pass", f"The {where} names every year of {label}")
    pct = v.unit == "%"
    verdict = None
    for q in ev:
        hits = _value_hits(q, v)
        if pc:
            header = _ordered_years([mm for p in pc for mm in period_mentions(p)])
            verdict = _merge(verdict, _columns(header, q, hits, pct, year, label))
            continue
        ments = period_mentions(q)
        if len({y for y, _, _ in ments}) < 2:
            continue
        head_end = max(e for _, _, e in ments)
        row = [t for t in scan_numbers(q) if t.pct == pct]
        if row and head_end <= row[0].start:  # a header line, then a row
            verdict = _merge(verdict, _columns(_ordered_years(ments), q, hits, pct, year, label, floor=head_end))
    if isinstance(verdict, str):
        return _cr("period_match", "fail", verdict)
    tail = " and the value sits in that year's column" if verdict is True else ""
    return _cr("period_match", "pass", f"The {where} names {label}{tail}")


def _source_resolves(item: m.DataItem, sources: dict[str, m.Source]) -> m.CheckResult:
    src = sources.get(item.source_id)
    if src is None:
        return _cr("citations_resolve", "fail", f"Source {item.source_id} is not in this run's plan")
    issue = _source_issue(src)
    if issue:
        return _cr("citations_resolve", "blocked", issue[:1].upper() + issue[1:])
    foreign = sorted({c.doc_id for c in item.citations if src.doc_ids and c.doc_id not in src.doc_ids})
    if foreign:
        return _cr("citations_resolve", "fail", f"{_and(foreign)} does not belong to {src.source_id}")
    return _cr("citations_resolve", "pass", f"Source {src.source_id} is {src.approval.replace('_', ' ')} and not revoked")


def evidence_pages(item: m.DataItem, results: dict[int, m.CheckResult] | None = None) -> dict[str, list[int]]:
    """Each document's pages that hold a text evidence quote of the item: where its quote_exact found it
    (results, by citation index), else where it is cited."""
    out: dict[str, list[int]] = {}
    for i, cit in enumerate(item.citations):
        if cit.role != "evidence" or not isinstance(cit.locator, m.TextLocator):
            continue
        res = (results or {}).get(i)
        page = res.closest_locator.page if res is not None and res.result == "pass" and res.closest_locator \
            else cit.locator.page
        pages = out.setdefault(cit.doc_id, [])
        if page not in pages:
            pages.append(page)
    return out


def _text_quote_results(item: m.DataItem, pages_by_doc: dict[str, list[str]],
                        sources: dict[str, m.Source]) -> dict[int, m.CheckResult]:
    """quote_exact per text citation index. Evidence first, so each unit or period header is then looked
    for only on the page where its evidence was found (see context_pages)."""
    out: dict[int, m.CheckResult] = {}
    for i, cit in enumerate(item.citations):
        if cit.role == "evidence" and isinstance(cit.locator, m.TextLocator):
            out[i] = _quote_exact(cit, pages_by_doc)
    ev_pages = evidence_pages(item, out)
    src = sources.get(item.source_id) if sources else None
    for i, cit in enumerate(item.citations):
        if i not in out and isinstance(cit.locator, m.TextLocator):
            out[i] = _quote_exact(cit, pages_by_doc, context_pages(cit, ev_pages, src))
    return out


def check_data_item(item: m.DataItem, pages_by_doc: dict[str, list[str]],
                    sources: dict[str, m.Source]) -> list[m.CheckResult]:
    """pages_by_doc maps doc_id to page texts (page n at index n-1). Leave quarantined documents out.
    Unit and period headers are looked for only where the evidence was found (context_pages)."""
    out: list[m.CheckResult] = []
    roles: dict[str, list[str]] = {"evidence": [], "unit_context": [], "period_context": []}
    json_docs: list[tuple[m.Citation, object]] = []
    quotes = _text_quote_results(item, pages_by_doc, sources)
    for i, cit in enumerate(item.citations):
        if isinstance(cit.locator, m.TextLocator):
            out.append(quotes[i])
            roles[cit.role].append(text.normalize(cit.quote))
        else:
            res, doc = _json_value(item, cit, pages_by_doc)
            out.append(res)
            if doc is not None and cit.role == "evidence":
                json_docs.append((cit, doc))
    ev, uc, pc = roles["evidence"], roles["unit_context"], roles["period_context"]
    v = item.value
    if v is not None:
        if ev:
            out.append(_value_in_quote(v, ev))
            out.append(_unit_context(item, uc, ev))
            p = _period_text(v, ev, pc)
            if p is not None:
                out.append(p)
        elif not json_docs:
            out.append(_cr("value_in_quote", "fail", "No evidence citation holds the value"))
        if json_docs and not ev:
            cit, doc = json_docs[0]
            p = _json_period(v, doc, cit.locator.pointer)
            if p is not None:
                out.append(p)
    out.append(_numbers_traced(item.statement, _data_cands(item, quotes=True), what="statement"))
    out.append(_source_resolves(item, sources))
    return out


# ---------------------------------------------------------------------------
# Conclusions and computations
# ---------------------------------------------------------------------------


def _data_env(c: m.Conclusion, items: dict) -> dict[str, Decimal]:
    env: dict[str, Decimal] = {}
    for did in c.data_ids:
        it = items.get(did)
        if it is not None and it.kind == "data" and it.value is not None:
            env[did] = base_value(it)
    return env


def conclusion_env(c: m.Conclusion, items: dict) -> dict[str, Decimal]:
    """Base values of the cited data plus every computation, in order. Raises compute.ComputeError."""
    env = _data_env(c, items)
    for comp in c.computations:
        env[comp.name] = compute.safe_eval(comp.expr, env)
    return env


def display_of(value: Decimal, display: str) -> tuple[bool, str]:
    """Whether display shows value after half up at its own decimals, and what it should show.
    "38.2%" reads the value as a fraction, "€1.2 billion" divides by the word, "2.5" is as is."""
    toks = scan_numbers(text.normalize(display), include_labels=True)
    if not toks:
        return False, "a number"
    t = toks[0]
    x = value * 100 if t.pct else value / WORD_FACTOR[t.word] if t.word else value
    shown = half_up(x, t.places)
    expect = f"{Decimal(shown):,}" + ("%" if t.pct else "") + (f" {t.word}" if t.word else "")
    return Decimal(shown) == t.number, expect


_ID_RX = re.compile(r"\b([A-Z]{1,4})-(\d{1,4})\b")  # the id form compute.ids_in reads
_COUNT = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
MAX_AVERAGE_GAP_DAYS = 396  # about 13 months: two year-ends one fiscal year apart, with a little slack


def _expr_tree(expr: str) -> tuple[ast.Expression, dict[str, str]] | None:
    """The parsed expression and a map from its Python names back to item ids ("D_021" -> "D-021")."""
    back = {f"{a}_{b}": f"{a}-{b}" for a, b in (i.split("-") for i in compute.ids_in(expr))}
    try:
        return ast.parse(_ID_RX.sub(lambda mm: f"{mm.group(1)}_{mm.group(2)}", expr), mode="eval"), back
    except SyntaxError:
        return None


def _operands(expr: str) -> set[str]:
    """Every D-id and computation name an expression reads."""
    parsed = _expr_tree(expr)
    if parsed is None:
        return set()
    tree, back = parsed
    return {back.get(n.id, n.id) for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _averages(expr: str) -> list[tuple[str, str]]:
    """Operand pairs the expression averages as (A + B) / 2, each a D-id or a computation name."""
    parsed = _expr_tree(expr)
    if parsed is None:
        return []
    tree, back = parsed
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and isinstance(n.right, ast.Constant)
                and n.right.value == 2 and isinstance(n.left, ast.BinOp) and isinstance(n.left.op, ast.Add)
                and isinstance(n.left.left, ast.Name) and isinstance(n.left.right, ast.Name)):
            out.append((back.get(n.left.left.id, n.left.left.id), back.get(n.left.right.id, n.left.right.id)))
    return out


def _period_point(p: str | None) -> tuple[str, object] | None:
    """A year-end as ("date", date) for "2025-09-30" or ("fy", 2025) for "FY2025". Anything else is None."""
    p = (p or "").strip()
    mm = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", p)
    if mm:
        try:
            return "date", date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
        except ValueError:
            return None
    mm = re.fullmatch(r"FY\s?(\d{4})", p, re.IGNORECASE)
    return ("fy", int(mm.group(1))) if mm else None


_DATED = re.compile(r"\b(?:at|as\s+(?:of|at)|on)\s+(?:the\s+end\s+of\s+)?(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+|"
                    r"\d{1,2}\s+[A-Z][a-z]{2,8}\.?,?\s+)?(?:19|20)\d{2}\b|\b(?:19|20)\d{2}-\d{2}-\d{2}\b",
                    re.IGNORECASE)


def _year_end(name: str, c: m.Conclusion, items: dict, seen: frozenset[str] = frozenset()) -> str | None:
    """The one balance-sheet date behind an operand, as its period label: a D-id whose period is an ISO date
    or whose statement is dated ("at September 30, 2023"), or an earlier computation whose D-ids all share
    one such period (ticap_fy23 = D-127 - D-126 - ..., all at FY2023). None when unclear or not a balance."""
    it = items.get(name)
    if it is not None and it.kind == "data":
        p = it.value.period if it.value is not None else None
        point = _period_point(p)
        if point is None or (point[0] == "fy" and not _DATED.search(it.statement)):
            return None
        return p
    comp = next((x for x in c.computations if x.name == name), None)
    if comp is None or name in seen:
        return None
    periods = {_year_end(o, c, items, seen | {name}) for o in _operands(comp.expr)}
    return periods.pop() if len(periods) == 1 and None not in periods else None


def _year_end_gap(a: str, b: str) -> str | None:
    """How far apart two year-ends are, in words, when that is clearly more than one fiscal year, else None.
    Two fiscal-year labels count whole years. A label against a date counts from the nearest day of the
    label's calendar year, since its year-end month is not known."""
    pa, pb = _period_point(a), _period_point(b)
    if pa is None or pb is None:
        return None
    if pa[0] == pb[0] == "fy":
        years = abs(pa[1] - pb[1])
        return f"{_COUNT.get(years, str(years))} fiscal years" if years >= 2 else None
    if pa[0] == pb[0] == "date":
        days = abs((pa[1] - pb[1]).days)
    else:
        fy, d = (pa[1], pb[1]) if pa[0] == "fy" else (pb[1], pa[1])
        lo, hi = date(fy, 1, 1), date(fy, 12, 31)
        days = 0 if lo <= d <= hi else min(abs((d - lo).days), abs((d - hi).days))
    if days <= MAX_AVERAGE_GAP_DAYS:
        return None
    years = round(days / 365.25)
    if abs(days / 365.25 - years) < 0.1:
        return f"{_COUNT.get(years, str(years))} years"
    return f"about {round(days / 30.44)} months"


def _average_gaps(comp: m.Computation, c: m.Conclusion, items: dict) -> list[str]:
    """An average balance ((A + B) / 2, as in average TICAP) must use year-ends one fiscal year apart. Two
    D-ids are only compared when they look like the same metric (same unit, mostly the same words)."""
    out = []
    for a, b in _averages(comp.expr):
        if a == b:
            continue
        ia, ib = items.get(a), items.get(b)
        if ia is not None and ib is not None and ia.kind == ib.kind == "data":
            if ia.value is None or ib.value is None or ia.value.unit != ib.value.unit:
                continue
            wa, wb = _words(ia.statement), _words(ib.statement)
            if not (wa and wb) or len(wa & wb) / min(len(wa), len(wb)) < 0.6:
                continue
        pa, pb = _year_end(a, c, items), _year_end(b, c, items)
        gap = _year_end_gap(pa, pb) if pa and pb else None
        if gap:
            out.append(f"{comp.name}: the average of {a} and {b} uses year-ends {pa} and {pb}, {gap} apart, "
                       f"not one fiscal year")
    return out


def _computations(c: m.Conclusion, items: dict) -> m.CheckResult | None:
    if not c.computations:
        return None
    env = _data_env(c, items)
    problems: list[str] = []
    for comp in c.computations:
        used = compute.ids_in(comp.expr)
        stray = [r for r in used if r not in c.data_ids]
        if stray:
            problems.append(f"{comp.name} uses {_and(stray)}, which the conclusion does not cite")
            continue
        no_value = [r for r in used if r not in env]
        if no_value:
            problems.append(f"{comp.name} uses {_and(no_value)}, which has no value")
            continue
        try:
            val = compute.safe_eval(comp.expr, env)
        except compute.ComputeError as e:
            problems.append(f"{comp.name}: {e}")
            continue
        env[comp.name] = val
        if abs(val - _dec(comp.result)) > max(Decimal("1e-6"), abs(val) * Decimal("1e-9")):
            problems.append(f"{comp.name}: the stored result is {comp.result}, but it recomputes to {_fmt(val)}")
        ok, expect = display_of(val, comp.display)
        if not ok:
            problems.append(f"{comp.name} = {_fmt(val)}, which rounds half up to {expect}, "
                            f"but the display says {comp.display}")
        problems += _average_gaps(comp, c, items)
    if problems:
        return _cr("computation", "fail", "; ".join(problems))
    return _cr("computation", "pass", f"{len(c.computations)} computation(s) re-evaluated from data values")


def check_conclusion(c: m.Conclusion, items: dict[str, m.Item], sources: dict[str, m.Source], *,
                     rubric: dict | None = None) -> list[m.CheckResult]:
    rub = rubric or RUBRIC
    out = [_resolve(c.data_ids, items, sources, kinds=("data",))]
    comp = _computations(c, items)
    if comp is not None:
        out.append(comp)
    cands = [x for cp in c.computations for x in _text_cands(f"{c.id}:{cp.name}", cp.display)]
    cands += _threshold_cands(c.criteria, rub) + _pool(c.data_ids, items, rubric=rub)
    out.append(_numbers_traced(c.text, cands, what="conclusion"))
    return out


# ---------------------------------------------------------------------------
# Extrapolations (BOFFIN scores)
# ---------------------------------------------------------------------------

_CMP = re.compile(r"(>=|<=|≥|≤|>|<)\s*(\d+(?:\.\d+)?)\s*(%|x|y)?")


def _threshold(s: str | None) -> tuple[str, Decimal, str] | None:
    """A rubric bar with exactly one comparison ("> 15%", "... dividends > 2x"). Worded bars give None."""
    found = _CMP.findall(s or "")
    if len(found) != 1:
        return None
    op, num, unit = found[0]
    return op, Decimal(num), unit


def _meets(x: Decimal, thr: tuple[str, Decimal, str]) -> bool:
    op, num, unit = thr
    t = num / 100 if unit == "%" else num
    return {">": x > t, "<": x < t, ">=": x >= t, "≥": x >= t, "<=": x <= t, "≤": x <= t}[op]


def _bar(thr: tuple[str, Decimal, str]) -> str:
    return f"{thr[0]} {thr[1]}{thr[2]}"


def _shown_against(x: Decimal, unit: str, bars: list[tuple[str, Decimal, str]]) -> str:
    """The recomputed value as the detail shows it. Within half a display step of a bar, "10.0%" could be
    either side of a 10% bar, so more decimals are shown until it is unambiguous, plus which side it is on
    ("9.996%, below the 10% bar"). The score itself is always decided on the exact value."""
    bars = [b for b in bars if b[2] == unit]
    v = x * 100 if unit == "%" else x
    base = 1 if unit == "%" else 2
    half_step = Decimal(5) * Decimal(10) ** -(base + 1)
    places = base
    while places < 8 and any(v != b[1] and Decimal(half_up(v, places)) == b[1] for b in bars):
        places += 1
    shown = f"{half_up(v, places)}{unit}"
    near = [b[1] for b in bars if abs(v - b[1]) <= half_step]
    if not near:
        return shown
    n = near[0]
    side = "exactly at" if v == n else "below" if v < n else "above"
    return f"{shown}, {side} the {n}{unit} bar"


def _threshold_rule(e: m.Extrapolation, items: dict, crit: dict) -> m.CheckResult | None:
    if e.score is None or crit.get("scoring") != "threshold":
        return None
    plus, minus = _threshold(crit.get("plus_one")), _threshold(crit.get("minus_one"))
    if plus is None or minus is None:
        return None  # a worded bar: the judges check it, code cannot
    if not e.rule.input:
        return _cr("threshold_rule", "fail",
                   f"{e.criterion} is scored by a threshold, so rule.input must name a computation (C-###:name)")
    cid, _, name = e.rule.input.partition(":")
    c = items.get(cid)
    if cid not in e.conclusion_ids:
        return _cr("threshold_rule", "fail", f"The rule input {e.rule.input} is not among the cited conclusions")
    if c is None or c.kind != "conclusion" or name not in {cp.name for cp in c.computations}:
        return _cr("threshold_rule", "fail", f"The rule input {e.rule.input} names no computation")
    try:
        x = conclusion_env(c, items)[name]
    except compute.ComputeError as err:
        return _cr("threshold_rule", "fail", f"Could not recompute {e.rule.input}: {err}")
    shown = _shown_against(x, plus[2], [plus, minus])
    if _meets(x, plus):
        expected, why = 1, f"meets the +1 bar ({_bar(plus)})"
    elif _meets(x, minus):
        expected, why = -1, f"meets the -1 bar ({_bar(minus)})"
    else:
        expected, why = 0, f"sits between the bars ({_bar(minus)} and {_bar(plus)})"
    if expected == e.score:
        return _cr("threshold_rule", "pass", f"Recomputed {e.rule.input} = {shown}, which {why}: score {_score(expected)}")
    return _cr("threshold_rule", "fail", f"Recomputed {e.rule.input} = {shown}, which {why}, so the score is "
                                         f"{_score(expected)}, not {_score(e.score)}")


def check_extrapolation(e: m.Extrapolation, items: dict[str, m.Item], rubric: dict | None, *,
                        sources: dict[str, m.Source] | None = None) -> list[m.CheckResult]:
    rub = rubric or RUBRIC
    crit = rub.get("criteria", {}).get(e.criterion)
    if crit is None:
        res = _cr("citations_resolve", "fail", f"{e.criterion} is not a criterion in the rubric")
    elif crit.get("category") != e.category:
        res = _cr("citations_resolve", "fail", f"{e.criterion} belongs to {crit.get('category')}, not {e.category}")
    elif e.score is None:
        if not (e.na_reason or "").strip():
            res = _cr("citations_resolve", "fail", "N/A without a stated reason")
        elif e.conclusion_ids:
            res = _resolve(e.conclusion_ids, items, sources, kinds=("conclusion",))
            if res.result == "pass":
                res = _cr("citations_resolve", "pass", "N/A with a stated reason, and the cited conclusions resolve")
        else:
            res = _cr("citations_resolve", "pass", "N/A with a stated reason")
    elif not e.conclusion_ids:
        res = _cr("citations_resolve", "fail", "A score must cite at least one conclusion")
    else:
        res = _resolve(e.conclusion_ids, items, sources, kinds=("conclusion",))
    out = [res]
    thr = _threshold_rule(e, items, crit) if crit else None
    if thr is not None:
        out.append(thr)
    cands = _threshold_cands([e.criterion], rub) + _SCORE_CANDS + _pool(e.conclusion_ids, items, rubric=rub)
    out.append(_numbers_traced(e.text, cands, what="score text"))
    return out


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _places(y: float) -> int:
    exp = _dec(y).as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def _chart_expected(g: m.Chart, ref: str, items: dict, envs: dict) -> tuple[Decimal | None, str | None]:
    base, _, name = ref.partition(":")
    it = items.get(base)
    if it is None:
        return None, f"{ref} does not exist"
    if it.kind == "data":
        v = it.value
        if name or v is None:
            return None, f"{ref} has no single value to plot"
        if g.unit == "%":
            return (_dec(v.number), None) if v.unit == "%" else (None, f"{base} is in {v.unit}, the chart is in %")
        if v.unit != g.unit:
            return None, f"{base} is in {v.unit}, the chart is in {g.unit}"
        return to_base(_dec(v.number), v.scale) / SCALES[g.scale], None
    if it.kind == "conclusion":
        if not name:
            return None, f"{ref} must name a computation (C-###:name)"
        if base not in envs:
            try:
                envs[base] = conclusion_env(it, items)
            except compute.ComputeError as e:
                return None, f"{base} could not be recomputed: {e}"
        if name not in envs[base]:
            return None, f"{ref} names no computation"
        x = envs[base][name]
        return (x * 100 if g.unit == "%" else x / SCALES[g.scale]), None
    return None, f"{ref} is a {it.kind} item; charts read data values and computations"


def check_chart(g: m.Chart, items: dict[str, m.Item], *,
                sources: dict[str, m.Source] | None = None) -> list[m.CheckResult]:
    """Pass sources, so a chart resting on a revoked, quarantined or rejected source comes back blocked."""
    out = [_resolve(g.refs, items, sources, kinds=("data", "conclusion"))]
    problems: list[str] = []
    envs: dict[str, dict[str, Decimal]] = {}
    n = 0
    for s in g.series:
        for p in s.points:
            n += 1
            if p.ref.split(":")[0] not in g.refs:
                problems.append(f"{s.name} {p.x}: {p.ref} is not listed in refs")
                continue
            exp, err = _chart_expected(g, p.ref, items, envs)
            if err:
                problems.append(f"{s.name} {p.x}: {err}")
                continue
            shown = half_up(exp, _places(p.y))
            if Decimal(shown) != _dec(p.y):
                problems.append(f"{s.name} {p.x}: plotted {p.y}, but {p.ref} gives {shown}")
    if n == 0:
        problems.append("The chart has no points")
    if problems:
        out.append(_cr("chart_values", "fail", "; ".join(problems)))
    else:
        out.append(_cr("chart_values", "pass", f"{n} of {n} points equal their data items or computations"))
    return out


# ---------------------------------------------------------------------------
# Brief sentences
# ---------------------------------------------------------------------------


def _item_text(it) -> str:
    if it.kind == "data":
        return it.statement
    return it.title if it.kind == "chart" else it.text


def _copy_exact(s: m.SummarySentence, items: dict) -> m.CheckResult:
    if len(s.derived_from) != 1:
        return _cr("copy_exact", "fail", "A copied sentence must cite exactly one item")
    ref = s.derived_from[0]
    it = items.get(ref)
    if it is None:
        return _cr("copy_exact", "fail", f"{ref} does not exist")
    a, b = text.normalize(s.text).strip(), text.normalize(_item_text(it)).strip()
    if a == b:
        return _cr("copy_exact", "pass", f"Equals {ref} exactly")
    diff = text.diff_segments(a, b)
    return _cr("copy_exact", "fail", f"Differs from the text of {ref} in {text.changed_chars(diff)} character(s)",
               diff=diff)


def check_sentence(s: m.SummarySentence, items: dict[str, m.Item], holes: list[m.Hole], *,
                   rollup: m.Rollup | None = None, rubric: dict | None = None,
                   sources: dict[str, m.Source] | None = None) -> list[m.CheckResult]:
    """Pass rollup when a sentence cites "rollup", or its scorecard numbers come back blocked."""
    out = [_resolve(s.derived_from, items, sources, kinds=("data", "conclusion", "extrapolation", "chart"),
                    holes=holes, allow_rollup=True)]
    if s.mode == "copied":
        out.append(_copy_exact(s, items))
        return out
    blocked = None
    if "rollup" in s.derived_from and rollup is None:
        blocked = "The rollup was not given, so the scorecard numbers could not be traced"
    cands = _pool(s.derived_from, items, rubric=rubric or RUBRIC, rollup=rollup)
    out.append(_numbers_traced(s.text, cands, what="sentence", blocked_reason=blocked))
    return out


# ---------------------------------------------------------------------------
# Tier rules
# ---------------------------------------------------------------------------

_STOP = set("the a an of in for and at on to was were is are by with as its their our fiscal year fy million "
            "billion thousand eur usd per from than that this which be been has had have end total".split())


def _tier(it, sources: dict) -> str:
    src = sources.get(it.source_id) if sources else None
    return src.tier if src is not None else it.source_tier


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in _STOP and len(w) > 1}


def _same_metric(a: m.DataItem, b: m.DataItem) -> bool:
    """Same unit, same period years, and the statements share most of their content words."""
    va, vb = a.value, b.value
    if va.unit != vb.unit or period_key(va.period or "")[0] != period_key(vb.period or "")[0]:
        return False
    wa, wb = _words(a.statement), _words(b.statement)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def _agrees(ref: m.DataItem, shown: m.DataItem) -> bool:
    """shown's number equals ref's value at shown's scale and decimals ("18.7 billion" vs 18,742 million)."""
    toks = scan_numbers(text.normalize(shown.value.as_reported), include_labels=True)
    places = toks[0].places if toks else _places(shown.value.number)
    if ref.value.unit == "%":
        x = _dec(ref.value.number)
    else:
        x = to_base(_dec(ref.value.number), ref.value.scale) / SCALES[shown.value.scale]
    return Decimal(half_up(x, places)) == _dec(shown.value.number)


def _shown(it: m.DataItem) -> str:
    v = it.value
    return f"{v.as_reported}" + ("" if v.scale == "one" else f" {v.scale}") + ("" if v.unit == "%" else f" {v.unit}")


def check_tier_rule(item, items: dict[str, m.Item], sources: dict[str, m.Source]) -> list[m.CheckResult]:
    """tier_rule for one item: a tier 3 number needs priority or tier 2 support, a tier 2 number must
    not contradict tier 1, and a conclusion may not rest on tier 3 data alone. [] when it does not apply."""
    if item.kind == "conclusion":
        data = [items[d] for d in item.data_ids if d in items and items[d].kind == "data"]
        t3 = [d.id for d in data if _tier(d, sources) == "tier_3"]
        if not t3:
            return []
        if len(t3) == len(data):
            return [_cr("tier_rule", "fail", f"Every cited data item ({_and(t3)}) comes from a tier 3 source")]
        return [_cr("tier_rule", "pass", f"Tier 3 items ({_and(t3)}) are not the only support")]
    if item.kind != "data" or item.value is None:
        return []
    tier = _tier(item, sources)
    if tier == "priority":
        return []
    others = [o for o in items.values()
              if o.kind == "data" and o.id != item.id and o.value is not None
              and o.verification.status not in ("fail", "blocked") and _same_metric(item, o)]
    if tier == "tier_2":
        t1 = [o for o in others if _tier(o, sources) == "priority"]
        if not t1:
            return [_cr("tier_rule", "pass", "No tier 1 figure for this metric and period, so the tier 2 value may stand")]
        agree = [o for o in t1 if _agrees(o, item)]
        if agree:
            return [_cr("tier_rule", "pass", f"Agrees with {agree[0].id} (tier 1)")]
        return [_cr("tier_rule", "fail", f"Contradicts {t1[0].id} (tier 1): {_shown(item)} here, "
                                         f"{_shown(t1[0])} in the priority source")]
    support = [o for o in others if _tier(o, sources) != "tier_3" and (_agrees(o, item) or _agrees(item, o))]
    if support:
        return [_cr("tier_rule", "pass", f"Corroborated by {support[0].id} ({_tier(support[0], sources)})")]
    return [_cr("tier_rule", "fail", "Only a tier 3 source supports this number, and no priority or tier 2 item states it")]


def check_tier_rules(items: dict[str, m.Item], sources: dict[str, m.Source]) -> dict[str, list[m.CheckResult]]:
    """tier_rule results for every item they apply to, keyed by item id."""
    out: dict[str, list[m.CheckResult]] = {}
    for iid, it in items.items():
        res = check_tier_rule(it, items, sources)
        if res:
            out[iid] = res
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def check_item(item, *, items: dict[str, m.Item], sources: dict[str, m.Source],
               pages_by_doc: dict[str, list[str]] | None = None, rubric: dict | None = None) -> list[m.CheckResult]:
    """Every code check for one report item, by kind. Tier rules are separate (they read all items)."""
    if item.kind == "data":
        return check_data_item(item, pages_by_doc or {}, sources)
    if item.kind == "conclusion":
        return check_conclusion(item, items, sources, rubric=rubric)
    if item.kind == "extrapolation":
        return check_extrapolation(item, items, rubric, sources=sources)
    return check_chart(item, items, sources=sources)
