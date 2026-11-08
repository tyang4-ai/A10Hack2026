"""Number parsing, scale conversion and display rounding (round half up, never format).

Filings write numbers many ways: "1,234", "(1,234)" for a negative, a unicode minus, "€1.2 billion",
"12.5%". Everything here is Decimal, so a check never trips on float error. Years, fiscal-year
labels, dates and ids are not numbers a claim has to trace, so scan_numbers skips them by default.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

SCALES: dict[str, Decimal] = {"one": Decimal(1), "thousand": Decimal(10) ** 3,
                              "million": Decimal(10) ** 6, "billion": Decimal(10) ** 9}
WORD_FACTOR: dict[str, Decimal] = {"thousand": SCALES["thousand"], "million": SCALES["million"],
                                   "billion": SCALES["billion"], "trillion": Decimal(10) ** 12}

_WORDS = {"thousand": "thousand", "thousands": "thousand", "k": "thousand",
          "million": "million", "millions": "million", "mn": "million", "m": "million",
          "mio": "million", "mio.": "million",
          "billion": "billion", "billions": "billion", "bn": "billion",
          "trillion": "trillion", "trillions": "trillion", "tn": "trillion"}
_CURRENCY = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY",
             "EUR": "EUR", "USD": "USD", "GBP": "GBP", "CHF": "CHF", "JPY": "JPY"}
_MINUS = "-−‒–"  # hyphen-minus, unicode minus, figure dash, en dash

_TOKEN = re.compile(
    r"(?<![\w.,])"
    r"(?P<open>\()?"
    r"(?:(?P<cur1>[€$£¥]|(?:EUR|USD|GBP|CHF|JPY)(?=[\s\d+\-−‒–]))\s?)?"
    r"(?:(?P<sign>[+\-−‒–])(?P<gap>\s{0,3}))?"  # SHL PDFs put a thin space after the minus
    r"(?:(?P<cur2>[€$£¥])\s?)?"
    r"(?P<core>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?P<pct>\s?(?:%|percent\b|per\s+cent\b))?"
    r"(?:\s?(?P<word>thousands?|millions?|billions?|trillions?|mn|bn|tn|mio\.?)(?!\w)|(?P<abbr>[mk])(?!\w))?"
    r"(?:\s?(?P<cur3>[€$£¥]|(?:EUR|USD|GBP|CHF|JPY)(?!\w)))?"
    r"(?P<close>\))?",
    re.IGNORECASE,
)

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
          r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")

# Spans whose digits are labels, not amounts: dates, fiscal-year labels, quarters, notes, ids, forms.
_SKIP = [
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?!\w)"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"),
    re.compile(r"\b(?:FY|fiscal(?:\s+year)?)\s?'?\d{2,4}(?:\s?/\s?\d{2,4})?\b", re.IGNORECASE),
    re.compile(r"\b(?:19|20)\d{2}\s?/\s?\d{2}\b"),
    re.compile(r"\b[QH][1-4]\b|\b[1-4][QH]\b"),
    re.compile(r"(?:\b(?:IFRS|IAS|Notes?|notes?|No\.|pages?|Section|section|Article|Art\.)|§)\s?\d+"),
    re.compile(r"\b[A-Za-z]+-\d+\b"),
    re.compile(r"\b\d+-[A-Z]\b"),
]

# Period mentions, most specific first. Each gives the fiscal or calendar year it names.
_FY = re.compile(r"\b(?:FY|fiscal(?:\s+year)?)\s?'?(?P<y>\d{4}|\d{2})(?:\s?/\s?(?P<y2>\d{4}|\d{2}))?(?!\d|,\d|\.\d)",
                 re.IGNORECASE)
_MDY = re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?P<y>(?:19|20)\d{{2}})(?!\d)")
_DMY_WORD = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+(?P<y>(?:19|20)\d{{2}})(?!\d)")
_DMY_NUM = re.compile(r"(?<!\d)\d{1,2}[./]\d{1,2}[./](?P<y>(?:19|20)\d{2})(?!\d)")
_YEAR_SLASH = re.compile(r"(?<![\w.,])(?P<y>(?:19|20)\d{2})\s?/\s?(?P<y2>\d{2})(?!\d)")
_YEAR = re.compile(r"(?<![\w.,])(?P<y>(?:19|20)\d{2})(?!\d|,\d|\.\d|\s?%)")
_QUARTER = re.compile(r"\bQ([1-4])\b", re.IGNORECASE)


@dataclass(frozen=True)
class NumToken:
    """One number as written in a text, with where it sits and what it means."""
    raw: str            # the token as written: "(1,204)", "€1.2 billion", "12.5%"
    start: int
    end: int
    core: str           # the digits as written: "1,204"
    core_start: int
    core_end: int
    number: Decimal     # signed, before any scale word: "(1,204)" gives -1204, "€1.2 billion" gives 1.2
    places: int         # decimals shown
    pct: bool
    word: str | None    # "thousand", "million", "billion" or "trillion" when written next to the number
    currency: str | None
    # A dash, then a space, then digits, inside running text ("sales – 8,722", "5 – 6%"): a table minus
    # or a range dash. number reads it as a minus. Sign-sensitive checks accept either reading.
    sign_ambiguous: bool = False

    @property
    def value(self) -> Decimal:
        """The number with its scale word applied: "€1.2 billion" gives 1200000000."""
        return self.number * WORD_FACTOR[self.word] if self.word else self.number


def _make(text: str, mm: re.Match) -> NumToken:
    """Turn a regex match into a token: pair parentheses, keep a sign only where it is a sign."""
    open_, close = mm.group("open"), mm.group("close")
    paren = bool(open_ and close)
    start, end = mm.start(), mm.end()
    if open_ and not paren:
        start += 1
    if close and not paren:
        end -= 1
    neg = paren
    ambiguous = False
    sign = mm.group("sign")
    if sign:
        si = mm.start("sign")
        prev = text[si - 1] if si > 0 else ""
        if mm.group("cur1") is None and prev and prev in "%)]":  # "5%-6%": a range dash, not a minus
            start = mm.start("cur2") if mm.group("cur2") else mm.start("core")
        else:
            neg = neg or sign in _MINUS
            ambiguous = bool(mm.group("gap")) and prev not in ("", "(", "[") and not paren
    core = mm.group("core")
    number = Decimal(core.replace(",", ""))
    word_raw = mm.group("word") or mm.group("abbr")
    cur = mm.group("cur1") or mm.group("cur2") or mm.group("cur3")
    return NumToken(
        raw=text[start:end], start=start, end=end, core=core,
        core_start=mm.start("core"), core_end=mm.end("core"),
        number=-number if neg else number,
        places=len(core.split(".", 1)[1]) if "." in core else 0,
        pct=bool(mm.group("pct")),
        word=_WORDS[word_raw.lower()] if word_raw else None,
        currency=_CURRENCY.get(cur.strip().upper()) if cur else None,
        sign_ambiguous=ambiguous,
    )


def _is_year(t: NumToken) -> bool:
    """A bare 4-digit 1900-2100. "2024 – 2025" is a range of years, so an ambiguous dash still counts."""
    return (not t.pct and t.word is None and t.currency is None and (t.number > 0 or t.sign_ambiguous)
            and t.places == 0 and len(t.core) == 4 and 1900 <= abs(t.number) <= 2100)


def _skip_spans(text: str) -> list[tuple[int, int]]:
    return [mm.span() for rx in _SKIP for mm in rx.finditer(text)]


def scan_numbers(text: str, *, include_labels: bool = False) -> list[NumToken]:
    """Every number token in text, in order. Years, fiscal-year labels, dates and ids are skipped
    unless include_labels is set."""
    skips = [] if include_labels else _skip_spans(text)
    out: list[NumToken] = []
    for mm in _TOKEN.finditer(text):
        t = _make(text, mm)
        if not include_labels and (_is_year(t) or any(s < t.core_end and t.core_start < e for s, e in skips)):
            continue
        out.append(t)
    return out


def parse_number(s: str) -> Decimal | None:
    """"1,234" "(1,234)" "−5.2" "– 8,722" "€1.2 billion" "12.5%".

    The whole string must be one number. A minus (hyphen, U+2212, en dash) may be followed by spaces,
    thin or no-break spaces included. A scale word is applied ("€1.2 billion" gives 1200000000).
    A percentage stays in percent ("12.5%" gives 12.5). None when s is not a single number.
    """
    t = unicodedata.normalize("NFKC", s).strip()
    mm = _TOKEN.fullmatch(t)
    if mm is None or bool(mm.group("open")) != bool(mm.group("close")):
        return None
    return _make(t, mm).value


def to_base(value: Decimal, scale: str) -> Decimal:
    """A value as reported, times its scale: (1150, "million") gives 1150000000."""
    try:
        return value * SCALES[scale]
    except KeyError:
        raise ValueError(f"unknown scale {scale!r}") from None


def numbers_in_text(text: str) -> list[tuple[str, Decimal]]:
    """Every number in text as (token as written, value with any scale word applied), skipping years
    and fiscal-year labels. Percentages stay in percent."""
    return [(t.raw, t.value) for t in scan_numbers(text)]


def half_up(x: Decimal | float, places: int = 1) -> str:
    """Display rounding, half away from zero. format(5.85, ".1f") gives "5.8", this gives "5.9"."""
    d = x if isinstance(x, Decimal) else Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    with localcontext() as ctx:
        ctx.prec = 60
        q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # never show "-0.0"
    return format(q, "f")


def _year(y: str, y2: str | None) -> int:
    """The year a label names. A split fiscal year ("2024/25") names its ending year."""
    first = int(y) + (2000 if len(y) == 2 else 0)
    if not y2:
        return first
    return int(y2) + (first // 100 * 100 if len(y2) == 2 else 0)


def period_mentions(text: str) -> list[tuple[int, int, int]]:
    """Every period mention as (year, start, end), in order: "FY2025", "fiscal year 2025",
    "Sep 30, 2025", "2025/26", or a bare 2025."""
    out: list[tuple[int, int, int]] = []
    taken: list[tuple[int, int]] = []
    for rx in (_FY, _MDY, _DMY_WORD, _DMY_NUM, _YEAR_SLASH, _YEAR):
        for mm in rx.finditer(text):
            s, e = mm.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            out.append((_year(mm.group("y"), mm.groupdict().get("y2")), s, e))
            taken.append((s, e))
    return sorted(out, key=lambda t: t[1])


def period_key(period: str) -> tuple[set[int], int | None]:
    """Years a period names, plus its quarter: "Q3 FY2026" gives ({2026}, 3), "2025-2030" ({2025, 2030}, None)."""
    q = _QUARTER.search(period)
    return {y for y, _, _ in period_mentions(period)}, (int(q.group(1)) if q else None)
