"""Planted errors: test the checker before trusting it (BACKEND.md section 5).

Simulated data: tests/data/planted/ holds three statement pages of a fictional company in the SHL style
("(in millions of €)" headers, comma thousands, negatives in parentheses). Each case plants one error
an extractor, drafter or summarizer could make, next to a clean twin. Every planted item must fail its
target check and every clean twin must pass all code checks. Run with -s to see the summary line.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.verify import checks, compute, text

DATA = Path(__file__).parent / "data" / "planted"
PAGES = [(DATA / f"annual_report_p{i}.txt").read_text(encoding="utf-8") for i in (1, 2, 3)]
DOC = "DOC-901"
PAGES_BY_DOC = {DOC: PAGES}
AT = "2026-11-08T03:00:00.000Z"
SOURCES = {"S-901": m.Source(
    source_id="S-901", title="Annual report FY2025 (simulated)", url="https://example.com/ar2025.pdf",
    domain="example.com", kind="annual_report", data_types=["financial_filing"], access=m.SourceAccess(),
    tier="priority", rank=1, scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data",
    need_ids=["N-01"], criteria=["leverage"], vault_folder="01-Annual-Reports", proposed_by="planner",
    approval="approved", status="extracted", doc_ids=[DOC])}

INCOME_UNIT = "(in millions of €)"
INCOME_HEADER = "Fiscal year Note 2025 2024"
BALANCE_HEADER = "Sep 30, 2025 Sep 30, 2024"


# ---------------------------------------------------------------------------
# Builders. Offsets come from code (text.locate), never from the case.
# ---------------------------------------------------------------------------


def cite(quote: str, role: str = "evidence", page: int = 1) -> m.Citation:
    hit = text.locate(PAGES, quote, page)
    p, s, e = hit if hit else (page, 0, 0)  # a planted quote is not in the source: keep the hint page
    return m.Citation(doc_id=DOC, locator=m.TextLocator(page=p, char_start=s, char_end=e), quote=quote, role=role)


def eur_m(number: float, as_reported: str, period: str = "FY2025", scale: str = "million") -> m.Value:
    return m.Value(number=number, as_reported=as_reported, unit="EUR", scale=scale, period=period)


def pending() -> m.Verification:
    return m.Verification(status="pending")


def data(iid: str, statement: str, quote: str, value: m.Value | None = None, *, page: int = 1,
         unit: str | None = None, period: str | None = None) -> m.DataItem:
    cits = [cite(quote, page=page)]
    if unit:
        cits.append(cite(unit, "unit_context", page))
    if period:
        cits.append(cite(period, "period_context", page))
    return m.DataItem(id=iid, category="financial_strength", criteria=["leverage"], need_id="N-01",
                      statement=statement, value=value, citations=cits, source_id="S-901", source_tier="priority",
                      extracted_by="agent", agent_id="extractor-01", verification=pending())


def comp(name: str, expr: str, display: str, env: dict) -> m.Computation:
    """Computations are evaluated by code. The display is the case's claim about how it rounds."""
    val = compute.safe_eval(expr, env)
    env[name] = val
    return m.Computation(name=name, expr=expr, result=float(val), display=display)


def conclusion(cid: str, text_: str, data_ids: list[str], comps: list[tuple[str, str, str]] = (),
               criteria: tuple[str, ...] = ("leverage",)) -> m.Conclusion:
    env = {d: checks.base_value(BASE[d]) for d in data_ids}
    return m.Conclusion(id=cid, category="financial_strength", criteria=list(criteria), text=text_,
                        data_ids=data_ids, computations=[comp(n, e, d, env) for n, e, d in comps],
                        agent_id="drafter-01", verification=pending())


def verified(item):
    """The item with its verification set from the code checks, as the verify stage would do."""
    res = checks.check_item(item, items=ITEMS, sources=SOURCES, pages_by_doc=PAGES_BY_DOC)
    item = item.model_copy(update={"verification": m.Verification(status=checks.status_from(res), checks=res)})
    ITEMS[item.id] = item
    assert item.verification.status == "pass", (item.id, [c.detail for c in res if c.result != "pass"])
    return item


# Verified data the conclusion, score, chart and sentence cases cite.
ITEMS: dict[str, m.Item] = {}
BASE: dict[str, m.DataItem] = {}
for _d in [
    data("D-901", "Revenue was €18,742 million in fiscal year 2025.", "Revenue 4 18,742 17,906",
         eur_m(18742, "18,742"), unit=INCOME_UNIT, period=INCOME_HEADER),
    data("D-902", "Revenue was €17,906 million in fiscal year 2024.", "Revenue 4 18,742 17,906",
         eur_m(17906, "17,906", "FY2024"), unit=INCOME_UNIT, period=INCOME_HEADER),
    data("D-903", "Short-term financial debt was €1,044 million at September 30, 2025.",
         "Short-term financial debt and current maturities of long-term financial debt 1,044 2,187",
         eur_m(1044, "1,044"), page=2, unit=INCOME_UNIT, period=BALANCE_HEADER),
    data("D-904", "Long-term financial debt was €11,592 million at September 30, 2025.",
         "Long-term financial debt 11,592 12,047", eur_m(11592, "11,592"), page=2, unit=INCOME_UNIT,
         period=BALANCE_HEADER),
    data("D-905", "Free cash flow was €2,160 million in fiscal year 2025.",
         "Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million).",
         eur_m(2160, "2,160"), page=3),
    data("D-906", "Comparable revenue grew 5.1% in fiscal year 2025.",
         "On a comparable basis, revenue grew 5.1% in fiscal year 2025.",
         m.Value(number=5.1, as_reported="5.1%", unit="%", scale="one", period="FY2025"), page=3),
]:
    BASE[_d.id] = verified(_d)

C_COVERAGE = verified(conclusion(
    "C-902", "Five years of free cash flow at the fiscal year 2025 rate would cover long-term financial debt of "
             "€11,592 million 0.9 times.", ["D-905", "D-904"], [("coverage", "(D-905 * 5) / D-904", "0.9")],
    criteria=("liquidity",)))


def score(eid: str, s: int, text_: str) -> m.Extrapolation:
    return m.Extrapolation(id=eid, category="financial_strength", criterion="liquidity", score=s, text=text_,
                           conclusion_ids=["C-902"], confidence="medium", agent_id="drafter-01",
                           rule=m.ScoreRule(type="threshold", plus_one="> 2x", minus_one="< 1x", input="C-902:coverage"),
                           verification=pending())


def chart(gid: str, y_2025: float) -> m.Chart:
    pts = [m.ChartPoint(x="FY2024", y=17906.0, ref="D-902"), m.ChartPoint(x="FY2025", y=y_2025, ref="D-901")]
    return m.Chart(id=gid, category="compounder", title="Revenue by fiscal year", chart_type="bar", unit="EUR",
                   scale="million", series=[m.ChartSeries(name="Revenue", points=pts)], refs=["D-902", "D-901"],
                   verification=pending())


def sentence(sid: str, text_: str, mode: str, refs: list[str]) -> m.SummarySentence:
    return m.SummarySentence(id=sid, text=text_, mode=mode, derived_from=refs, verification=pending())


def run_item(item) -> list[m.CheckResult]:
    return checks.check_item(item, items=ITEMS, sources=SOURCES, pages_by_doc=PAGES_BY_DOC, rubric=RUBRIC)


def run_sentence(s: m.SummarySentence) -> list[m.CheckResult]:
    return checks.check_sentence(s, ITEMS, [])


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass
class Case:
    name: str
    target: str
    clean: object
    planted: object
    run: Callable[[object], list[m.CheckResult]] = run_item


CASES = [
    Case("swapped digit", "quote_exact",
         data("D-911", "Revenue was €18,742 million in fiscal year 2025.", "Revenue 4 18,742 17,906",
              eur_m(18742, "18,742"), unit=INCOME_UNIT, period=INCOME_HEADER),
         data("D-911", "Revenue was €18,724 million in fiscal year 2025.", "Revenue 4 18,724 17,906",
              eur_m(18724, "18,724"), unit=INCOME_UNIT, period=INCOME_HEADER)),
    Case("flipped not", "quote_exact",
         data("D-912", "The Diagnostics segment did not reach its margin target in fiscal year 2025.",
              "The Diagnostics segment did not reach its margin target in fiscal year 2025.", page=3),
         data("D-912", "The Diagnostics segment reached its margin target in fiscal year 2025.",
              "The Diagnostics segment did reach its margin target in fiscal year 2025.", page=3)),
    Case("wrong year (prose)", "period_match",
         data("D-913", "Free cash flow was €2,160 million in fiscal year 2025.",
              "Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million).",
              eur_m(2160, "2,160"), page=3),
         data("D-913", "Free cash flow was €2,160 million in fiscal year 2024.",
              "Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million).",
              eur_m(2160, "2,160", "FY2024"), page=3)),
    Case("wrong year (table)", "period_match",
         data("D-914", "Net income was €1,894 million in fiscal year 2025.", "Net income 1,894 1,667",
              eur_m(1894, "1,894"), unit=INCOME_UNIT, period=INCOME_HEADER),
         data("D-914", "Net income was €1,894 million in fiscal year 2023.", "Net income 1,894 1,667",
              eur_m(1894, "1,894", "FY2023"), unit=INCOME_UNIT, period=INCOME_HEADER)),
    Case("right number, wrong column", "period_match",
         data("D-915", "Revenue was €17,906 million in fiscal year 2024.", "Revenue 4 18,742 17,906",
              eur_m(17906, "17,906", "FY2024"), unit=INCOME_UNIT, period=INCOME_HEADER),
         data("D-915", "Revenue was €17,906 million in fiscal year 2025.", "Revenue 4 18,742 17,906",
              eur_m(17906, "17,906", "FY2025"), unit=INCOME_UNIT, period=INCOME_HEADER)),
    Case("dropped minus", "value_in_quote",
         data("D-916", "Cost of sales was €10,233 million in fiscal year 2025.", "Cost of sales (10,233) (9,871)",
              eur_m(-10233, "(10,233)"), unit=INCOME_UNIT, period=INCOME_HEADER),
         data("D-916", "Cost of sales was €10,233 million in fiscal year 2025.", "Cost of sales (10,233) (9,871)",
              eur_m(10233, "10,233"), unit=INCOME_UNIT, period=INCOME_HEADER)),
    Case("wrong scale", "unit_context",
         data("D-917", "Net income was €1,894 million in fiscal year 2025.", "Net income 1,894 1,667",
              eur_m(1894, "1,894"), unit=INCOME_UNIT, period=INCOME_HEADER),
         data("D-917", "Net income was €1,894 thousand in fiscal year 2025.", "Net income 1,894 1,667",
              eur_m(1894, "1,894", scale="thousand"), unit=INCOME_UNIT, period=INCOME_HEADER)),
    # 12,636 / 2,160 is exactly 5.85: half up gives 5.9, Python's format() gives 5.8.
    Case("computation off by rounding", "computation",
         conclusion("C-911", "Total financial debt of €12,636 million was 5.9 times fiscal year 2025 free cash flow "
                             "of €2,160 million.", ["D-903", "D-904", "D-905"],
                    [("total_debt", "D-903 + D-904", "€12,636 million"), ("debt_to_fcf", "total_debt / D-905", "5.9")]),
         conclusion("C-911", "Total financial debt of €12,636 million was 5.8 times fiscal year 2025 free cash flow "
                             "of €2,160 million.", ["D-903", "D-904", "D-905"],
                    [("total_debt", "D-903 + D-904", "€12,636 million"),
                     ("debt_to_fcf", "total_debt / D-905", f"{5.85:.1f}")])),
    Case("number the data does not say", "numbers_traced",
         conclusion("C-912", "Comparable revenue grew 5.1% in fiscal year 2025.", ["D-906"]),
         conclusion("C-912", "Comparable revenue grew 5.4% in fiscal year 2025.", ["D-906"])),
    Case("score against the threshold", "threshold_rule",
         score("E-911", -1, "Liquidity scores −1 under the framework: free cash flow would cover long-term "
                            "financial debt 0.9 times, below the 1x threshold."),
         score("E-911", 1, "Liquidity scores +1 under the framework: free cash flow would cover long-term "
                           "financial debt 0.9 times.")),
    Case("copied sentence with one word changed", "copy_exact",
         sentence("H-911", "Revenue was €18,742 million in fiscal year 2025.", "copied", ["D-901"]),
         sentence("H-911", "Revenue reached €18,742 million in fiscal year 2025.", "copied", ["D-901"]),
         run_sentence),
    Case("chart point that does not match its item", "chart_values",
         chart("G-911", 18742.0), chart("G-911", 18724.0)),
    # "17.3% (2024: 16.8%)" under an agent-supplied period header that names fiscal year 2025: the
    # comparative's own "2024:" wins over the header, so 16.8% claimed for FY2025 fails (B-fix-checks).
    Case("comparative claimed as the current year", "period_match",
         data("D-918", "The adjusted EBIT margin was 17.3% in fiscal year 2025.",
              "The adjusted EBIT margin was 17.3% (2024: 16.8%).",
              m.Value(number=17.3, as_reported="17.3%", unit="%", scale="one", period="FY2025"), page=3,
              period="fiscal year 2025"),
         data("D-918", "The adjusted EBIT margin was 16.8% in fiscal year 2025.",
              "The adjusted EBIT margin was 17.3% (2024: 16.8%).",
              m.Value(number=16.8, as_reported="16.8%", unit="%", scale="one", period="FY2025"), page=3,
              period="fiscal year 2025")),
]

# B-fix-cite: a negative amount stated without its minus. "Financial income, net (148)" is not a cost line,
# so "€148 million" for D-907 (-148) fails numbers_traced, while "−€148 million" traces.
BASE["D-907"] = verified(data("D-907", "Financial income, net was −€148 million in fiscal year 2025.",
                              "Financial income, net (148) (171)", eur_m(-148, "(148)"), unit=INCOME_UNIT,
                              period=INCOME_HEADER))
CASES.append(Case("minus sign dropped in a conclusion", "numbers_traced",
                  conclusion("C-913", "Financial income, net was −€148 million in fiscal year 2025.", ["D-907"]),
                  conclusion("C-913", "Financial income, net was €148 million in fiscal year 2025.", ["D-907"])))


def _outcome(case: Case) -> tuple[bool, bool, str]:
    clean = case.run(case.clean)
    planted = case.run(case.planted)
    target = [c for c in planted if c.check == case.target]
    caught = bool(target) and target[0].result == "fail" and checks.status_from(planted) == "fail"
    clean_ok = checks.status_from(clean) == "pass"
    why = (f"clean: {[(c.check, c.detail) for c in clean if c.result != 'pass']}; "
           f"planted {case.target}: {[c.detail for c in target] or 'no result'}")
    return caught, clean_ok, why


def test_planted_errors_caught_and_clean_twins_pass() -> None:
    results = [(case, *_outcome(case)) for case in CASES]
    caught = sum(r[1] for r in results)
    clean = sum(r[2] for r in results)
    print(f"\nplanted: {caught}/{len(CASES)} caught, clean: {clean}/{len(CASES)} pass")
    bad = [f"{case.name}: {why}" for case, c, ok, why in results if not (c and ok)]
    assert not bad, "\n".join(bad)


def test_swapped_digit_points_at_the_real_span() -> None:
    res = run_item(CASES[0].planted)
    qe = res[0]
    assert qe.check == "quote_exact" and qe.result == "fail"
    loc = qe.closest_locator
    assert loc is not None and PAGES[loc.page - 1][loc.char_start:loc.char_end] == "Revenue 4 18,742 17,906"
    assert any(d.op != "equal" for d in qe.diff)
    assert "".join(d.source for d in qe.diff) == "Revenue 4 18,742 17,906"
    assert "".join(d.agent for d in qe.diff) == "Revenue 4 18,724 17,906"


def test_flipped_not_shows_the_missing_word() -> None:
    qe = run_item(CASES[1].planted)[0]
    assert qe.result == "fail"
    assert any(d.op == "insert" and d.source.strip() == "not" for d in qe.diff)


def test_wrong_column_names_the_column() -> None:
    pm = next(c for c in run_item(CASES[4].planted) if c.check == "period_match")
    assert pm.result == "fail" and "column 2" in pm.detail and "2024" in pm.detail


def test_rounding_case_is_the_float_format_trap() -> None:
    assert f"{5.85:.1f}" == "5.8"  # the bug the planted display copies
    detail = next(c for c in run_item(CASES[7].planted) if c.check == "computation").detail
    assert "5.9" in detail and "5.8" in detail
