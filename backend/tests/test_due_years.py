"""Due years in period_match (B-fix-due).

A due year ("US$0.9 billion maturing until fiscal year 2021", "due in 2027", "2031 and thereafter") says when
an amount falls due, not which period it belongs to. period_match sets it aside the way it sets aside a
comparison base, so the balance date decides, and a claim whose period is the due year fails. A maturity
table whose columns are due years keeps period = the column's due year, and a range cell ("Fiscal years
2028 to 2030") is one column.

Quotes are real SHL text copied from the r2a run export (run_20261108_5zqdvb), labeled per test. The export
tests read backend/.data/r2a_export directly and skip when it is missing (it is never committed).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.axelrod.contract import m
from backend.axelrod.verify import checks, text

EXPORT = Path(__file__).resolve().parents[1] / ".data" / "r2a_export" / "state.json"

# Real SHL text, Annual Report FY2025, file page 177: the maturity analysis header and two rows.
P177_HEAD = ("Fiscal year \n2026 \nFiscal year \n2027 \nFiscal years \n2028 to 2030 \nFiscal years \n2031 and \n"
             "thereafter")
P177_ROW = "Non-derivative financial liabilities \n6,243 \n2,098 \n5,158 \n6,233"
P177_NIL_ROW = "Loans from banks \n94 \n‐ \n‐ \n‐"
# Real SHL text, Annual Report FY2018, file page 80 (the term loan paragraph).
P80_LOAN = "approximately US$ 0.9 billion maturing until fiscal year 2021"
P80_RATE = "Interest rates for these loans were 1.9 % \nfor the loans maturing until fiscal year 2021"
P80_OPEN = ("As of September 30, 2018, existing term loans were mainly denominated in U. S. dollars with "
            "approximately US$ 0.9 billion maturing until fiscal year 2021")
# Real SHL text, Annual Report FY2022, file page 72 (the Varian loan).
P72_850 = ("In fiscal year 2021, €850 million thereof were utilized as a loan and the rest of the financing commitment "
           "was canceled. This loan originally matured in fiscal year 2022 and has been extended to fiscal year 2029.")


def value(as_reported: str, period: str, *, unit: str = "EUR", scale: str = "million") -> m.Value:
    return m.Value(as_reported=as_reported, number=float(as_reported.replace(",", "")), unit=unit, scale=scale,
                   period=period)


def period(v: m.Value, ev: list[str], pc: list[str]) -> m.CheckResult:
    return checks._period_text(v, [text.normalize(q) for q in ev], [text.normalize(q) for q in pc])


# ---------------------------------------------------------------------------
# Due years in the value's own clause
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "US$ 0.9 billion maturing until fiscal year 2021", "loans due in 2027", "notes due 2030",
    "the loan matures in fiscal year 2026", "with a maturity in 2028", "repayable by 2030", "expiring in 2028",
    "redeemable in 2029", "for the loans until fiscal year 2021", "Fiscal years 2031 and thereafter",
    "This loan originally matured in fiscal year 2022",
])
def test_due_cues_mark_the_year_as_a_due_year(phrase):
    q = text.normalize(phrase)
    (y, s, e), = [mm for mm in checks.period_mentions(q) if mm[0] >= 2021]
    assert checks._due_phrase(q, s, e), phrase


@pytest.mark.parametrize("phrase", [
    "revenue in fiscal year 2025", "until 2030 the target holds", "growth due to China in 2025",
    "as of September 30, 2018", "Trade payables 2025",
])
def test_other_years_are_not_due_years(phrase):
    q = text.normalize(phrase)
    assert all(checks._due_phrase(q, s, e) is None for _, s, e in checks.period_mentions(q)), phrase


@pytest.mark.parametrize("ev, unit, scale", [(P80_LOAN, "USD", "billion"), (P80_RATE, "%", "one")])
def test_balance_date_decides_when_the_clause_names_only_a_due_year(ev, unit, scale):
    raw = "0.9" if unit == "USD" else "1.9"
    res = period(value(raw, "FY2018", unit=unit, scale=scale), [ev], ["September 30, 2018"])
    assert res.result == "pass", res.detail


@pytest.mark.parametrize("ev, unit, scale", [(P80_LOAN, "USD", "billion"), (P80_RATE, "%", "one")])
def test_a_claim_whose_period_is_the_due_year_fails(ev, unit, scale):
    """Before B-fix-due this passed: the due year in the value's own clause was read as its period."""
    raw = "0.9" if unit == "USD" else "1.9"
    res = period(value(raw, "FY2021", unit=unit, scale=scale), [ev], ["September 30, 2018"])
    assert res.result == "fail"
    assert "due year" in res.detail and "2021" in res.detail, res.detail


def test_a_due_year_claim_fails_even_when_the_header_names_that_year():
    """Real S-012 p72: the cited header paragraph happens to name fiscal year 2023 too."""
    pc = "Up to and including fiscal year 2022, the loan was subject to variable interest rates. From fiscal year 2023"
    ev = "US$1.2 billion maturing in fiscal year 2023 (contractual interest rate: 0.6%),"
    assert period(value("1.2", "FY2022", unit="USD", scale="billion"), [ev], [pc]).result == "pass"
    res = period(value("1.2", "FY2023", unit="USD", scale="billion"), [ev], [pc])
    assert res.result == "fail" and "due year" in res.detail, res.detail


def test_without_a_header_the_evidence_balance_date_decides():
    assert period(value("0.9", "FY2018", unit="USD", scale="billion"), [P80_OPEN], []).result == "pass"
    res = period(value("0.9", "FY2021", unit="USD", scale="billion"), [P80_OPEN], [])
    assert res.result == "fail" and "due year" in res.detail, res.detail


def test_an_original_maturity_never_sets_the_period():
    """Real S-012 p72 (D-517 in r2a): FY2022 was this loan's original due year, and passed before B-fix-due."""
    res = period(value("850", "FY2022"), [P72_850], [])
    assert res.result == "fail" and "due year" in res.detail, res.detail


def test_a_table_row_with_a_due_year_label_keeps_its_column_check():
    """Simulated row whose label names a due year, under a balance-date header."""
    row, head = "Bonds maturing 2027 500 450", "Sep 30, 2025 Sep 30, 2024"
    assert period(value("500", "FY2025"), [row], [head]).result == "pass"
    assert period(value("450", "FY2024"), [row], [head]).result == "pass"
    assert period(value("500", "FY2024"), [row], [head]).result == "fail"
    res = period(value("500", "FY2027"), [row], [head])
    assert res.result == "fail" and "due year" in res.detail, res.detail


# ---------------------------------------------------------------------------
# Range cells in a header
# ---------------------------------------------------------------------------

def test_a_range_cell_is_one_column():
    assert checks._ordered_years([text.normalize(P177_HEAD)]) == [(2026,), (2027,), (2028, 2030), (2031,)]
    assert checks._ordered_years(["2028-2030"]) == [(2028, 2030)]
    assert checks._ordered_years(["FY2028–FY2030 FY2031"]) == [(2028, 2030), (2031,)]
    assert checks._ordered_years(["2025 2024"]) == [(2025,), (2024,)]
    assert checks._ordered_years(["2025 - 2024"]) == [(2025,), (2024,)]  # descending: two columns
    assert checks._ordered_years(["2025", "2025 2024"]) == [(2025,), (2024,)]


@pytest.mark.parametrize("as_reported, period_label", [
    ("6,243", "FY2026"), ("2,098", "FY2027"), ("5,158", "FY2028-FY2030"), ("6,233", "FY2031+"),
])
def test_due_year_columns_pass_in_their_own_column(as_reported, period_label):
    res = period(value(as_reported, period_label), [P177_ROW], [P177_HEAD])
    assert res.result == "pass" and "column" in res.detail, res.detail


@pytest.mark.parametrize("as_reported, period_label, shown", [
    ("2,098", "FY2026", "2027"), ("6,243", "FY2027", "2026"), ("6,233", "FY2028-FY2030", "2031"),
    ("5,158", "FY2028", "2028 to 2030"), ("5,158", "FY2031+", "2028 to 2030"),
])
def test_a_wrong_column_claim_fails(as_reported, period_label, shown):
    """Before B-fix-due these passed: the range cell made five header columns for four value cells."""
    res = period(value(as_reported, period_label), [P177_ROW], [P177_HEAD])
    assert res.result == "fail", res.detail
    assert f"labels {shown}, not {period_label}" in res.detail, res.detail


def test_nil_cells_line_up_with_the_range_header():
    assert period(value("94", "FY2026"), [P177_NIL_ROW], [P177_HEAD]).result == "pass"
    assert period(value("94", "FY2027"), [P177_NIL_ROW], [P177_HEAD]).result == "fail"


def test_a_header_line_inside_the_evidence_also_counts_ranges_once():
    ev = f"{P177_HEAD} \n{P177_ROW}"
    assert period(value("2,098", "FY2027"), [ev], []).result == "pass"
    assert period(value("2,098", "FY2026"), [ev], []).result == "fail"


def test_a_balance_date_claim_on_a_due_year_table_still_fails():
    res = period(value("2,098", "FY2025"), [P177_ROW], [P177_HEAD])
    assert res.result == "fail" and "does not mention FY2025" in res.detail, res.detail


# ---------------------------------------------------------------------------
# Real items from the r2a export, checked end to end
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def export():
    if not EXPORT.exists():
        pytest.skip("r2a export not present (backend/.data/r2a_export is never committed)")
    raw = json.loads(EXPORT.read_text(encoding="utf-8"))
    items = raw["items"].values() if isinstance(raw["items"], dict) else raw["items"]
    data = {i["id"]: m.DataItem.model_validate(i) for i in items if i.get("kind") == "data"}
    sources = {s["source_id"]: m.Source.model_validate(s) for s in raw["sources"]}
    return data, {k: list(v) for k, v in raw["pages"].items()}, sources


def _pm(item: m.DataItem, pages, sources) -> m.CheckResult:
    return next(c for c in checks.check_data_item(item, pages, sources) if c.check == "period_match")


def _maturities(data: dict, source_id: str, page: int) -> list[m.DataItem]:
    out = []
    for it in data.values():
        ev = next((c for c in it.citations if c.role == "evidence"), None)
        if (it.source_id == source_id and it.value is not None and ev is not None
                and isinstance(ev.locator, m.TextLocator) and ev.locator.page == page and "matur" in ev.quote):
            out.append(it)
    return sorted(out, key=lambda i: i.id)


@pytest.mark.parametrize("source_id, page, balance, count", [("S-014", 80, "FY2018", 8), ("S-012", 72, "FY2022", 7)])
def test_real_balance_date_items_pass(export, source_id, page, balance, count):
    data, pages, sources = export
    items = [it for it in _maturities(data, source_id, page) if it.value.period == balance and it.id != "D-517"]
    assert len(items) >= count  # the term loan rows, plus a "current maturities" table row that passed already
    for it in items:
        res = _pm(it, pages, sources)
        assert res.result == "pass", (it.id, res.detail)


@pytest.mark.parametrize("item_id, due", [("D-477", "FY2021"), ("D-513", "FY2023"), ("D-522", "FY2023"),
                                          ("D-546", "FY2041")])
def test_real_items_claimed_at_their_due_year_fail(export, item_id, due):
    data, pages, sources = export
    it = data[item_id]
    claim = it.model_copy(update={"value": it.value.model_copy(update={"period": due})})
    res = _pm(claim, pages, sources)
    assert res.result == "fail" and "due year" in res.detail, res.detail


def test_real_maturity_table_wrong_column_fails(export):
    """S-001 p177, D-220: €2,098 million sits in the fiscal year 2027 column."""
    data, pages, sources = export
    it = data["D-220"]
    assert _pm(it, pages, sources).result == "pass"
    wrong = it.model_copy(update={"value": it.value.model_copy(update={"period": "FY2026"})})
    res = _pm(wrong, pages, sources)
    assert res.result == "fail" and "labels 2027, not FY2026" in res.detail, res.detail
