"""Table layouts the extract stage's header finder must read (B-fix-s015).

- Philips prints a statement's year header after its rows ("Sales ... Net income ... Note 2025 2024 2023"),
  and its unit header ends a title line without brackets ("Consolidated balance sheets in millions of EUR").
  In the pre-R2 run (run_20261108_5zqdvb) 25 of 47 failed data items came from that layout (S-015).
- A maturity's due year ("US$0.9 billion maturing until fiscal year 2021") is never a period anchor. The
  period context is the balance date ("As of September 30, 2018"). Since B-fix-due the checker sets a due
  year in the value's own clause aside, so the balance date decides and such an item passes.

Page texts are short excerpts of real annual reports, copied from the r2a run export (labeled per
fixture). The extractor answers are the recorded live answers of that run, replayed by a fake extractor.
"""
from __future__ import annotations

import pytest

from backend.axelrod import bedrock
from backend.axelrod.agents import extractor as ex
from backend.axelrod.agents.base import register_fake
from backend.axelrod.contract import m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import extract as st
from backend.axelrod.store import Store

# Real Philips text: Annual Report 2025, file page 92, the income statement and the EPS block, contiguous.
PHILIPS_INCOME_P92 = (
    "Consolidated statements of income\nPhilips Group\nConsolidated statements of income in millions of EUR \n"
    "for the year ended December 31,\nSales\n6\n17,834\n18,021\n18,169\nCost of sales\n(9,776)\n(10,248)\n(10,721)\n"
    "Gross margin\n8,058\n7,773\n7,448\nSelling expenses\n(4,342)\n(4,486)\n(4,524)\nGeneral and administrative "
    "expenses\n(628)\n(582)\n(608)\nResearch and development expenses\n(1,700)\n(1,747)\n(1,890)\nOther business "
    "income\n6\n87\n590\n112\nOther business expenses\n6\n(51)\n(1,019)\n(652)\nIncome from operations\n6\n1,424\n"
    "529\n(115)\nFinancial income\n7\n113\n105\n63\nFinancial expenses\n7\n(346)\n(387)\n(376)\nResults of "
    "associates\n(9)\n(124)\n(98)\nIncome before taxes\n1,182\n123\n(526)\nIncome tax (expense) benefit\n8\n(282)\n"
    "(963)\n73\nIncome from continuing operations\n901\n(840)\n(454)\nDiscontinued operations, net of income "
    "taxes\n3\n(4)\n142\n(10)\nNet income\n897\n(698)\n(463)\nAttribution of net income:\nNet income attributable "
    "to shareholders of Koninklijke Philips N.V.\n895\n(702)\n(466)\nNet income attributable to non-controlling "
    "interests\n1\n3\n2\nNote\n2025\n2024\n2023\n"
    "Philips Group\nEarnings per common share attributable to shareholders of Koninklijke Philips N.V. in EUR \n"
    "for the year ended December 31,\nBasic earnings per common share\nIncome from continuing operations\n0.95\n"
    "(0.90)\n(0.48)\nNet income\n0.94\n(0.75)\n(0.49)\nDiluted earnings per common share\nIncome from continuing "
    "operations\n0.93\n(0.90)\n(0.48)\nNet income\n0.93\n(0.75)\n(0.49)\n2025\n2024\n2023\n")
# Real Philips text: Annual Report 2025, file page 93, the balance sheet. Its two blocks each end with
# "Note 2025 2024". Rows in the middle of each block are left out, the rest is as printed.
PHILIPS_BALANCE_P93 = (
    "Consolidated balance sheets\nPhilips Group\nConsolidated balance sheets in millions of EUR\nas of December 31,\n"
    "Non-current assets\nProperty, plant and equipment\n2\n10\n2,217\n2,452\nGoodwill\n2\n11\n9,271\n10,383\n"
    "Total non-current assets\n17,012\n18,955\nTotal assets\n26,944\n28,976\nNote\n2025\n2024\n"
    "Equity\nShareholders’ equity\n17\n10,957\n12,006\nNon-controlling interests\n17\n32\n37\nGroup equity\n"
    "10,990\n12,043\nTotal liabilities and group equity\n26,944\n28,976\nNote\n2025\n2024\n"
    "Amounts may not add up due to rounding.\n")
# Real SHL text: Annual Report FY2018, file page 80. The debt table's head and its last row, then the term
# loan paragraph as printed (rows in the middle of the table are left out).
SHL_DEBT_P80 = (
    "Note\xa016  Debt\nSept 30,\n( in millions of € )\n2018\n2017\nShort-term debt and current "
    "maturities  \nof long-term debt\n57\n55\nTotal debt\n4,707\n11,016\n"
    "In addition, the \xadSiemens Group provided term loans with \xadvarious \nmaturities and in various "
    "currencies. As of September\xa030,\xa02018, \nexisting term loans were mainly denominated in U. S. dollars "
    "with \napproximately US$ 0.9 billion maturing until fiscal year\xa02021 ( Sep-\ntember\xa030, 2017: "
    "US$ 2.5 billion ), approximately US$ 0.7 billion \nmaturing in fiscal year\xa02023 "
    "( September\xa030, 2017: US$ 0.7 bil-\nlion ), approximately US$ 1.7 billion maturing in "
    "fiscal year\xa02027 \n( September\xa0 30, 2017: US$ 1.7 billion ) and approximately \nUS$ 1.0"
    " billion maturing in fiscal year\xa0 2046 ( September\xa0 30, \n2017: US$ 1.0 billion ). "
    "Interest rates for these loans were 1.9 % \nfor the loans maturing until fiscal year\xa02021 ( September"
    "\xa030, \n2017: 1.5 % to 1.9 % ), 2.2 % for the loan maturing in fiscal \nyear\xa0 2023 "
    "( September\xa0 30, 2017: 2.2 % ), 2.5 % for the loan \n\xadmaturing in fiscal year\xa02027 "
    "( September\xa030, 2017: 2.5 % ) and \n3.4 % for the loan maturing in fiscal year\xa02046 "
    "( September\xa030, \n2017: 3.4 % ).\n")
# Real SHL text: Annual Report FY2025, file page 177, the maturity analysis (head and first row).
SHL_MATURITY_P177 = (
    "Maturity analysis as of Sept 30, 2025 \n \n \n \n \n(in millions of €) \nFiscal year \n2026 \nFiscal year \n"
    "2027 \nFiscal years \n2028 to 2030 \nFiscal years \n2031 and \nthereafter \nNon-derivative financial "
    "liabilities \n6,243 \n2,098 \n5,158 \n6,233 \nThereof: \n \n \n \n \nLoans from banks \n94 \n‐ \n‐ \n‐ \n")

NEEDS = [m.DataNeed(need_id="N-02", label="Peer returns", dimension="competition", question="Peer ROTIC inputs?",
                    data_types=["financial_filing"], criteria=["rotic_vs_peers"], rationale="test"),
         m.DataNeed(need_id="N-05", label="Debt", dimension="financials", question="Debt and maturities?",
                    data_types=["financial_filing"], criteria=["leverage"], rationale="test")]


@pytest.fixture(autouse=True)
def _restore_fakes():
    saved = dict(bedrock._FAKES)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


def make_ctx(settings, pages: list[str], *, title: str) -> RunContext:
    """One approved annual report with the given pages, the real Store, RunContext and Gateway."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
    state.run.stage = "extract"
    state.needs = list(NEEDS)
    src = m.Source(source_id="S-915", title=title, url="https://example.com/S-915", domain="example.com",
                   kind="annual_report", data_types=["financial_filing"], access=m.SourceAccess(), tier="priority",
                   rank=1, scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data",
                   need_ids=["N-02", "N-05"], criteria=["rotic_vs_peers", "leverage"],
                   vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved", status="fetched",
                   doc_ids=["DOC-915"])
    doc = m.Document(doc_id="DOC-915", source_id="S-915", title=title, url=src.url, content_type="pdf",
                     paginated=True, pages=len(pages), sha256="0" * 64, bytes=sum(len(p) for p in pages),
                     fetched_at=now_iso(), vault_paths=m.DocVaultPaths(raw="a.raw", text="a.md", meta="a.json"),
                     scan=m.ScanResult(scanner="none", verdict="skipped"))
    state.sources.append(src)
    state.documents.append(doc)
    state.pages["DOC-915"] = list(pages)
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    return ctx


def answers(*items: dict):
    def fn(system: str, user: str, schema: dict) -> dict:
        return {"items": [dict(i) for i in items]}
    return fn


def row(quote: str, value: str, period: str, statement: str, *, period_quote: str, unit_quote: str,
        scale: str = "million", unit: str = "EUR") -> dict:
    """One extractor answer, shaped like the recorded tool input."""
    return {"metric": "", "quote": quote, "value_as_reported": value, "period": period, "unit": unit,
            "unit_quote": unit_quote, "period_quote": period_quote, "scale": scale, "statement": statement,
            "need_ids": ["N-02"]}


async def extracted(ctx: RunContext, *items: dict) -> list[m.DataItem]:
    register_fake(ex.TOOL, answers(*items))
    await st.run(ctx)
    return sorted(ctx.state.data_items(), key=lambda i: i.id)


def cit(it: m.DataItem, role: str) -> m.Citation | None:
    return next((c for c in it.citations if c.role == role), None)


def bad(it: m.DataItem) -> list[str]:
    return [f"{c.check}: {c.detail}" for c in it.verification.checks if c.result != "pass"]


def exact(it: m.DataItem, page: str) -> bool:
    """Every text citation's quote is the page's exact characters at its span (B-fix-cite's invariant)."""
    return all(c.quote == page[c.locator.char_start:c.locator.char_end] for c in it.citations)


# ---------------------------------------------------------------------------
# Headers printed after the rows (Philips)
# ---------------------------------------------------------------------------


async def test_income_statement_header_printed_after_the_rows(settings):
    """The live extractor joined two lines into a header the page never prints ("for the year ended
    December 31, 2025 2024 2023"), so every period_context failed quote_exact. Code now cites the year
    header printed after the block, and the column check reads each year (recorded answers, real text)."""
    joined = "for the year ended December 31, 2025 2024 2023"
    sales = "Sales 6 17,834 18,021 18,169"
    items = await extracted(
        make_ctx(settings, [PHILIPS_INCOME_P92], title="Philips Annual Report 2025 (peer)"),
        row(sales, "17,834", "2025", "Sales were EUR 17,834 million in 2025.", period_quote=joined,
            unit_quote="in millions of EUR"),
        row(sales, "18,021", "2024", "Sales were EUR 18,021 million in 2024.", period_quote=joined,
            unit_quote="in millions of EUR"),
        row("Income before taxes 1,182 123 (526)", "(526)", "2023", "Income before taxes was EUR 526 million "
            "loss in 2023.", period_quote=joined, unit_quote="in millions of EUR"),
        row("Net income 0.94 (0.75) (0.49)", "0.94", "2025", "Basic earnings per share from net income were "
            "EUR 0.94 in 2025.", period_quote="2025 2024 2023", unit_quote="in EUR", scale="one"),
        # wrong claim: 18,021 sits in the 2024 column
        row(sales, "18,021", "2025", "Sales were EUR 18,021 million in 2025.", period_quote=joined,
            unit_quote="in millions of EUR"),
    )
    s25, s24, ibt, eps, wrong = items
    first_header = PHILIPS_INCOME_P92.index("2025\n2024\n2023")
    eps_header = PHILIPS_INCOME_P92.rindex("2025\n2024\n2023")
    for it in (s25, s24, ibt):
        assert it.verification.status == "pass", (it.statement, bad(it))
        pc = cit(it, "period_context")
        assert (pc.quote, pc.locator.char_start) == ("2025\n2024\n2023", first_header)
        assert cit(it, "unit_context").quote == "in millions of EUR"
        assert "that year's column" in next(c.detail for c in it.verification.checks if c.check == "period_match")
        assert exact(it, PHILIPS_INCOME_P92)
    assert eps.verification.status == "pass", bad(eps)
    assert cit(eps, "period_context").locator.char_start == eps_header   # the EPS block's own header
    assert wrong.verification.status == "fail"
    assert "column" in next(c.detail for c in wrong.verification.checks if c.check == "period_match")


async def test_each_balance_sheet_block_takes_the_header_after_it(settings):
    """Two blocks, each followed by "Note 2025 2024". A row in the second block takes the header after it,
    not the first block's header above it, even when the agent quoted that one (recorded answers, real text)."""
    items = await extracted(
        make_ctx(settings, [PHILIPS_BALANCE_P93], title="Philips Annual Report 2025 (peer)"),
        row("Property, plant and equipment 2 10 2,217 2,452", "2,217", "2025", "Property, plant and equipment "
            "was EUR 2,217 million as of December 31, 2025.", period_quote="as of December 31, Note 2025 2024",
            unit_quote="(in millions of EUR)"),
        row("Shareholders’ equity 17 10,957 12,006", "10,957", "2025", "Shareholders' equity was EUR 10,957 "
            "million as of December 31, 2025.", period_quote="Note 2025 2024", unit_quote="in millions of EUR"),
        row("Goodwill 2 11 9,271 10,383", "10,383", "2025", "Goodwill was EUR 10,383 million as of December 31, "
            "2025.", period_quote="2025 2024", unit_quote="in millions of EUR"),
    )
    ppe, equity, wrong = items
    first = PHILIPS_BALANCE_P93.index("2025\n2024")
    second = PHILIPS_BALANCE_P93.rindex("2025\n2024")
    assert ppe.verification.status == "pass", bad(ppe)
    assert cit(ppe, "period_context").locator.char_start == first
    assert cit(ppe, "unit_context").quote == "in millions of EUR"   # code's own, not the agent's "(...)"
    assert equity.verification.status == "pass", bad(equity)
    assert cit(equity, "period_context").locator.char_start == second
    for it in (ppe, equity):
        assert exact(it, PHILIPS_BALANCE_P93)
    assert wrong.verification.status == "fail"
    assert "column" in next(c.detail for c in wrong.verification.checks if c.check == "period_match")


async def test_header_first_tables_keep_the_header_above(settings):
    """SHL prints the year header right after the unit header, so nothing changes there (real SHL text)."""
    (it,) = await extracted(
        make_ctx(settings, [SHL_DEBT_P80], title="Siemens Healthineers Annual Report FY2018"),
        row("Total debt 4,707 11,016", "4,707", "FY2018", "Total debt was €4,707 million in fiscal year 2018.",
            period_quote="", unit_quote=""))
    assert it.verification.status == "pass", bad(it)
    assert cit(it, "period_context").quote == "2018\n2017"
    assert st._trailing_header(SHL_DEBT_P80, cit(it, "evidence")) is None


def test_a_header_after_the_rows_is_never_borrowed_from_another_table():
    """Simulated pages: the next table's header (after its own unit line) and a header behind another
    period line are not this row's header."""
    nxt = ("Table A in millions of EUR\nfor the year ended December 31,\nRevenue\n100\n90\n"
           "Table B in millions of EUR\nNote\n2025\n2024\nCost\n(50)\n(40)\n")
    split = "Table A in millions of EUR\nRevenue\n100\n90\nOutlook for 2026\nNote\n2025\n2024\n"
    own = "Table A in millions of EUR\nRevenue\n100\n90\nCost\n(50)\n(40)\nNote\n2025\n2024\n"
    for page, expected in ((nxt, None), (split, None), (own, own.index("2025\n2024"))):
        ev = st.cite_on_page("DOC-1", [page], 1, "Revenue 100 90", "evidence")
        got = st._trailing_header(page, ev)
        assert (got[0] if got else None) == expected, page


def test_unit_headers_without_brackets():
    page = ("Consolidated balance sheets in millions of EUR\n(in millions of €)\nWe sold devices in millions of "
            "units.\nAmounts in EUR millions\nCosts in millions of EUR rose\n")
    quotes = [page[s:e] for s, e in st._unit_spans(page)]
    assert quotes == ["in millions of EUR", "(in millions of €)", "Amounts in EUR millions"]
    hair = "Sept 30,\n( in millions of € )\n2018\n"   # real SHL FY2018 text: hair spaces inside the brackets
    assert [hair[s:e] for s, e in st._unit_spans(hair)] == ["( in millions of € )"]


# ---------------------------------------------------------------------------
# Maturities: the due year is not the period
# ---------------------------------------------------------------------------


def test_due_years_are_never_period_anchors():
    assert st._anchors("US$0.7 billion maturing in fiscal year 2023", heading=False) == []
    assert st._anchors("loans due in 2027 and others", heading=True) == []
    assert st._anchors("Fiscal years 2031 and thereafter", heading=True) == []
    assert [y for y, _, _ in st._anchors("As of September 30, 2018, loans maturing in fiscal year 2021",
                                         heading=False)] == [2018]


async def test_maturity_cites_the_balance_date_and_passes_on_it(settings):
    """The live extractor claimed FY2018 for term loans "maturing until fiscal year 2021" (recorded answers,
    real SHL text). Code cites the balance date "September 30, 2018", not the table's "2018 2017", a due year
    or a "(September 30, 2017: ...)" comparative. period_match sets the due year in the value's own clause
    aside (B-fix-due), so the balance date decides and the item passes."""
    loans = ("existing term loans were mainly denominated in U. S. dollars with approximately US$ 0.9 billion "
             "maturing until fiscal year 2021")
    items = await extracted(
        make_ctx(settings, [SHL_DEBT_P80], title="Siemens Healthineers Annual Report FY2018"),
        row(loans, "0.9", "FY2018", "Term loans of approximately US$0.9 billion were maturing until fiscal year "
            "2021 as of September 30, 2018.", period_quote="", unit_quote="", scale="billion", unit="USD"),
        row("2.2 % for the loan maturing in fiscal year  2023", "2.2", "FY2018", "Interest rate for the loan "
            "maturing in fiscal year 2023 was 2.2% as of September 30, 2018.", period_quote="", unit_quote="",
            scale="one", unit="%"),
    )
    balance = "September\xa030,\xa02018"
    for it in items:
        assert cit(it, "period_context").quote == balance, it.statement
        assert exact(it, SHL_DEBT_P80)
        assert it.verification.status == "pass", bad(it)
        pm = next(c for c in it.verification.checks if c.check == "period_match")
        assert pm.result == "pass", bad(it)


def test_a_table_row_with_a_due_year_keeps_its_table_header():
    """Simulated page: a one-line row whose label names a due year. The balance-date search is for prose,
    so the row takes its column header, never one date out of it."""
    page = "Debt (simulated)\nSep 30, 2025 Sep 30, 2024\nBonds maturing 2027 500 450\n"
    ev = st.cite_on_page("DOC-1", [page], 1, "Bonds maturing 2027 500 450", "evidence")
    raw = ex.ExtractedItem(statement="Bonds due in 2027 were 500 at Sep 30, 2025.", quote=ev.quote,
                           value_as_reported="500", unit="count", period="FY2025")
    value = st.make_value(raw)
    assert st._balance_date("DOC-1", page, ev, ev.quote, value) is None
    (pc,) = st.context_citations("DOC-1", [page], ev, raw, value)
    assert pc.quote == "Sep 30, 2025 Sep 30, 2024"


async def test_due_year_tables_keep_their_column_check(settings):
    """A maturity table whose columns are due years keeps its due-year header, so a claim is checked against
    the column it sits in, and a balance-date claim on it fails (real SHL text, S-001 page 177, with the
    header the live extractor quoted)."""
    q = "Non-derivative financial liabilities 6,243 2,098 5,158 6,233"
    head = "Fiscal year 2026 Fiscal year 2027 Fiscal years 2028 to 2030 Fiscal years 2031 and thereafter"
    items = await extracted(
        make_ctx(settings, [SHL_MATURITY_P177], title="Siemens Healthineers Annual Report FY2025"),
        row(q, "2,098", "FY2027", "Non-derivative financial liabilities of €2,098 million are due in fiscal year "
            "2027.", period_quote=head, unit_quote="(in millions of €)"),
        row(q, "2,098", "FY2025", "Non-derivative financial liabilities were €2,098 million as of September 30, "
            "2025.", period_quote=head, unit_quote="(in millions of €)"),
    )
    # Range cells such as "2028 to 2030" count as one column (B-fix-due), so this header aligns its rows.
    due, balance_claim = items
    assert due.verification.status == "pass", bad(due)
    assert cit(due, "period_context").quote.startswith("Fiscal year \n2026")
    assert balance_claim.verification.status == "fail"
    assert any(c.check == "period_match" and c.result == "fail" for c in balance_claim.verification.checks)
