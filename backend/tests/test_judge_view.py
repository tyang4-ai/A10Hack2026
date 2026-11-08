"""B-fix-judge: what a judge sees (signed expr_value, exact computation results) and the statement's years in
period_match. Offline (FAKE_MODELS=1).

Judge views use excerpts of real SHL text (S-001, annual report FY2025, labeled where used), joined into one
test page. Judges answer from fakes registered here (simulated). The replay walks the stored runs, read only:
backend/data/replay/* (in the repo), backend/.data/g5b_export and backend/.data/fixyield (not committed, so
those cases skip on other machines). Run with -s to see the replay lines.
"""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from backend.axelrod.agents import judge
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import verify
from backend.axelrod.store import Store
from backend.axelrod.verify import checks, compute, text

REPO = Path(__file__).resolve().parents[2]
D = Decimal


@pytest.fixture(autouse=True)
def _judge_fakes():
    """Every test starts and ends with the module's own simulated judge."""
    judge.register_fakes()
    yield
    judge.register_fakes()


# ---------------------------------------------------------------------------
# Builders (offsets come from code, text.locate)
# ---------------------------------------------------------------------------

# Real SHL text (S-001, annual report FY2025): the EBIT row and the IFRS 3 purchase price allocation row, each
# with its unit header and year header, joined into one test page.
INCOME = ("(in millions of €, earnings per share in €) \n2025 \n2024 \n"
          "Earnings before interest and taxes \n \n3,154 \n2,807 \n"
          "(in millions of €) \n2025 \n2024 \n"
          "Amortization, depreciation and other effects from IFRS 3 purchase price allocation adjustments \n−357 \n−375 \n"
          "Comparable revenue growth \n8.5%")
# Real SHL text (S-001, annual report FY2025, page 160): the loan table behind G5b D-262.
LOANS = ("(Carrying amounts in millions of €) \nMaturity \n(fiscal year) \nContractual  \ninterest rate \n"
         "Sept 30, 2025 \nSept 30, 2024 \nSept 30, 2025 \nSept 30, 2024 \n"
         "Loan (US$1,742 million) \n2026 \n1.38% \n1,484 \n‐ \n‐ \n1,556 \n"
         "Loan (US$1,740 million) \n2031 \n2.30% \n‐ \n‐ \n1,482 \n1,554")
PAGES = {"DOC-001": [INCOME, LOANS],
         "DOC-007": ['{"units": {"USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": 19552000000, '
                     '"fy": 2024, "form": "10-K"}]}}']}  # simulated SEC companyfacts shape
SOURCE = m.Source(source_id="S-001", title="Annual report FY2025", url="https://example.com/ar2025.pdf",
                  domain="example.com", kind="annual_report", data_types=["financial_filing"],
                  access=m.SourceAccess(), tier="priority", rank=1,
                  scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data", need_ids=[],
                  criteria=[], vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved",
                  status="extracted", doc_ids=["DOC-001", "DOC-007"])
SOURCES = {"S-001": SOURCE}


def cite(quote: str, role: str = "evidence", page: int = 1) -> m.Citation:
    p, s, e = text.locate(PAGES["DOC-001"], quote, page) or (page, 0, 0)
    return m.Citation(doc_id="DOC-001", locator=m.TextLocator(page=p, char_start=s, char_end=e), quote=quote, role=role)


def data(iid: str, statement: str, cits: list[m.Citation], value: m.Value | None, *,
         status: str = "pending") -> m.DataItem:
    return m.DataItem(id=iid, category="financial_strength", criteria=["liquidity"], statement=statement,
                      value=value, citations=cits, source_id="S-001", source_tier="priority", extracted_by="agent",
                      verification=m.Verification(status=status))


def eur_m(number: float, as_reported: str, period: str = "FY2025") -> m.Value:
    return m.Value(number=number, as_reported=as_reported, unit="EUR", scale="million", period=period)


def ebit() -> m.DataItem:
    return data("D-125", "Earnings before interest and taxes were €3,154 million in fiscal year 2025.",
                [cite("Earnings before interest and taxes \n \n3,154 \n2,807"),
                 cite("(in millions of €, earnings per share in €)", "unit_context"), cite("2025 \n2024", "period_context")],
                eur_m(3154, "3,154"), status="pass")


def ppa() -> m.DataItem:
    return data("D-203", "Amortization, depreciation and other effects from IFRS 3 purchase price allocation "
                         "adjustments was −€357 million in fiscal year 2025.",
                [cite("Amortization, depreciation and other effects from IFRS 3 purchase price allocation "
                      "adjustments \n−357 \n−375"), cite("(in millions of €)", "unit_context")],
                eur_m(-357, "−357"), status="pass")


def growth() -> m.DataItem:
    v = m.Value(number=8.5, as_reported="8.5%", unit="%", scale="one", period="FY2025")
    return data("D-140", "Comparable revenue growth was 8.5% in fiscal year 2025.",
                [cite("Comparable revenue growth \n8.5%")], v, status="pass")


def ebit_plus_ppa() -> m.Conclusion:
    """EBIT before the purchase price allocation effects: subtracting a negative-reported amount adds it back."""
    return m.Conclusion(id="C-001", category="financial_strength", criteria=["liquidity"],
                        text="EBIT before purchase price allocation effects, that is EBIT plus the €357 million of "
                             "amortization, was €3,511 million in fiscal year 2025.",
                        data_ids=["D-125", "D-203"],
                        computations=[m.Computation(name="ebit_before_ppa", expr="D-125 - D-203", result=3511e6,
                                                    display="€3,511 million")],
                        agent_id="drafter-01", verification=m.Verification(status="pending"))


def make_ctx(settings, items: list[m.Item]) -> RunContext:
    """A real Store, Vault, Gateway and RunContext holding the given items (simulated run)."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG (test)", ticker="SHL"))
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    state.sources = [SOURCE.model_copy()]
    for it in items:
        state.items[it.id] = it
    ctx.emit("run.status", {"status": "running", "stage": "verify"}, stage="verify", message="Stage verify")
    return ctx


# ---------------------------------------------------------------------------
# Task 1: the judge's evidence shows the signed numbers code used
# ---------------------------------------------------------------------------


def test_data_view_puts_the_signed_expr_value_next_to_the_reported_value() -> None:
    view = verify.data_view(ppa())
    assert view["value"] == "−357" and view["expr_value"] == "-357000000"
    assert list(view)[list(view).index("value") + 1] == "expr_value"
    assert verify.data_view(ebit())["expr_value"] == "3154000000"
    assert verify.data_view(growth())["expr_value"] == "0.085"  # a percentage as a fraction
    fact = data("D-900", "The loan has no amounts drawn.", [cite("Loan (US$1,742 million)", page=2)], None)
    assert "expr_value" not in verify.data_view(fact)


def test_plain_numbers_have_no_exponent() -> None:
    assert verify.plain(D("4.03E+8")) == "403000000"
    assert verify.plain(0.1) == "0.1" and verify.plain(-0.0) == "0" and verify.plain(3511e6) == "3511000000"


async def test_judges_see_signed_values_exact_results_and_the_sign_rule(settings) -> None:
    """G5b C-003 and B-fix-yield C-064/C-065: judges read "EBIT less a −375 amortization" as not adding it back.
    Now the prompt carries every cited id's signed expr_value and the rule to judge on it."""
    c = ebit_plus_ppa()
    ctx = make_ctx(settings, [ebit(), ppa(), c])
    assert checks.status_from(verify.code_checks(ctx.state, c)) == "pass"  # so the claim reaches the judges
    seen: list[tuple[str, str]] = []

    def fake(system: str, user: str, schema: dict) -> dict:  # simulated judge that records its view
        seen.append((system, user))
        return {"verdict": "SUPPORTED", "unsupported_spans": [], "reason": "Matches (simulated judge)."}

    register_fake(tool_name(judge.JudgeVerdict), fake)
    res = await verify.judge_claim(ctx, c)
    assert res.result == "pass" and (res.votes_for, res.votes_total) == (3, 3)
    assert len(seen) == 3 and len({u for _, u in seen}) == 1  # three isolated judges, one identical view
    system, user = seen[0]
    assert "A negative-reported cost subtracted in an expression is added back" in system
    assert "judge the arithmetic on the signed values shown" in system
    evidence = {e["id"]: e for e in judge.parse_block(user, "evidence")}
    assert (evidence["D-203"]["value"], evidence["D-203"]["expr_value"]) == ("−357", "-357000000")
    claim = judge.parse_block(user, "claim")
    assert claim["computations"][0]["result"] == "3511000000"
    assert claim["computations"][0]["checked_by_code"] is True


def test_the_view_alone_recomputes_every_computation() -> None:
    """A judge needs nothing but its view to redo the arithmetic: the expr_values give each result exactly."""
    c = ebit_plus_ppa()
    state = SimpleNamespace(items={"D-125": ebit(), "D-203": ppa(), "C-001": c})  # all evidence_view reads
    env = {e["id"]: D(e["expr_value"]) for e in verify.evidence_view(state, c)}
    for comp in verify.claim_view(c)["computations"]:
        assert compute.safe_eval(comp["expr"], env) == D(comp["result"])
    assert env["D-125"] - env["D-203"] > env["D-125"]  # subtracting the negative amount adds it back


def test_score_evidence_shows_each_computation_result() -> None:
    view = verify.conclusion_view(ebit_plus_ppa())
    assert view["computations"] == [{"name": "ebit_before_ppa", "expr": "D-125 - D-203", "result": "3511000000",
                                     "display": "€3,511 million"}]


# ---------------------------------------------------------------------------
# Task 2: every year the statement names is backed by a quote or is the value's period
# ---------------------------------------------------------------------------

LOAN_ROW = "Loan (US$1,740 million) \n2031 \n2.30% \n‐ \n‐ \n1,482 \n1,554"
LOAN_HEADER = "Sept 30, 2025 \nSept 30, 2024 \nSept 30, 2025 \nSept 30, 2024"


def loan(statement: str, period: str = "FY2025", header: str | None = LOAN_HEADER) -> m.DataItem:
    cits = [cite(LOAN_ROW, page=2), cite("(Carrying amounts in millions of €)", "unit_context", 2)]
    if header:
        cits.append(cite(header, "period_context", 2))
    return data("D-262", statement, cits, eur_m(1482, "1,482", period))


def period_match(item: m.DataItem) -> m.CheckResult:
    return next(c for c in checks.check_data_item(item, PAGES, SOURCES) if c.check == "period_match")


def test_d262_statement_year_that_no_quote_shows_fails() -> None:
    """G5b D-262 (real SHL text): the statement says 2029, the row's maturity column says 2031."""
    bad = loan("A loan of US$1,740 million (€1,482 million) is due in fiscal year 2029.")
    res = period_match(bad)
    assert res.result == "fail"
    assert "'fiscal year 2029'" in res.detail and "no cited quote shows 2029" in res.detail and "FY2025" in res.detail
    others = [c for c in checks.check_data_item(bad, PAGES, SOURCES) if c.check != "period_match"]
    assert checks.status_from(others) == "pass"  # only period_match catches it
    assert period_match(loan("A loan of US$1,740 million (€1,482 million) is due in fiscal year 2031.")).result == "pass"
    assert period_match(loan("A loan of US$1,740 million (€1,482 million) matures in 2031.")).result == "pass"
    assert period_match(loan("A loan of US$1,740 million was carried at €1,482 million.")).result == "pass"


def test_year_ends_comparatives_and_the_values_own_period_count() -> None:
    assert period_match(loan("At September 30, 2025, the US$1,740 million loan was carried at €1,482 million, "
                             "against €1,554 million at September 30, 2024.")).result == "pass"
    assert period_match(loan("At Sep 30, 2023, the loan was carried at €1,482 million.")).result == "fail"
    # without a header, the value's own period still backs the statement's FY2025
    no_header = loan("In fiscal year 2025, the US$1,740 million loan due 2031 was carried at €1,482 million.",
                     header=None)
    assert checks._statement_period(no_header.statement, no_header.value, [text.normalize(LOAN_ROW)]) is None
    assert "2029" in checks._statement_period("Due in FY29.", no_header.value, [text.normalize(LOAN_ROW)])


def test_an_existing_failure_keeps_its_detail_and_names_the_year_too() -> None:
    wrong = loan("A loan of €1,482 million is due in fiscal year 2029.", period="FY2023")
    res = period_match(wrong)
    assert res.result == "fail"
    assert "does not mention FY2023" in res.detail and "'fiscal year 2029'" in res.detail


def test_amounts_that_look_like_years_are_not_years() -> None:
    years = checks._statement_years("Revenue was €2030 million (2030 percent of plan) in FY25, due 2031 and "
                                    "at Sept 30, 2025.")
    assert years == [(2025, "FY25"), (2031, "2031"), (2025, "Sept 30, 2025")]


def test_json_fact_statement_year_is_its_period_end() -> None:
    """Simulated SEC companyfacts shape: a calendar 2023 fact filed in the fy 2024 10-K."""
    v = m.Value(number=19552000000, as_reported="19552000000", unit="USD", scale="one", period="FY2023")
    cit = m.Citation(doc_id="DOC-007", locator=m.JsonLocator(pointer="/units/USD/0/val"), quote="19552000000")

    def item(statement: str) -> m.DataItem:
        return data("D-901", statement, [cit], v).model_copy(update={"extracted_by": "code"})
    ok = item("GE HealthCare revenue for FY2023 (year to Dec 31, 2023) was USD 19,552 million (us-gaap, 10-K).")
    assert period_match(ok).result == "pass", period_match(ok).detail
    filing_year = period_match(item("GE HealthCare revenue for FY2024 was USD 19,552 million."))
    assert filing_year.result == "fail" and "'FY2024'" in filing_year.detail  # fy 2024 is the filing, not the fact


def test_facts_without_a_number_keep_their_checks() -> None:
    """Their statements add the report's own year ("strong growth in fiscal year 2025", seen in every stored
    run) that no quote repeats. Without a value there is no period to compare, so period_match stays out."""
    fact = data("D-158", "The loan table lists maturities in fiscal year 2029.", [cite(LOAN_ROW, page=2)], None)
    assert "period_match" not in {c.check for c in checks.check_data_item(fact, PAGES, SOURCES)}


# ---------------------------------------------------------------------------
# Task 3: replay over the stored runs (read only)
# ---------------------------------------------------------------------------

RUNS = {
    "whhj0d": REPO / "backend" / "data" / "replay" / "run_20261108_whhj0d" / "state.json",
    "pin2ve": REPO / "backend" / "data" / "replay" / "run_20261108_pin2ve" / "state.json",
    "g5b": REPO / "backend" / ".data" / "g5b_export" / "state.json",
    "fixyield": REPO / "backend" / ".data" / "fixyield" / "runs" / "run_20261108_0murfv" / "state.json",
}
# Every pass-to-fail the statement rule makes on the stored runs, each checked by hand as a real mismatch.
# fixyield is a copy of the G5b state, so it holds the same D-262.
PASS_TO_FAIL = {"whhj0d": set(), "pin2ve": set(), "g5b": {"D-262"}, "fixyield": {"D-262"}}


def period_before_after(it: m.DataItem, pages: dict[str, list[str]]):
    """period_match without the statement rule and with it, built the way check_data_item builds them."""
    roles: dict[str, list[str]] = {"evidence": [], "unit_context": [], "period_context": []}
    json_docs = []
    for cit in it.citations:
        if isinstance(cit.locator, m.TextLocator):
            roles[cit.role].append(text.normalize(cit.quote))
        elif cit.role == "evidence":
            _, doc = checks._json_value(it, cit, pages)
            if doc is not None:
                json_docs.append((cit, doc))
    ev, uc, pc = roles["evidence"], roles["unit_context"], roles["period_context"]
    if ev:
        before = checks._period_text(it.value, ev, pc)
        return before, checks._with_statement(before, it.statement, it.value, ev + uc + pc)
    if json_docs:
        cit, doc = json_docs[0]
        before = checks._json_period(it.value, doc, cit.locator.pointer)
        return before, checks._with_statement(before, it.statement, it.value, uc + pc,
                                              checks._json_years(doc, cit.locator.pointer))
    return None, None


@pytest.mark.parametrize("name", list(RUNS))
def test_replay_statement_years_over_the_stored_runs(name: str) -> None:
    path = RUNS[name]
    if not path.exists():
        pytest.skip(f"stored run {name} is not on this machine ({path})")
    s = json.loads(path.read_text(encoding="utf-8"))
    sources = {x["source_id"]: m.Source.model_validate(x) for x in s["sources"]}
    moves: Counter = Counter()
    to_fail: dict[str, str] = {}
    for raw in s["items"].values():
        if raw["kind"] != "data" or raw.get("value") is None:
            continue
        it = TypeAdapter(m.Item).validate_python(raw)
        before, after = period_before_after(it, s["pages"])
        if before is None:
            continue
        moves[f"{before.result}->{after.result}"] += 1
        assert not (before.result == "fail" and after.result != "fail")  # the rule only ever adds a failure
        if before.result != "fail" and after.result == "fail":
            to_fail[it.id] = after.detail
            full = next(c for c in checks.check_data_item(it, s["pages"], sources) if c.check == "period_match")
            assert (full.result, full.detail) == ("fail", after.detail)  # check_data_item takes the same path
    print(f"\nreplay {name}: period_match {dict(sorted(moves.items()))}, pass to fail: "
          + ("; ".join(f"{k} ({v})" for k, v in sorted(to_fail.items())) or "none"))
    assert set(to_fail) == PASS_TO_FAIL[name]
    if "D-262" in to_fail:
        assert "'fiscal year 2029'" in to_fail["D-262"]
