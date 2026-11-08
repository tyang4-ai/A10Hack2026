"""Unit tests for backend/axelrod/verify: text matching, numbers, the safe evaluator and the code checks.

The last section runs the checks over the mock fixture (a fictional company), so they are known to
agree with a realistic, contract-valid report.
"""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from pydantic import TypeAdapter

from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.verify import checks, compute, numbers, text
from backend.axelrod.verify.numbers import half_up, numbers_in_text, parse_number, to_base

D = Decimal


# ---------------------------------------------------------------------------
# text: normalization and exact matching
# ---------------------------------------------------------------------------


def test_normalize_is_nfkc_plus_whitespace_only() -> None:
    assert text.normalize("ﬁscal year \n\t 2025") == "fiscal year 2025"  # ligature, NBSP, whitespace run
    assert text.normalize("１２３") == "123"  # full-width digits are compatibility characters
    assert text.normalize("’ “ ” – —") == "’ “ ” – —"  # quotes and dashes are left alone


def test_curly_quotes_and_dashes_do_not_match_straight_ones() -> None:
    page = "The company’s outlook – “cautiously positive” – is unchanged."
    assert text.find_quote(page, "The company’s outlook – “cautiously positive” –") is not None
    assert text.find_quote(page, "The company's outlook") is None            # straight apostrophe
    assert text.find_quote(page, '"cautiously positive"') is None            # straight double quotes
    assert text.find_quote(page, "outlook - “cautiously") is None            # hyphen for an en dash
    assert text.find_quote(page, "outlook — “cautiously") is None            # em dash for an en dash
    cit = m.Citation(doc_id="DOC-001", locator=m.TextLocator(page=1, char_start=0, char_end=10),
                     quote="The company's outlook - \"cautiously positive\"")
    qe = checks._quote_exact(cit, {"DOC-001": [page]})
    assert qe.result == "fail" and qe.closest_locator is not None


def test_find_quote_returns_offsets_into_the_original_text() -> None:
    page = "Header\n\nRevenue\n4    18,742 17,906\nCost"
    s, e = text.find_quote(page, "Revenue 4 18,742 17,906")
    assert page[s:e] == "Revenue\n4    18,742 17,906"
    assert text.find_quote(page, "evenue 4 18,742 17,90") is not None
    assert text.find_quote(page, "") is None


def test_find_quote_never_cuts_a_ligature() -> None:
    page = "the ﬁnal figure"
    assert text.find_quote(page, "final") == (4, 8)  # "ﬁ" is one character in the page
    assert text.find_quote(page, "inal") is None  # would start inside the ligature


def test_pdf_noise_bel_soft_hyphen_is_dropped_with_original_offsets() -> None:
    page = "Siemens Healthi­neers\u0007 reported a loss of – 8,722 million."
    assert text.normalize(page) == "Siemens Healthineers reported a loss of – 8,722 million."
    s, e = text.find_quote(page, "Healthineers reported")
    assert page[s:e] == "Healthi­neers\u0007 reported"
    assert text.find_quote("a\u0007b", "ab") == (0, 3)
    p, s, e = text.locate(["x", page], "loss of – 8,722", 1)
    assert p == 2 and page[s:e] == "loss of – 8,722"


def test_locate_tries_the_hint_first_then_other_pages() -> None:
    pages = ["Total debt 1,150", "Total debt 1,150", "Free cash flow 468"]
    assert text.locate(pages, "Total debt 1,150", 2)[0] == 2
    assert text.locate(pages, "Free cash flow 468", 1) == (3, 0, 18)
    assert text.locate(pages, "Total debt 1,510", 1) is None


def test_near_miss_gives_the_diff_and_the_real_span() -> None:
    page = "Senior notes due 2030 750\nTotal debt 1,150\n\nScheduled principal payments"
    diff, (s, e) = text.near_miss(page, "Total debt 1,510")
    assert page[s:e] == "Total debt 1,150"
    assert "".join(d.agent for d in diff) == "Total debt 1,510"
    assert "".join(d.source for d in diff) == "Total debt 1,150"
    assert text.changed_chars(diff) == 2


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("s,want", [
    ("1,234", D(1234)), ("(1,234)", D(-1234)), ("−5.2", D("-5.2")), ("€1.2 billion", D(1200000000)),
    ("12.5%", D("12.5")), ("EUR 3,118 million", D(3118000000)), ("$468 million", D(468000000)),
    ("€5m", D(5000000)), ("+1", D(1)), ("0.21", D("0.21")),
    ("– 8,722", D(-8722)), ("– 8,722", D(-8722)), ("- 8,722", D(-8722)),
    ("− 8,722", D(-8722)),
])
def test_parse_number(s: str, want: Decimal) -> None:
    assert parse_number(s) == want


@pytest.mark.parametrize("s", ["abc", "1,23", "(1,234", "", "5%-6%", "FY2025 revenue"])
def test_parse_number_rejects_non_numbers(s: str) -> None:
    assert parse_number(s) is None


def test_to_base_and_unknown_scale() -> None:
    assert to_base(D("1.5"), "million") == D(1500000)
    assert to_base(D(7), "one") == D(7)
    with pytest.raises(ValueError):
        to_base(D(1), "lakh")


def test_half_up_is_not_float_format() -> None:
    assert f"{5.85:.1f}" == "5.8"
    assert half_up(5.85) == "5.9"
    assert half_up(D("2.45")) == "2.5"
    assert half_up(0.125, 2) == "0.13"
    assert half_up(-5.85) == "-5.9"
    assert half_up(D("-0.04")) == "0.0"
    assert half_up(D("12636"), 0) == "12636"


def test_numbers_in_text_skips_years_labels_dates_and_ids() -> None:
    s = ("Revenue rose 5.1% to €18,742 million in fiscal year 2025 (FY2024: €17,906 million) at September 30, "
         "2025, per D-001, Form 10-K and IFRS 16, in Q3 and from 2024 to 2025/26.")
    assert numbers_in_text(s) == [("5.1%", D("5.1")), ("€18,742 million", D(18742000000)),
                                  ("€17,906 million", D(17906000000))]


def test_scan_numbers_reads_parentheses_and_flags_spaced_dashes() -> None:
    toks = numbers.scan_numbers("Cost of sales (10,233) – 9,871 and 5%–6%, 2024 – 2025")
    assert [(t.raw, t.number, t.sign_ambiguous) for t in toks] == [
        ("(10,233)", D(-10233), False), ("– 9,871", D(-9871), True), ("5%", D(5), False), ("6%", D(6), False)]


def test_period_mentions_and_keys() -> None:
    years = [y for y, _, _ in numbers.period_mentions(
        "Fiscal year 2025 2024. Sep 30, 2025. fiscal 2024/25, FY23 (2022: 4)")]
    assert years == [2025, 2024, 2025, 2025, 2023, 2022]
    assert numbers.period_key("Q3 FY2026") == ({2026}, 3)
    assert numbers.period_key("2025-2030") == ({2025, 2030}, None)
    assert numbers.period_key("2025-12-31") == ({2025}, None)
    assert numbers.period_key("TTM") == (set(), None)


# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------


def test_safe_eval_is_exact_decimal() -> None:
    env = {"D-903": D(1044e6), "D-904": D(11592e6), "D-905": D(2160e6)}
    assert compute.safe_eval("(D-903 + D-904) / D-905", env) == D("5.85")
    env["total"] = D(12636e6)
    assert compute.safe_eval("total / D-905 * 100 - -1", env) == D("586")
    assert compute.safe_eval("(D-905 * 5) / D-904", env).quantize(D("1e-20")) == D("0.93167701863354037267")
    assert compute.safe_eval("1 - 0.21", {}) == D("0.79")


def test_fiscal_year_label_before_a_comma() -> None:
    assert [y for y, _, _ in numbers.period_mentions("In FY2025, revenue rose; fiscal 2024/25, too.")] == [2025, 2025]
    assert numbers.numbers_in_text("In FY2025, revenue rose 4%.") == [("4%", D(4))]


@pytest.mark.parametrize("expr", ["D-001 ** 2", "abs(D-001)", "D-001.real", "D-002 + 1", "1 / 0", "D-001 % 2",
                                  "[1][0]", "1 +", "__import__('os')"])
def test_safe_eval_rejects(expr: str) -> None:
    with pytest.raises(compute.ComputeError):
        compute.safe_eval(expr, {"D-001": D(3)})


def test_ids_in() -> None:
    assert compute.ids_in("nopat / ((D-020 + D-021) / 2)") == ["D-020", "D-021"]


# ---------------------------------------------------------------------------
# checks: small hand-built items
# ---------------------------------------------------------------------------

PAGE = ("Consolidated statements of income\n(in millions of €, earnings per share in €)\nFiscal year 2025 2024\n"
        "Revenue 22,363 21,680\nBasic earnings per share 1.74 1.52\nCost of sales – 8,722 – 8,111")
PAGES = {"DOC-001": [PAGE], "DOC-002": ['{"facts": {"Rev": {"value": "22363000000", '
                                        '"dimensions": {"period": "2024-10-01T00:00:00/2025-10-01T00:00:00"}}}}']}


def _src(sid: str = "S-001", **kw) -> m.Source:
    base = dict(source_id=sid, title="t", url="https://example.com/x", domain="example.com", kind="annual_report",
                data_types=["financial_filing"], access=m.SourceAccess(), tier="priority", rank=1,
                scores=m.SourceScores(reliability=0.9, relevance=0.9), rationale="r", need_ids=[], criteria=[],
                vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved", status="extracted")
    base.update(kw)
    return m.Source(**base)


SOURCES = {"S-001": _src()}


def _cit(quote: str, role: str = "evidence", page: int = 1, doc: str = "DOC-001") -> m.Citation:
    p, s, e = text.locate(PAGES[doc], quote, page) or (page, 0, 0)
    return m.Citation(doc_id=doc, locator=m.TextLocator(page=p, char_start=s, char_end=e), quote=quote, role=role)


def _data(statement: str, cits: list[m.Citation], value: m.Value | None, iid: str = "D-001", **kw) -> m.DataItem:
    base = dict(id=iid, category="compounder", criteria=["secular_growth"], statement=statement, value=value,
                citations=cits, source_id="S-001", source_tier="priority", extracted_by="agent",
                verification=m.Verification(status="pending"))
    base.update(kw)
    return m.DataItem(**base)


def _by(res: list[m.CheckResult], name: str) -> m.CheckResult:
    return next(c for c in res if c.check == name)


def test_unit_context_accepts_the_shl_income_statement_header() -> None:
    header = "(in millions of €, earnings per share in €)"
    rev = _data("Revenue was €22,363 million in fiscal year 2025.",
                [_cit("Revenue 22,363 21,680"), _cit(header, "unit_context"), _cit("Fiscal year 2025 2024", "period_context")],
                m.Value(number=22363, as_reported="22,363", unit="EUR", scale="million", period="FY2025"))
    res = checks.check_data_item(rev, PAGES, SOURCES)
    assert checks.status_from(res) == "pass", [c.detail for c in res]
    wrong = rev.model_copy(update={"value": rev.value.model_copy(update={"scale": "billion"})})
    assert _by(checks.check_data_item(wrong, PAGES, SOURCES), "unit_context").result == "fail"
    eps = _data("Basic earnings per share were €1.74 in fiscal year 2025.",
                [_cit("Basic earnings per share 1.74 1.52"), _cit(header, "unit_context"),
                 _cit("Fiscal year 2025 2024", "period_context")],
                m.Value(number=1.74, as_reported="1.74", unit="EUR", scale="one", period="FY2025"))
    assert checks.status_from(checks.check_data_item(eps, PAGES, SOURCES)) == "pass"
    eps_m = eps.model_copy(update={"value": eps.value.model_copy(update={"scale": "million"})})
    assert _by(checks.check_data_item(eps_m, PAGES, SOURCES), "unit_context").result == "fail"


def test_unit_context_scale_and_currency() -> None:
    v = m.Value(number=22363, as_reported="22,363", unit="USD", scale="million", period="FY2025")
    item = _data("Revenue was $22,363 million.", [_cit("Revenue 22,363 21,680"), _cit("(in millions of €", "unit_context")], v)
    assert "USD" in _by(checks.check_data_item(item, PAGES, SOURCES), "unit_context").detail
    no_uc = _data("Revenue was 22,363.", [_cit("Revenue 22,363 21,680")], v.model_copy(update={"unit": "EUR"}))
    assert _by(checks.check_data_item(no_uc, PAGES, SOURCES), "unit_context").result == "fail"


def test_spaced_minus_from_shl_pdfs_passes_value_in_quote() -> None:
    v = m.Value(number=-8722, as_reported="– 8,722", unit="EUR", scale="million", period="FY2025")
    item = _data("Cost of sales was €8,722 million in fiscal year 2025.",
                 [_cit("Cost of sales – 8,722 – 8,111"),
                  _cit("(in millions of €, earnings per share in €)", "unit_context"),
                  _cit("Fiscal year 2025 2024", "period_context")], v)
    res = checks.check_data_item(item, PAGES, SOURCES)
    assert checks.status_from(res) == "pass", [c.detail for c in res if c.result != "pass"]
    wrong_col = item.model_copy(update={"value": v.model_copy(update={"number": -8111, "as_reported": "– 8,111"})})
    assert _by(checks.check_data_item(wrong_col, PAGES, SOURCES), "period_match").result == "fail"


def test_period_match_ignores_maturity_years_in_prose() -> None:
    page = ("We have a €500 million revolving credit facility that expires in 2028, with no amounts drawn as of "
            "September 30, 2025. Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million).")
    pages = {"DOC-001": [page]}

    def period(quote: str, number: float, as_rep: str, label: str) -> m.CheckResult:
        p, s, e = text.locate([page], quote, 1)
        cit = m.Citation(doc_id="DOC-001", locator=m.TextLocator(page=p, char_start=s, char_end=e), quote=quote)
        v = m.Value(number=number, as_reported=as_rep, unit="EUR", scale="million", period=label)
        return _by(checks.check_data_item(_data("s", [cit], v), pages, SOURCES), "period_match")

    rcf = "We have a €500 million revolving credit facility that expires in 2028, with no amounts drawn as of September 30, 2025."
    assert period(rcf, 500, "500", "FY2025").result == "pass"
    fcf = "Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million)."
    assert period(fcf, 1953, "1,953", "FY2024").result == "pass"
    assert period(fcf, 1953, "1,953", "FY2025").result == "fail"


def test_quote_on_another_page_passes_with_the_page_fixed() -> None:
    pages = {"DOC-001": ["Cover page", PAGE]}
    cit = m.Citation(doc_id="DOC-001", locator=m.TextLocator(page=1, char_start=0, char_end=5),
                     quote="Revenue 22,363 21,680")
    item = _data("Revenue was 22,363.", [cit], None)
    res = checks.check_data_item(item, pages, SOURCES)
    qe = _by(res, "quote_exact")
    assert qe.result == "pass" and qe.detail.startswith("Warning") and "page 2" in qe.detail
    fixed = checks.with_fixed_locators(item, res)
    loc = fixed.citations[0].locator
    assert loc.page == 2 and pages["DOC-001"][1][loc.char_start:loc.char_end] == "Revenue 22,363 21,680"
    assert item.citations[0].locator.page == 1  # the check never mutates its input


def test_quarantined_document_blocks_and_never_passes() -> None:
    item = _data("Revenue was 22,363.", [_cit("Revenue 22,363 21,680")], None)
    res = checks.check_data_item(item, {"DOC-001": []}, SOURCES)
    assert _by(res, "quote_exact").result == "blocked"
    assert checks.status_from(res) == "blocked"


def test_json_value_and_period() -> None:
    v = m.Value(number=22363000000, as_reported="22363000000", unit="EUR", scale="one", period="FY2025")
    cit = m.Citation(doc_id="DOC-002", locator=m.JsonLocator(pointer="/facts/Rev/value"), quote="22363000000")
    item = _data("Revenue was €22,363 million in FY2025.", [cit], v, extracted_by="code")
    res = checks.check_data_item(item, PAGES, SOURCES)
    assert [c.check for c in res] == ["json_value", "period_match", "numbers_traced", "citations_resolve"]
    assert checks.status_from(res) == "pass", [c.detail for c in res]
    assert "2025-09-30" in _by(res, "period_match").detail  # midnight end is exclusive
    off = item.model_copy(update={"value": v.model_copy(update={"number": 22336000000})})
    assert _by(checks.check_data_item(off, PAGES, SOURCES), "json_value").result == "fail"
    bad_ptr = item.model_copy(update={"citations": [cit.model_copy(update={"locator": m.JsonLocator(pointer="/facts/Revenue")})]})
    assert "does not resolve" in _by(checks.check_data_item(bad_ptr, PAGES, SOURCES), "json_value").detail
    wrong_year = item.model_copy(update={"value": v.model_copy(update={"period": "FY2024"})})
    assert _by(checks.check_data_item(wrong_year, PAGES, SOURCES), "period_match").result == "fail"


def test_json_pointer_rfc6901() -> None:
    doc = {"a/b": {"m~n": [10, 20]}, "": 1}
    assert checks.json_pointer_get(doc, "/a~1b/m~0n/1") == 20
    assert checks.json_pointer_get(doc, "") is doc
    assert checks.json_pointer_get(doc, "/") == 1
    for bad in ("/a~1b/m~0n/01", "/a~1b/m~0n/2", "/nope", "a"):
        with pytest.raises((KeyError, IndexError, ValueError)):
            checks.json_pointer_get(doc, bad)


def test_revoked_source_blocks_the_item_and_its_dependents() -> None:
    item = _data("Revenue was €22,363 million.", [_cit("Revenue 22,363 21,680"), _cit("(in millions of €", "unit_context")],
                 m.Value(number=22363, as_reported="22,363", unit="EUR", scale="million"))
    revoked = {"S-001": _src(status="revoked")}
    cr = _by(checks.check_data_item(item, PAGES, revoked), "citations_resolve")
    assert cr.result == "blocked" and "source revoked" in cr.detail.lower()
    passed = item.model_copy(update={"verification": m.Verification(status="pass")})
    c = m.Conclusion(id="C-001", category="compounder", criteria=["secular_growth"], text="Revenue was €22,363 million.",
                     data_ids=["D-001"], verification=m.Verification(status="pending"))
    items = {"D-001": passed, "C-001": c}
    assert _by(checks.check_conclusion(c, items, SOURCES), "citations_resolve").result == "pass"
    cr = _by(checks.check_conclusion(c, items, revoked), "citations_resolve")
    assert cr.result == "blocked" and "source revoked" in cr.detail


def test_citations_resolve_fails_on_missing_or_failed_ids() -> None:
    d = _data("x", [_cit("Revenue 22,363 21,680")], None)
    items = {"D-001": d.model_copy(update={"verification": m.Verification(status="fail")})}
    c = m.Conclusion(id="C-001", category="compounder", criteria=["secular_growth"], text="No numbers here.",
                     data_ids=["D-001", "D-404"], verification=m.Verification(status="pending"))
    cr = _by(checks.check_conclusion(c, items, SOURCES), "citations_resolve")
    assert cr.result == "fail" and "D-404 does not exist" in cr.detail and "D-001 is fail" in cr.detail


def test_status_from() -> None:
    p = checks._cr("quote_exact", "pass", "ok")
    f = checks._cr("value_in_quote", "fail", "no")
    b = checks._cr("unit_context", "blocked", "?")
    assert checks.status_from([p, b, f]) == "fail"
    assert checks.status_from([p, b]) == "blocked"
    assert checks.status_from([p]) == "pass"
    assert checks.status_from([]) == "pending"


def test_tier_rules() -> None:
    def item(iid: str, tier: str, number: float, as_rep: str, scale: str = "million") -> m.DataItem:
        return _data(f"Revenue was €{as_rep} {scale} in fiscal year 2025.", [_cit("Revenue 22,363 21,680")],
                     m.Value(number=number, as_reported=as_rep, unit="EUR", scale=scale, period="FY2025"),
                     iid=iid, source_id={"priority": "S-001", "tier_2": "S-002", "tier_3": "S-003"}[tier],
                     source_tier=tier, verification=m.Verification(status="pass"))

    sources = {"S-001": _src(), "S-002": _src("S-002", tier="tier_2"), "S-003": _src("S-003", tier="tier_3")}
    t1 = item("D-001", "priority", 22363, "22,363")
    agree = item("D-002", "tier_2", 22.4, "22.4", "billion")
    clash = item("D-003", "tier_2", 23.1, "23.1", "billion")
    lone = item("D-004", "tier_3", 950, "950")
    items = {i.id: i for i in (t1, agree, clash, lone)}
    out = checks.check_tier_rules(items, sources)
    assert "D-001" not in out
    assert out["D-002"][0].result == "pass" and "D-001" in out["D-002"][0].detail
    assert out["D-003"][0].result == "fail" and "Contradicts D-001" in out["D-003"][0].detail
    assert out["D-004"][0].result == "fail"
    c = m.Conclusion(id="C-001", category="compounder", criteria=["secular_growth"], text="t", data_ids=["D-004"],
                     verification=m.Verification(status="pending"))
    assert checks.check_tier_rule(c, items, sources)[0].result == "fail"


def test_extrapolation_na_and_wrong_category() -> None:
    na = m.Extrapolation(id="E-001", category="research_stage", criterion="research_stage", score=None,
                         na_reason="First review.", text="Research stage is not scored.", conclusion_ids=[],
                         rule=m.ScoreRule(type="threshold"), confidence="low", verification=m.Verification(status="pending"))
    assert checks.status_from(checks.check_extrapolation(na, {}, RUBRIC)) == "pass"
    no_reason = na.model_copy(update={"na_reason": None})
    assert checks.status_from(checks.check_extrapolation(no_reason, {}, RUBRIC)) == "fail"
    wrong_cat = na.model_copy(update={"category": "compounder"})
    assert "belongs to research_stage" in checks.check_extrapolation(wrong_cat, {}, RUBRIC)[0].detail


def test_sentence_about_a_declared_gap_may_cite_the_failed_item() -> None:
    c = m.Conclusion(id="C-009", category="dominant_business", criteria=["rotic_vs_peers"], text="t", data_ids=["D-001"],
                     verification=m.Verification(status="fail"))
    hole = m.Hole(hole_id="HOLE-03", item_id="C-009", reason="failed twice")
    s = m.SummarySentence(id="H-001", text="A market-share claim failed verification.", mode="derived",
                          derived_from=["C-009", "HOLE-03"], verification=m.Verification(status="pending"))
    assert checks.status_from(checks.check_sentence(s, {"C-009": c}, [hole])) == "pass"
    alone = s.model_copy(update={"derived_from": ["C-009"]})
    assert checks.check_sentence(alone, {"C-009": c}, [hole])[0].result == "fail"


def test_rollup_sentence_blocks_without_the_rollup() -> None:
    s = m.SummarySentence(id="H-001", text="The company scores +0.50, with 4 of 6 criteria scored.", mode="derived",
                          derived_from=["rollup"], verification=m.Verification(status="pending"))
    assert _by(checks.check_sentence(s, {}, []), "numbers_traced").result == "blocked"


# ---------------------------------------------------------------------------
# checks over the mock fixture (fictional company, contract-valid)
# ---------------------------------------------------------------------------


@pytest.fixture
def fx(fixture_bundle: dict) -> dict:
    b = copy.deepcopy({k: v for k, v in fixture_bundle.items() if k != "page_text"})
    items = {i["id"]: TypeAdapter(m.Item).validate_python(i) for i in b["items"]}
    sources = {s["source_id"]: m.Source.model_validate(s) for s in b["sources"]}
    pages = {d["doc_id"]: [fixture_bundle["page_text"](d["doc_id"], p) for p in range(1, d["pages"] + 1)]
             for d in b["docs"]}
    return {"items": items, "sources": sources, "pages": pages, "report": b["report"], "summary": b["summary"]}


def test_fixture_xbrl_items_pass_every_code_check(fx: dict) -> None:
    xbrl = [i for i in fx["items"].values() if i.kind == "data" and i.extracted_by == "code"]
    assert len(xbrl) == 21
    for it in xbrl:
        res = checks.check_data_item(it, fx["pages"], fx["sources"])
        assert checks.status_from(res) == "pass", (it.id, [c.detail for c in res if c.result != "pass"])


def test_fixture_text_quotes_are_exact(fx: dict) -> None:
    for it in fx["items"].values():
        if it.kind == "data" and it.extracted_by == "agent":
            res = checks.check_data_item(it, fx["pages"], fx["sources"])
            for c in res:
                if c.check in ("quote_exact", "value_in_quote", "unit_context", "numbers_traced", "citations_resolve"):
                    assert c.result == "pass", (it.id, c.check, c.detail)


def test_fixture_rotic_conclusion_scores_chart_and_brief(fx: dict) -> None:
    items = fx["items"]
    for cid in ("C-001", "C-005", "C-006", "C-007", "C-008"):
        res = checks.check_conclusion(items[cid], items, fx["sources"])
        assert checks.status_from(res) == "pass", (cid, [c.detail for c in res if c.result != "pass"])
    for eid in ("E-001", "E-005"):
        res = checks.check_extrapolation(items[eid], items, RUBRIC)
        assert _by(res, "threshold_rule").result == "pass", _by(res, "threshold_rule").detail
        assert checks.status_from(res) == "pass", [c.detail for c in res]
    for gid in ("G-001", "G-002"):
        assert checks.status_from(checks.check_chart(items[gid], items)) == "pass"
    rollup = m.Rollup.model_validate(fx["report"]["rollup"])
    holes = [m.Hole.model_validate(h) for h in fx["report"]["holes"]]
    first = fx["summary"]["sections"][0]["blocks"][0]["sentences"]
    for raw in first:
        s = m.SummarySentence.model_validate(raw)
        res = checks.check_sentence(s, items, holes, rollup=rollup)
        assert checks.status_from(res) == "pass", (s.id, [c.detail for c in res if c.result != "pass"])
