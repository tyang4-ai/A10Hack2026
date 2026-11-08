"""B-fix-yield: small claims, repair by removal, and scores that rest on what passed. Offline (FAKE_MODELS=1).

The data is the mock fixture's verified data (a fictional company, Northwind Instruments), plus a few
fictional rows (simulated). Drafter, judges and repairer answer from fakes registered here (simulated).
The last test replays the six conclusions that failed in the G5b run (real SHL output, read only from
backend/.data/g5b_report.json, skipped when that file is not there) through code-only repair.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.axelrod.agents import drafter, judge, repairer
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import draft, finalize, repair, verify
from backend.axelrod.store import Store
from backend.axelrod.verify import checks

G5B_REPORT = Path("backend/.data/g5b_report.json")
NOT_SCORED = {"score": None, "text": "Not scored.", "confidence": "low", "na_reason": "Not part of this test draft."}


# ---------------------------------------------------------------------------
# Fixtures (fictional data, simulated)
# ---------------------------------------------------------------------------


def _register_defaults() -> None:
    drafter.register_fakes()
    judge.register_fakes()
    repairer.register_fakes()


@pytest.fixture(autouse=True)
def _default_fakes():
    """Every test starts and ends with the module fakes, whatever another test registered."""
    _register_defaults()
    yield
    _register_defaults()


def make_ctx(settings, fixture_bundle, fixture_report) -> RunContext:
    """A real Store, Vault, Gateway and RunContext holding the fixture's verified data items."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Northwind Instruments (fixture)", ticker="NWI-X"))
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    state.needs = [m.DataNeed.model_validate(n) for n in fixture_bundle["needs"]]
    sources = []
    for raw in fixture_bundle["sources"]:
        s = m.Source.model_validate(raw)
        s.approval, s.status = "approved", "extracted"
        sources.append(s)
    state.sources = sources
    state.as_of = "2026-11-08"
    for item in fixture_report.items:
        if item.kind == "data":
            state.items[item.id] = item
    state.ids.counters["D"] = len(state.items)
    ctx.emit("run.status", {"status": "running", "stage": "draft"}, stage="draft", message="Stage draft")
    return ctx


async def run_reasoning(ctx: RunContext) -> None:
    for stage, mod in (("draft", draft), ("verify", verify), ("repair", repair)):
        ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
        await mod.run(ctx)


def by_criterion(state) -> dict[str, m.Extrapolation]:
    return {e.criterion: e for e in state.extrapolations()}


def _fictional(base: m.DataItem, iid: str, statement: str, number: float, *, quote: str | None = None,
               period: str = "FY2025") -> m.DataItem:
    """A fictional data item copied from a fixture item, so its citations still validate (simulated)."""
    v = base.value.model_copy(update={"number": number, "as_reported": f"{number:,.0f}", "period": period})
    cits = [c.model_copy(update={"quote": quote}) if quote and c.role == "evidence" else c for c in base.citations]
    return base.model_copy(update={"id": iid, "statement": f"{statement} (fictional)", "value": v, "citations": cits})


def leverage_fake(conclusions: list[dict], score: dict, prompts: list[str] | None = None):
    """Simulated drafter for the leverage and liquidity group: the given leverage draft, liquidity not scored."""
    def fake(system: str, user: str, schema: dict) -> dict:
        if prompts is not None:
            prompts.append(user)
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            if crit["criterion"] == "leverage":
                out["conclusions"] += conclusions
                out["scores"].append(score)
            else:
                out["scores"].append({"criterion": crit["criterion"], **NOT_SCORED})
        return out

    return fake


def word_judge(word: str, span_of=lambda text, word: text):
    """The module's simulated judge, plus: a claim using word is unsupported (span_of picks the span)."""
    def fake(system: str, user: str, schema: dict) -> dict:
        text = (judge.parse_block(user, "claim") or {}).get("text", "")
        if word in text:
            return {"verdict": "UNSUPPORTED", "unsupported_spans": [span_of(text, word)],
                    "reason": f"Nothing cited says {word!r} (simulated judge)."}
        return judge.fake_judge(system, user, schema)

    return fake


def _note(item: m.Item, n: int = 0) -> str:
    return item.verification.attempts[n].note or ""


# ---------------------------------------------------------------------------
# Sentences and claim size
# ---------------------------------------------------------------------------


def test_sentences_split_only_where_a_sentence_surely_ends():
    assert draft.sentences("Total debt was $1,150 million. Free cash flow was $468 million.") == [
        "Total debt was $1,150 million.", "Free cash flow was $468 million."]
    assert len(draft.sentences("Net debt to EBITDA was 2.8x. Pensions were €488 million.")) == 2
    assert len(draft.sentences("The U.S. federal rate is 21%. It applies to FY2025.")) == 2
    assert len(draft.sentences("Revenue was approx. 5% higher at Sep. 30, 2025 than a year earlier.")) == 1
    assert len(draft.sentences("Cover was 2.5 times, e.g. FY2025. The company pays no dividend.")) == 2
    assert draft.claim_size("Revenue rose from €1,710 million in FY2021 to €2,412 million in FY2025, an increase "
                            "of 41.1%.") == (1, 3)                         # fiscal-year labels are not numbers


# ---------------------------------------------------------------------------
# Repair by removal
# ---------------------------------------------------------------------------

LONG = ("Total debt was $1,150 million at September 30, 2025. Free cash flow was $468 million in fiscal 2025. "
        "Debt and free cash flow add up to $1,618 million.")


async def test_long_conclusion_with_one_bad_number_loses_that_sentence(settings, fixture_bundle, fixture_report):
    """The undeclared sum fails numbers_traced. Code's first repair removes the sentence that holds it, nothing
    else, and the shorter claim passes code and judges. The repairer agent is never called."""
    register_fake(tool_name(drafter.DraftOutput), leverage_fake(
        [{"key": "c1", "criteria": ["leverage"], "data_ids": ["D-028", "D-031"], "text": LONG}],
        {"criterion": "leverage", "score": 0, "confidence": "medium", "conclusion_keys": ["c1"],
         "text": "Leverage scores 0 under the framework: total debt was $1,150 million (c1)."}))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await run_reasoning(ctx)
    state = ctx.state
    c = next(iter(state.conclusions()))
    first = c.verification.attempts[0]
    assert next(x for x in first.checks if x.check == "numbers_traced").unsupported_spans == ["$1,618 million"]
    assert first.action == "downgrade" and first.before == LONG
    assert _note(c) == f'{repair.REMOVED_NOTE}: "Debt and free cash flow add up to $1,618 million.".'
    assert c.text == "Total debt was $1,150 million at September 30, 2025. Free cash flow was $468 million in fiscal 2025."
    assert c.verification.status == "fixed" and c.data_ids == ["D-028", "D-031"]
    assert next(x for x in c.verification.checks if x.check == "support").result == "pass"
    assert not any(a.role == "repairer" for a in state.agents.values())
    e = by_criterion(state)["leverage"]
    assert e.score == 0 and e.verification.status == "fixed" and c.id in e.text   # the key became the id


async def test_judged_sentence_is_removed_and_its_citation_dropped(settings, fixture_bundle, fixture_report):
    """A judge names an unsupported span in the middle sentence: code removes that sentence and the citation only
    it used, and the rest stands."""
    text = ("Total debt was $1,150 million at September 30, 2025. Free cash flow was a conservative $468 million in "
            "fiscal 2025. The company has not paid cash dividends.")
    register_fake(tool_name(drafter.DraftOutput), leverage_fake(
        [{"key": "c1", "criteria": ["leverage"], "data_ids": ["D-028", "D-031", "D-033"], "text": text}],
        {"criterion": "leverage", "score": 0, "confidence": "medium", "conclusion_keys": ["c1"],
         "text": "Leverage scores 0 under the framework."}))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await run_reasoning(ctx)
    c = next(iter(ctx.state.conclusions()))
    assert c.verification.attempts[0].action == "downgrade" and "conservative" in _note(c)
    assert c.text == "Total debt was $1,150 million at September 30, 2025. The company has not paid cash dividends."
    assert c.data_ids == ["D-028", "D-033"] and "Dropped D-031, no longer used." in _note(c)
    assert c.verification.status == "fixed"
    assert not any(a.role == "repairer" for a in ctx.state.agents.values())


def _conclusion(text: str, data_ids: list[str], comps: list[tuple[str, str, str]], checks_: list[m.CheckResult],
                cid: str = "C-001") -> m.Conclusion:
    return m.Conclusion(id=cid, category="financial_strength", criteria=["liquidity"], text=text, data_ids=data_ids,
                        computations=[m.Computation(name=n, expr=x, result=0.0, display=d) for n, x, d in comps],
                        verification=m.Verification(status="fail", checks=checks_))


def _support_fail(*spans: str, detail: str = "Not supported: 0 of 3 judges said supported.") -> m.CheckResult:
    return m.CheckResult(check="support", method="llm_judge", result="fail", detail=detail,
                         unsupported_spans=list(spans), votes_for=0, votes_total=3)


def test_judged_number_takes_what_was_computed_from_it(fixture_report):
    """A judge rejects the sentence showing coverage. Coverage is not trusted after that, nor is the ratio computed
    from it, so the sentence showing the ratio goes too, even though a score names it as its rule input. The
    sentence that rests on neither stays."""
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    c = _conclusion(
        "Scheduled principal payments total $400 million. Free cash flow covers them 1.2 times. "
        "Five years of it would cover them 5.9 times.",
        ["D-030", "D-031"],
        [("coverage", "D-031 / D-030", "1.2"), ("five_year", "coverage * 5", "5.9")],
        [_support_fail("covers them 1.2 times")])
    items[c.id] = c
    items["E-001"] = m.Extrapolation(id="E-001", category="financial_strength", criterion="liquidity", score=1,
                                     text="Liquidity scores +1.", conclusion_ids=[c.id], confidence="low",
                                     rule=m.ScoreRule(type="threshold", input=f"{c.id}:five_year"),
                                     verification=m.Verification(status="pending"))
    p = repair.remove_unsupported(c, items)
    assert p.action == "downgrade" and p.item.text == "Scheduled principal payments total $400 million."
    assert p.item.computations == [] and p.item.data_ids == ["D-030"]
    assert "Dropped coverage, five_year and D-031, no longer used." in p.note


def test_untraced_number_keeps_a_computation_a_score_relies_on(fixture_report):
    """An untraced number says nothing about the computations, so a rule input stays even when the sentence that
    showed it goes."""
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    traced = m.CheckResult(check="numbers_traced", method="code", result="fail", detail="'$868 million'",
                           unsupported_spans=["$868 million"])
    c = _conclusion("Scheduled principal payments total $400 million. Free cash flow covers them 1.2 times, "
                    "and together they are $868 million.", ["D-030", "D-031"],
                    [("coverage", "D-031 / D-030", "1.2")], [traced])
    items[c.id] = c
    items["E-001"] = m.Extrapolation(id="E-001", category="financial_strength", criterion="liquidity", score=0,
                                     text="Liquidity scores 0.", conclusion_ids=[c.id], confidence="low",
                                     rule=m.ScoreRule(type="threshold", input=f"{c.id}:coverage"),
                                     verification=m.Verification(status="pending"))
    p = repair.remove_unsupported(c, items)
    assert p.item.text == "Scheduled principal payments total $400 million."
    assert [x.name for x in p.item.computations] == ["coverage"] and p.item.data_ids == ["D-030", "D-031"]


def test_one_sentence_claim_goes_to_the_repairer(fixture_report):
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    c = _conclusion("Free cash flow of $468 million was conservative.", ["D-031"], [], [_support_fail("conservative")])
    assert repair.remove_unsupported(c, items) is None


def test_judge_reason_names_the_span_when_the_judge_listed_none(fixture_report):
    """G5b C-003: "The evidence indicates Varian growth is 5.5%, not 5.6% as stated". 5.6% is in the claim, 5.5% is
    not, so only the sentence with 5.6% goes. A number inside a longer one (15.6%) is not a match."""
    text = "Segment A grew 5.6%. Segment B grew 15.6%."
    detail = "Not supported: 0 of 3 judges said supported. The evidence indicates growth is 5.5%, not 5.6% as stated."
    assert repair.detail_spans(detail, text) == ["5.6%"]
    assert repair.drop_sentences(text, lambda s: repair._holds_raw(s, "5.6%")) == (
        "Segment B grew 15.6%.", ["Segment A grew 5.6%."])
    assert repair.detail_spans("Not supported: 1 of 3 judges said supported. It says 'between 4.0% and 5.0%'.",
                               "Growth between 4.0% and 5.0% is expected.") == ["between 4.0% and 5.0%", "4.0%", "5.0%"]


def test_repairer_correcting_a_number_or_year_is_refused(fixture_report):
    """A "corrected" number is a new claim (G5b: 5.6% to 5.5%, 2029 to 2031 and back). Removing a part is fine."""
    c = _conclusion("A €1,482 million loan is due in fiscal year 2029. Segment growth was 5.6%.", ["D-031"], [], [])
    out = lambda text: repairer.RepairOutput(action="downgrade", text=text, note="Fixed (simulated).")  # noqa: E731
    p = repair._apply_conclusion(c, out("A €1,482 million loan is due in fiscal year 2031. Segment growth was 5.6%."))
    assert p.action == "delete" and p.item is None and p.note.startswith("Code refused the repair: it states 2031")
    p = repair._apply_conclusion(c, out("A €1,482 million loan is due in fiscal year 2029. Segment growth was 5.5%."))
    assert p.action == "delete" and "states 5.5%" in p.note
    p = repair._apply_conclusion(c, out("A €1.5 billion loan is due in FY2029."))
    assert p.action == "downgrade" and p.item.text == "A €1.5 billion loan is due in FY2029."


def test_repairer_may_put_the_claims_own_checked_number_in(fixture_report):
    """Seen live (B-fix-yield run 2): the text said 13.3% where the claim's own computation gives 9.3%. Putting
    the code-checked display in is not a new fact, and neither is a year a cited item states."""
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    c = _conclusion("Free cash flow covers principal 1.3 times.", ["D-030", "D-031"],
                    [("coverage", "D-031 / D-030", "1.2")], [])
    out = repairer.RepairOutput(action="downgrade", text="Free cash flow covers principal 1.2 times.", note="(simulated)")
    assert repair._apply_conclusion(c, out, items).action == "downgrade"
    out = repairer.RepairOutput(action="downgrade", text="Free cash flow covers principal 1.2 times in fiscal 2025.",
                                note="(simulated)")
    assert repair._apply_conclusion(c, out, items).action == "downgrade"           # D-031 states fiscal 2025
    out = repairer.RepairOutput(action="downgrade", text="Free cash flow covers principal 1.2 times in 2031.",
                                note="(simulated)")
    assert repair._apply_conclusion(c, out, items).action == "delete"


def test_code_puts_the_claims_own_number_where_the_text_has_another(fixture_report):
    """One untraced number, one final computation whose display the text never shows: that is the claim's
    number, so code writes it in (a recompute). With two candidates code can't tell, and does nothing."""
    traced = m.CheckResult(check="numbers_traced", method="code", result="fail", detail="'13.3%'",
                           unsupported_spans=["13.3%"])
    c = _conclusion("For fiscal 2025, free cash flow over total debt, as a share of debt, gives 13.3%.",
                    ["D-028", "D-031"], [("share", "D-031 / D-028", "40.7%")], [traced])
    p = repair.retarget_number(c)
    assert p.action == "recompute" and p.item.text.endswith("gives 40.7%.") and "13.3%" in p.note
    two = _conclusion(c.text, c.data_ids, [("share", "D-031 / D-028", "40.7%"), ("other", "D-030 / D-028", "34.8%")],
                      [traced])
    assert repair.retarget_number(two) is None


def test_recompute_does_not_swap_a_display_two_computations_share(fixture_report):
    """G5b C-001 wrote FY2025 and FY2026 ROTIC both as 10.7%. Swapping the wrong one's display turned the other
    into a wrong number. Code can't tell the mentions apart, so it removes the sentences that hold that display."""
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    c = _conclusion("Free cash flow covers principal 1.2 times. Debt is 1.2 times free cash flow. "
                    "The company has not paid cash dividends.", ["D-028", "D-030", "D-031", "D-033"],
                    [("coverage", "D-031 / D-030", "1.2"), ("debt_to_fcf", "D-028 / D-031", "1.2")], [])
    p = repair.recompute_conclusion(c, items)
    assert p.action == "recompute" and p.item.text == "The company has not paid cash dividends."
    assert [x.display for x in p.item.computations] == ["1.2", "2.5"]
    assert "removed the sentence(s) holding 1.2" in p.note
    out = drafter.DraftOutput.model_validate({"conclusions": [
        {"key": "c1", "data_ids": c.data_ids, "text": c.text,
         "computations": [{"name": x.name, "expr": x.expr, "display": x.display} for x in c.computations]}],
        "scores": []})
    got, notes = draft.fill_displays(out, items, set(items))
    assert got.conclusions[0].text == c.text and not notes             # left for the checks to catch


# ---------------------------------------------------------------------------
# A score rests on what passed
# ---------------------------------------------------------------------------

GRANULAR = [
    {"key": "c1", "criteria": ["leverage"], "data_ids": ["D-028"],
     "text": "Total debt was $1,150 million at September 30, 2025."},
    {"key": "c2", "criteria": ["leverage"], "data_ids": ["D-031"], "text": "Free cash flow was $468 million in fiscal 2025."},
    {"key": "c3", "criteria": ["leverage"], "data_ids": ["D-028"],
     "text": "Northwind has the lowest leverage in the laboratory instruments industry."},
]
GRANULAR_SCORE = {"criterion": "leverage", "score": -1, "confidence": "medium", "conclusion_keys": ["c1", "c2", "c3"],
                  "text": "Leverage scores -1 under the framework: total debt of $1,150 million (c1) is large against "
                          "free cash flow of $468 million (c2). Its leverage is still the lowest in the industry (c3)."}


async def test_granular_draft_keeps_the_criterion_scored_when_one_conclusion_fails(settings, fixture_bundle,
                                                                                  fixture_report):
    """Three small claims, one of them unsupported. That one fails and becomes a hole, the score drops it and
    the sentence of its text that rested on it, keeps the drafter's -1, and passes on the other two."""
    register_fake(tool_name(drafter.DraftOutput), leverage_fake(GRANULAR, GRANULAR_SCORE))
    register_fake(tool_name(judge.JudgeVerdict),
                  word_judge("lowest", lambda text, w: text[text.index("has the"):].rstrip(".")
                             if "has the" in text else w))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await run_reasoning(ctx)
    state = ctx.state
    c1, c2, c3 = state.conclusions()
    assert c3.verification.status == "fail" and c3.verification.attempts[-1].action == "delete"
    assert c1.verification.status == "pass" and c2.verification.status == "pass"
    e = by_criterion(state)["leverage"]
    assert e.score == -1 and e.verification.status == "fixed" and e.conclusion_ids == [c1.id, c2.id]
    assert e.text == (f"Leverage scores -1 under the framework: total debt of $1,150 million ({c1.id}) is large "
                      f"against free cash flow of $468 million ({c2.id}).")
    note = next(a.note for a in e.verification.attempts if a.action == "downgrade")
    assert note.startswith(f"Dropped {c3.id}, which did not pass. The score now rests on {c1.id} and {c2.id}.")
    assert repair.REMOVED_NOTE in note
    assert "leverage" not in state.rollup.na_criteria
    assert any(h.item_id == c3.id for h in state.holes)


def _scored(criterion: str, score: int, cids: list[str], text: str, rule_input: str | None = None) -> m.Extrapolation:
    crit = RUBRIC["criteria"][criterion]
    return m.Extrapolation(id="E-001", category=crit["category"], criterion=criterion, score=score, text=text,
                           conclusion_ids=cids, confidence="medium",
                           rule=m.ScoreRule(type=crit["scoring"], plus_one=crit["plus_one"], minus_one=crit["minus_one"],
                                            input=rule_input),
                           verification=m.Verification(status="fail", checks=[m.CheckResult(
                               check="citations_resolve", method="code", result="fail", detail="C-002 is fail")]))


def test_threshold_score_whose_rule_input_failed_stays_na(fixture_report):
    """Code can't recompute a threshold score without its input, so other passing conclusions don't save it."""
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    items["C-001"] = _conclusion("Scheduled principal payments total $400 million.", ["D-030"], [], [], cid="C-001")
    items["C-001"].verification = m.Verification(status="pass")
    items["C-002"] = _conclusion("Free cash flow covers principal 1.2 times.", ["D-030", "D-031"],
                                 [("coverage", "D-031 / D-030", "1.2")], [], cid="C-002")
    e = _scored("liquidity", 0, ["C-001", "C-002"], "Liquidity scores 0.", rule_input="C-002:coverage")
    p = repair.repair_extrapolation_by_code(e, items)
    assert p.action == "not_determinable" and p.item.score is None and p.item.conclusion_ids == ["C-001"]


def test_judgment_score_stands_on_its_passing_conclusions(fixture_report):
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    items["C-001"] = _conclusion("Total debt was $1,150 million.", ["D-028"], [], [], cid="C-001")
    items["C-001"].verification = m.Verification(status="pass")
    items["C-002"] = _conclusion("Free cash flow was $468 million.", ["D-031"], [], [], cid="C-002")
    e = _scored("leverage", -1, ["C-001", "C-002"],
                "Leverage scores -1: total debt was $1,150 million (C-001). Free cash flow was $468 million (C-002).")
    p = repair.repair_extrapolation_by_code(e, items)
    assert p.action == "downgrade" and p.item.score == -1 and p.item.conclusion_ids == ["C-001"]
    assert p.item.text == "Leverage scores -1: total debt was $1,150 million (C-001)."
    e = _scored("leverage", -1, ["C-001", "C-002"], "Free cash flow was $468 million (C-002).")
    p = repair.repair_extrapolation_by_code(e, items)
    assert p.item.score == -1 and p.item.text == "Leverage scores -1 under the framework."


def test_score_sentence_with_an_untraced_number_is_removed(fixture_report):
    items = {i.id: i for i in fixture_report.items if i.kind == "data"}
    items["C-001"] = _conclusion("Total debt was $1,150 million.", ["D-028"], [], [], cid="C-001")
    items["C-001"].verification = m.Verification(status="pass")
    e = _scored("leverage", -1, ["C-001"], "Leverage scores -1: total debt was $1,150 million (C-001). "
                                          "Debt rose by $99 million.")
    e.verification = m.Verification(status="fail", checks=[m.CheckResult(
        check="numbers_traced", method="code", result="fail", detail="'$99 million'", unsupported_spans=["$99 million"])])
    p = repair.repair_extrapolation_by_code(e, items)
    assert p.action == "downgrade" and p.item.score == -1
    assert p.item.text == "Leverage scores -1: total debt was $1,150 million (C-001)."
    assert p.note == f'{repair.REMOVED_NOTE}: "Debt rose by $99 million.".'


# ---------------------------------------------------------------------------
# Drafting: one claim per conclusion, missing scores, signs, printed rates
# ---------------------------------------------------------------------------

THREE_CLAIMS = ("Total debt was $1,150 million at September 30, 2025. Free cash flow was $468 million in fiscal 2025. "
                "Scheduled principal payments over the next five fiscal years total $400 million.")


def splitting_fake(prompts: list[str]):
    """Simulated drafter: one long conclusion first, then (shown code's advice) three small ones."""
    def fake(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            if crit["criterion"] != "leverage":
                out["scores"].append({"criterion": crit["criterion"], **NOT_SCORED})
                continue
            if "<code_advice>" in user:
                parts = THREE_CLAIMS.split(". ")
                ids = (["D-028"], ["D-031"], ["D-030"])
                out["conclusions"] = [{"key": f"c{n}", "criteria": ["leverage"], "data_ids": list(i),
                                       "text": p if p.endswith(".") else p + "."}
                                      for n, (p, i) in enumerate(zip(parts, ids), 1)]
                keys = ["c1", "c2", "c3"]
            else:
                out["conclusions"] = [{"key": "c1", "criteria": ["leverage"], "data_ids": ["D-028", "D-031", "D-030"],
                                       "text": THREE_CLAIMS}]
                keys = ["c1"]
            out["scores"].append({"criterion": "leverage", "score": 0, "confidence": "medium", "conclusion_keys": keys,
                                  "text": "Leverage scores 0 under the framework: debt of $1,150 million (c1) against "
                                          f"free cash flow of $468 million ({keys[-2] if len(keys) > 1 else 'c1'})."})
        return out

    return fake


async def test_a_long_claim_is_sent_back_once_to_be_split(settings, fixture_bundle, fixture_report):
    prompts: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), splitting_fake(prompts))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    state = ctx.state
    lev = [p for p in prompts if '"criterion": "leverage"' in p]
    assert len(lev) == 2 and "<code_advice>" not in lev[0]
    assert "<code_advice>" in lev[1] and "<code_check>" not in lev[1]       # no check problems, only advice
    notes = [json.loads(x) for x in lev[1].split("<code_advice>\n")[1].split("\n</code_advice>")[0].splitlines()]
    assert notes == [{"conclusion": "c1", "sentences": 3, "numbers": 3,
                      "problem": "3 sentence(s) and 3 number(s). One claim is at most 2 sentences and 3 numbers.",
                      "fix": notes[0]["fix"]}]
    cs = state.conclusions()
    assert [c.text for c in cs] == [s for s in draft.sentences(THREE_CLAIMS)]
    e = by_criterion(state)["leverage"]
    assert e.conclusion_ids == [c.id for c in cs]
    assert e.text == (f"Leverage scores 0 under the framework: debt of $1,150 million ({cs[0].id}) against free "
                      f"cash flow of $468 million ({cs[1].id}).")                 # keys became ids
    logs = [ev.data.text for ev in state.events if ev.type == "agent.log"]
    assert any(t.startswith("Code advice on 1 part(s) of the draft") for t in logs)
    assert any(t.startswith("Revised draft: 0 pre-check problem(s) left of 0, 0 of 1 advice note(s) left") for t in logs)


def test_precheck_flags_a_missing_score_and_a_flipped_sign(settings, fixture_bundle, fixture_report):
    """G5b: the ROTIC drafter's first answer had no scores, and its exprs added negative-reported costs back while
    its displays had the deduction in mind. Code names both."""
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    capex = _fictional(ctx.state.items["D-031"], "D-901", "Additions to property, plant and equipment were "
                                                          "$100 million in fiscal 2025.", -100)
    ctx.state.items[capex.id] = capex
    verified = {d.id: d for d in draft.usable_data(ctx.state)}
    out = drafter.DraftOutput.model_validate({
        "conclusions": [{"key": "c1", "criteria": ["liquidity"], "data_ids": ["D-031", "D-901"],
                         "text": "Free cash flow after capital spending was $368 million.",
                         "computations": [{"name": "after_capex", "expr": "D-031 - D-901", "display": "$368 million"}]}],
        "scores": [{"criterion": "liquidity", **NOT_SCORED}]})
    found = draft.precheck(ctx.state, ["leverage", "liquidity"], out, verified, set(verified))
    comp = next(p for p in found if p.get("check") == "computation")
    assert comp["sign_check"] == [
        "after_capex reads D-901, which is reported as a negative number (expr_value -100000000). Code gets "
        "$568 million where your display says $368 million. With D-901 taken as positive it would give $368 million. "
        "Check its sign: adding a negative-reported amount deducts it, subtracting it adds it back."]
    missing = [p for p in found if p.get("check") == "score_missing"]
    assert [p["score"] for p in missing] == ["leverage"]


def test_advice_flags_a_computed_rate_the_source_prints_differently(fixture_report):
    """G5b C-003: Varian revenue 4,081 against 3,866 computes to 5.6%, while the row prints 5.5% (real SHL row)."""
    base = next(i for i in fixture_report.items if i.id == "D-028")
    row = "Varian  4,081  3,866  5.5%  6.9%"
    a = _fictional(base, "D-902", "Varian segment revenue was €4,081 million in fiscal year 2025.", 4081, quote=row)
    b = _fictional(base, "D-903", "Varian segment revenue was €3,866 million in fiscal year 2024.", 3866, quote=row,
                   period="FY2024")
    verified = {a.id: a, b.id: b}
    out = drafter.DraftOutput.model_validate({"conclusions": [{
        "key": "c2", "data_ids": ["D-902", "D-903"], "text": "Varian revenue grew 5.6% in fiscal year 2025.",
        "computations": [{"name": "varian_growth", "expr": "D-902 / D-903 - 1", "display": "5.6%"}]}], "scores": []})
    notes = draft.advice(out, verified, set(verified))
    assert notes == [{"conclusion": "c2", "computation": "varian_growth",
                      "problem": "D-902's source line prints 5.5% next to these figures, and your computed display "
                                 "is 5.6%.", "fix": notes[0]["fix"]}]
    out.conclusions[0].computations[0].display = "5.56%"
    assert draft.advice(out, verified, set(verified)) == []                # not a rounding step away


def test_code_writes_a_computed_amount_in_its_inputs_currency(fixture_report):
    """Seen live (B-fix-yield run 1): a peer's USD amounts shown in €, and the judges rejected the claim for it.
    Only the marker changes, never the number. A ratio has no currency and stays."""
    verified = {i.id: i for i in fixture_report.items if i.kind == "data"}            # USD rows
    c = _conclusion("Debt net of cash flow was €682 million, 2.5 times free cash flow.", ["D-028", "D-031"],
                    [("net", "D-028 - D-031", "€682 million"), ("ratio", "D-028 / D-031", "2.5")], [])
    new, notes = draft.fix_currencies(c, verified)
    assert new.text == "Debt net of cash flow was USD 682 million, 2.5 times free cash flow."
    assert [x.display for x in new.computations] == ["USD 682 million", "2.5"] and len(notes) == 1
    assert checks.display_of(Decimal("682000000"), "USD 682 million")[0]
    assert draft.recurrency("USD 5 million", "EUR") == "EUR 5 million" and draft.recurrency("$5 million", "EUR") == "€5 million"


def test_drafter_prompt_asks_for_small_claims():
    s = drafter.SYSTEM
    assert "One claim per conclusion: its text has at most two sentences and at most three numbers" in s
    assert "Write at most three conclusions per criterion" not in s
    assert "Small claims (made-up ids and numbers)" in s and "(c2)" in s
    assert "one per criterion in <criteria>, always" in s
    assert "repeats every computation it needs in itself" in s and "whole chain of computations" in drafter.REVISE_ADVICE
    assert "Never replace a number, a year or a name with a different one" in repairer.SYSTEM


# ---------------------------------------------------------------------------
# Replay: G5b's six failing conclusions through code-only repair (real SHL output, read only)
# ---------------------------------------------------------------------------

G5B_FAILED = ["C-001", "C-002", "C-003", "C-004", "C-008", "C-009"]


def test_g5b_failing_conclusions_replay_through_code_only_repair():
    """Each conclusion as it reached the repairer agent in G5b (text and failing checks from that attempt), given
    to code's removal instead, then to the code checks again. Judges can't be replayed offline, so this counts
    what code alone repairs. Seen when written: all six get a removal and all six pass the code checks."""
    if not G5B_REPORT.is_file():
        pytest.skip("backend/.data/g5b_report.json is not here (recorded run, never committed)")
    rep = m.AgentReport.model_validate_json(G5B_REPORT.read_text(encoding="utf-8"))
    items = {i.id: i for i in rep.items}
    results = {}
    for cid in G5B_FAILED:
        c = items[cid]
        att = next(a for a in c.verification.attempts if (a.note or "").startswith("Repairer:"))
        start = c.model_copy(update={"text": att.before,
                                     "verification": m.Verification(status="fail", checks=att.checks)})
        p = repair.remove_unsupported(start, items)
        if p is None or p.item is None:
            results[cid] = p.action if p else "to the repairer"
            continue
        found = checks.check_conclusion(p.item, items, {}, rubric=RUBRIC)
        assert len(p.item.text) < len(start.text) and repair.REMOVED_NOTE in p.note
        results[cid] = checks.status_from(found)
    print("G5b code-only replay:", results)
    assert all(v in ("pass", "to the repairer", "delete") for v in results.values()), results
    assert sum(v == "pass" for v in results.values()) >= 5, results
