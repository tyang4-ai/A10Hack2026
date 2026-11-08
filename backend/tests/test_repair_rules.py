"""B-fix-repair: repair only weakens, and never contradicts code's threshold recomputation. Offline
(FAKE_MODELS=1). The judges and the repairer answer from fakes registered here (simulated).

The case behind it was seen live: a threshold input shown as 10.0% was 9.996% before rounding. Code
recomputed rotic_level as -1 against "< 10%", the repairer moved it back to 0, and the score failed and
became N/A. The data items below are fictional copies of fixture items (simulated), sized so that
NOPAT / TICAP is exactly 9.996%.
"""
from __future__ import annotations

import pytest

from backend.axelrod.agents import judge, repairer
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import finalize, repair, verify
from backend.axelrod.store import Store
from backend.axelrod.verify import checks

ROTIC_TEXT_0 = "ROTIC level (10y) scores 0 under the framework: the 10.0% value sits between the bars."
LEVERAGE_TEXT = "Leverage scores -1: total debt is 2.5 times annual free cash flow."


# ---------------------------------------------------------------------------
# Fixtures (fictional data, simulated)
# ---------------------------------------------------------------------------


def _register_defaults() -> None:
    judge.register_fakes()
    repairer.register_fakes()


@pytest.fixture(autouse=True)
def _default_fakes():
    """Every test starts and ends with the module fakes, whatever another test registered."""
    _register_defaults()
    yield
    _register_defaults()


def _fictional(base: m.DataItem, iid: str, statement: str, number: int) -> m.DataItem:
    """A fictional data item copied from a fixture item, so its citations still validate (simulated)."""
    v = base.value.model_copy(update={"number": number, "as_reported": f"{number:,}", "period": "FY2025"})
    return base.model_copy(update={"id": iid, "statement": f"{statement} (fictional)", "value": v})


def _score(eid: str, criterion: str, score: int | None, text: str, cids: list[str], *,
           rule_input: str | None = None, confidence: str = "medium") -> m.Extrapolation:
    crit = RUBRIC["criteria"][criterion]
    return m.Extrapolation(id=eid, category=crit["category"], criterion=criterion, score=score, text=text,
                           conclusion_ids=cids, confidence=confidence,
                           rule=m.ScoreRule(type=crit["scoring"], plus_one=crit["plus_one"],
                                            minus_one=crit["minus_one"], input=rule_input),
                           verification=m.Verification(status="pending"))


def reasoning_items(fixture_report: m.AgentReport) -> dict[str, m.Item]:
    """The fixture's data plus fictional rows: ROTIC 9.996% (shown 10.0%) and liquidity coverage 0.996 (shown
    1.0x), and two passing conclusions on them, plus the fixture's leverage conclusion."""
    items: dict[str, m.Item] = {i.id: i for i in fixture_report.items if i.kind == "data"}
    base = items["D-028"]                                                    # USD million, S-002
    items["D-901"] = _fictional(base, "D-901", "NOPAT for FY2025 was $9,996 million.", 9996)
    items["D-902"] = _fictional(base, "D-902", "Average TICAP for FY2025 was $100,000 million.", 100000)
    items["D-903"] = _fictional(base, "D-903", "Five-year free cash flow was $996 million.", 996)
    items["D-904"] = _fictional(base, "D-904", "Maturities, facilities and dividends were $1,000 million.", 1000)
    ok = m.Verification(status="pass")
    items["C-001"] = m.Conclusion(
        id="C-001", category="compounder", criteria=["rotic_level"], data_ids=["D-901", "D-902"], verification=ok,
        text="ROTIC was 10.0% in fiscal 2025.",
        computations=[m.Computation(name="rotic_fy2025", expr="D-901 / D-902", result=0.09996, display="10.0%")])
    items["C-002"] = m.Conclusion(
        id="C-002", category="financial_strength", criteria=["leverage"], data_ids=["D-028", "D-031"],
        verification=ok, text="Total debt of $1,150 million was 2.5 times fiscal 2025 free cash flow of $468 million.",
        computations=[m.Computation(name="debt_to_fcf", expr="D-028 / D-031", result=2.457265, display="2.5")])
    items["C-003"] = m.Conclusion(
        id="C-003", category="financial_strength", criteria=["liquidity"], data_ids=["D-903", "D-904"],
        verification=ok, text="Free cash flow covers maturities, facilities and dividends 1.0x.",
        computations=[m.Computation(name="coverage", expr="D-903 / D-904", result=0.996, display="1.0x")])
    return items


def rotic(score: int | None = 0, text: str = ROTIC_TEXT_0, confidence: str = "low") -> m.Extrapolation:
    return _score("E-001", "rotic_level", score, text, ["C-001"], rule_input="C-001:rotic_fy2025",
                  confidence=confidence)


def leverage(score: int | None = -1, confidence: str = "medium") -> m.Extrapolation:
    return _score("E-002", "leverage", score, LEVERAGE_TEXT, ["C-002"], confidence=confidence)


@pytest.fixture
def ctx(settings, fixture_bundle, fixture_report) -> RunContext:
    """A real Store, Vault, Gateway and RunContext with the fixture's sources and the items above."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Northwind Instruments (fixture)", ticker="NWI-X"))
    c = RunContext.create(state, store, store.vault, settings)
    c.emit("run.created", {"run": state.run}, message="created", stage="plan")
    sources = []
    for raw in fixture_bundle["sources"]:
        s = m.Source.model_validate(raw)
        s.approval, s.status = "approved", "extracted"
        sources.append(s)
    state.sources = sources
    state.as_of = "2026-11-08"
    state.items.update(reasoning_items(fixture_report))
    c.emit("run.status", {"status": "running", "stage": "repair"}, stage="repair", message="Stage repair")
    return c


def _check(item: m.Item, name: str) -> m.CheckResult:
    return next(x for x in item.verification.checks if x.check == name)


def _repairers(state) -> list[m.Agent]:
    return [a for a in state.agents.values() if a.role == "repairer"]


def reverting_repairer(prompts: list[str]):
    """Simulated repairer doing what the live one did: it reads the rounded 10.0% and moves the score to 0."""
    def fake(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        return {"action": "downgrade", "score": 0, "confidence": "low", "text": ROTIC_TEXT_0,
                "note": "The displayed 10.0% is not below 10%, so the score is 0 (simulated repair)."}
    return fake


# ---------------------------------------------------------------------------
# The 9.996% case
# ---------------------------------------------------------------------------


async def test_value_rounding_onto_the_bar_keeps_code_score(ctx):
    """Code recomputes -1, and its sentence says the value is below the bar before rounding. A judge that reads
    only the rounded display (as the live judges did) then has nothing to object to, so no repairer is needed."""
    def literal_judge(system: str, user: str, schema: dict) -> dict:
        text = (judge.parse_block(user, "claim") or {}).get("text", "")
        if "value 10.0% meets the -1 bar" in text:
            return {"verdict": "UNSUPPORTED", "unsupported_spans": ["meets the -1 bar (< 10%)"],
                    "reason": "10.0% is not below 10% (simulated judge)."}
        return judge.fake_judge(system, user, schema)

    prompts: list[str] = []
    register_fake(tool_name(judge.JudgeVerdict), literal_judge)
    register_fake(tool_name(repairer.RepairOutput), reverting_repairer(prompts))
    state = ctx.state
    state.items["E-001"] = rotic(0)
    book = finalize.CertaintyBook(state)
    assert await verify.verify_item(ctx, "E-001", book) == "fail"
    thr = _check(state.items["E-001"], "threshold_rule")
    assert thr.result == "fail" and "-1, not 0" in thr.detail
    await repair.repair_item(ctx, "E-001", book)
    e = state.items["E-001"]
    assert e.score == -1 and e.verification.status == "fixed"
    assert "shows as 10.0% after rounding, but before rounding it is below 10%" in e.text
    assert _check(e, "threshold_rule").result == "pass" and _check(e, "numbers_traced").result == "pass"
    assert _check(e, "support").result == "pass"
    first = e.verification.attempts[0]
    assert first.action == "recompute" and "below 10% before rounding" in first.note
    assert not prompts and not _repairers(state)


async def test_repairer_moving_code_score_back_is_refused(ctx):
    """The live sequence: judges reject the recomputed -1 once, the repairer proposes 0. Code refuses it, keeps
    -1 with its own sentence, records the refusal, and the re-check passes."""
    calls = {"n": 0}

    def judge_rejects_first_round(system: str, user: str, schema: dict) -> dict:
        calls["n"] += 1
        if calls["n"] <= verify.JUDGES:
            return {"verdict": "UNSUPPORTED", "unsupported_spans": [], "reason": "10.0% is not below 10% (simulated)."}
        return judge.fake_judge(system, user, schema)

    prompts: list[str] = []
    register_fake(tool_name(judge.JudgeVerdict), judge_rejects_first_round)
    register_fake(tool_name(repairer.RepairOutput), reverting_repairer(prompts))
    state = ctx.state
    state.items["E-001"] = rotic(0)
    book = finalize.CertaintyBook(state)
    await verify.verify_item(ctx, "E-001", book)
    await repair.repair_item(ctx, "E-001", book)
    e = state.items["E-001"]
    assert e.score == -1 and e.verification.status == "fixed"
    assert [a.action for a in e.verification.attempts] == ["recompute", "recompute", None]
    refusal = e.verification.attempts[1].note
    assert refusal.startswith("Code refused the repairer's score 0") and "stays -1" in refusal
    assert e.text == repair.code_score(e, state.items).text and "sits between" not in e.text
    assert _check(e, "threshold_rule").result == "pass"
    # the repairer was told code's score
    assert len(prompts) == 1
    block = judge.parse_block(prompts[0], "code_score")
    assert block["score"] == -1 and block["rule_input"] == "C-001:rotic_fy2025" and block["display"] == "10.0%"


def test_code_sentence_for_a_ratio_bar_traces_every_number(fixture_report):
    """liquidity "< 1x": 0.996 shows as 1.0x, still below 1x before rounding, so -1. The sentence uses only the
    display and the rubric's own numbers, so numbers_traced passes."""
    items = reasoning_items(fixture_report)
    e = _score("E-003", "liquidity", 0, "Liquidity scores 0: coverage is 1.0x.", ["C-003"],
               rule_input="C-003:coverage")
    cs = repair.code_score(e, items)
    assert cs.score == -1 and "before rounding it is below 1x" in cs.text
    new = e.model_copy(update={"score": cs.score, "text": cs.text})
    found = {c.check: c.result for c in checks.check_extrapolation(new, items, RUBRIC)}
    assert found["threshold_rule"] == "pass" and found["numbers_traced"] == "pass"


def test_display_that_shows_the_side_gets_the_plain_sentence(fixture_report):
    items = reasoning_items(fixture_report)
    items["C-001"].computations[0].display = "9.996%"
    cs = repair.code_score(rotic(0), items)
    assert cs.score == -1 and cs.text.endswith("the recomputed value 9.996% meets the -1 bar (< 10%).")
    assert cs.shown_as == "9.996%"


def test_judgment_and_worded_bars_have_no_code_score(fixture_report):
    items = reasoning_items(fixture_report)
    assert repair.code_score(leverage(), items) is None                 # judgment
    assert repair.numeric_bars("research_stage") is None                # worded threshold bars
    assert repair.numeric_bars("rotic_level") is not None and repair.numeric_bars("liquidity") is not None


def test_stale_threshold_failure_with_the_right_score_is_not_given_up(fixture_report):
    """threshold_rule failed earlier, but the stated score already matches code: re-check, never N/A."""
    items = reasoning_items(fixture_report)
    e = rotic(-1, text="ROTIC level (10y) scores -1.")
    prop = repair.recompute_threshold(e, items)
    assert prop.action == "recompute" and prop.item.score == -1 and "stands" in prop.note
    assert prop.item.text == repair.code_score(e, items).text


# ---------------------------------------------------------------------------
# What the repairer may change (threshold scores)
# ---------------------------------------------------------------------------


def _out(**kw) -> repairer.RepairOutput:
    kw.setdefault("note", "Weakened (simulated).")
    return repairer.RepairOutput.model_validate(kw)


def test_threshold_raise_is_refused_and_code_score_stays(fixture_report):
    items = reasoning_items(fixture_report)
    cs_text = repair.code_score(rotic(-1), items).text
    e = rotic(-1, text=cs_text, confidence="medium")
    prop = repair._apply_extrapolation(e, _out(action="downgrade", score=1, confidence="high",
                                               text="ROTIC level (10y) scores +1."), items)
    assert prop.action == "recompute" and prop.item.score == -1 and prop.item.text == cs_text
    assert prop.item.confidence == "medium"                              # a raise in coverage is refused too
    assert "Code refused the repairer's score +1" in prop.note and "Code kept data coverage medium" in prop.note


def test_threshold_lowering_edit_is_accepted(fixture_report):
    items = reasoning_items(fixture_report)
    e = rotic(-1, text=repair.code_score(rotic(-1), items).text, confidence="medium")
    narrower = "ROTIC level (10y) scores -1 under the framework, on one fiscal year of data."
    prop = repair._apply_extrapolation(e, _out(action="downgrade", score=-1, confidence="low", text=narrower), items)
    assert prop.action == "downgrade" and prop.item.score == -1
    assert prop.item.confidence == "low" and prop.item.text == narrower and "refused" not in prop.note


def test_threshold_score_may_still_become_na(fixture_report):
    items = reasoning_items(fixture_report)
    prop = repair._apply_extrapolation(rotic(-1), _out(action="not_determinable", na_reason="One year only."), items)
    assert prop.action == "not_determinable" and prop.item.score is None and prop.item.na_reason == "One year only."


def test_threshold_no_weaker_version_resubmits_code_sentence(fixture_report):
    items = reasoning_items(fixture_report)
    e = rotic(-1, text="ROTIC level (10y) scores -1 because of the drafter's wording.")
    prop = repair._apply_extrapolation(e, _out(action="downgrade", score=-1, text=e.text), items)
    assert prop.action == "recompute" and prop.item.score == -1
    assert prop.item.text == repair.code_score(e, items).text


# ---------------------------------------------------------------------------
# What the repairer may change (judgment scores)
# ---------------------------------------------------------------------------


def test_judgment_score_moving_away_from_zero_is_refused(fixture_report):
    items = reasoning_items(fixture_report)
    for old, new in ((-1, 1), (0, -1), (0, 1), (1, -1)):
        prop = repair._apply_extrapolation(leverage(old), _out(action="downgrade", score=new,
                                                               text="Leverage scores differently."), items)
        assert prop.item.score is None and prop.action == "not_determinable", (old, new)
        assert f"Code refused the repair: {repair._signed(old)} to {repair._signed(new)}" in prop.note


def test_judgment_lowering_edit_is_accepted(fixture_report):
    items = reasoning_items(fixture_report)
    softer = "Leverage scores 0: total debt is 2.5 times annual free cash flow."
    prop = repair._apply_extrapolation(leverage(-1), _out(action="downgrade", score=0, confidence="low",
                                                          text=softer), items)
    assert prop.action == "downgrade" and prop.item.score == 0
    assert prop.item.confidence == "low" and prop.item.text == softer


def test_judgment_coverage_is_never_raised(fixture_report):
    items = reasoning_items(fixture_report)
    prop = repair._apply_extrapolation(leverage(-1, confidence="low"),
                                       _out(action="downgrade", score=-1, confidence="high",
                                            text="Leverage scores -1: debt is 2.5 times free cash flow."), items)
    assert prop.item.score == -1 and prop.item.confidence == "low" and "Code kept data coverage low" in prop.note


def test_na_never_becomes_a_score():
    assert not repair.judgment_move_ok(None, 0) and not repair.judgment_move_ok(None, 1)
    assert repair.judgment_move_ok(None, None) and repair.judgment_move_ok(-1, 0) and repair.judgment_move_ok(1, 1)
    assert not repair.judgment_move_ok(-1, 1)


# ---------------------------------------------------------------------------
# Blocked items are left alone
# ---------------------------------------------------------------------------


async def test_item_resting_on_a_blocked_source_is_left_alone(ctx):
    """citations_resolve blocked (a revoked source behind it) plus another failing check: status fail, but
    rewording cannot help, so repair never touches it."""
    prompts: list[str] = []
    register_fake(tool_name(repairer.RepairOutput), reverting_repairer(prompts))
    state = ctx.state
    e = leverage(-1)
    e.verification = m.Verification(status="fail", checks=[
        m.CheckResult(check="citations_resolve", method="code", result="blocked", detail="source revoked: S-002"),
        m.CheckResult(check="numbers_traced", method="code", result="fail", detail="'3.1' does not match")])
    state.items[e.id] = e
    await repair.repair_item(ctx, e.id, finalize.CertaintyBook(state))
    after = state.items[e.id]
    assert after.verification.status == "fail" and not after.verification.attempts and after.score == -1
    assert not prompts and not _repairers(state)
