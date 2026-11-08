"""B-reason: draft, verify (code checks and three judges), repair, finalize. Offline (FAKE_MODELS=1).

The data is the mock fixture's verified data items (a fictional company, Northwind Instruments). The
drafter, judges and repairer answer from fakes (simulated). Two drafters are used: the module default
(a template), and FIXTURE_DRAFT below, which plants errors on purpose: a judgment word the data never
states, a market-share claim nothing supports, a display rounded the wrong way and a threshold score that
disagrees with its own input.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from backend.axelrod.agents import drafter, judge, repairer
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.runtime import RunContext, StopRequested
from backend.axelrod.stages import draft, finalize, repair, verify
from backend.axelrod.store import Store

SCREEN = RUBRIC["depths"]["screen"]


# ---------------------------------------------------------------------------
# Fixtures
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
        if s.source_id == "S-004":
            s.status, s.error = "failed", "HTTP 404"
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
    """The four B-reason stages in pipeline order, with run.status between them like the orchestrator."""
    for stage, mod in (("draft", draft), ("verify", verify), ("repair", repair), ("complete", finalize)):
        ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
        await mod.run(ctx)


def by_criterion(state) -> dict[str, m.Extrapolation]:
    return {e.criterion: e for e in state.extrapolations()}


def conclusion_with(state, words: str) -> m.Conclusion:
    hits = [c for c in state.conclusions() if words in (c.verification.attempts[0].before if c.verification.attempts
                                                           else c.text)]
    assert len(hits) == 1, [c.text for c in state.conclusions()]
    return hits[0]


# ---------------------------------------------------------------------------
# A drafter fake that plants errors (simulated)
# ---------------------------------------------------------------------------

PLANTED_ADJECTIVE = "Total debt of $1,150 million was a conservative 2.5 times fiscal 2025 free cash flow of $468 million."
PLANTED_SHARE = "Northwind holds the second-largest share of the laboratory analytical instruments market."
WRONG_DISPLAY = ("Five years of free cash flow at the fiscal 2025 rate would cover the $400 million of scheduled "
                 "principal payments 5.8 times, and the company pays no dividend.")


def _fixture_parts(report_items: dict) -> dict[str, dict]:
    c1 = report_items["C-001"]
    rotic = {"key": "rot", "criteria": ["rotic_level"], "text": c1.text, "data_ids": list(c1.data_ids),
             "computations": [{"name": c.name, "expr": c.expr, "display": c.display} for c in c1.computations]}
    return {
        "rotic_level": {
            "conclusions": [rotic],
            "score": {"criterion": "rotic_level", "score": 1, "confidence": "medium", "conclusion_keys": ["rot"],
                      "rule_input": "rot:rotic_avg",
                      "text": "ROTIC level scores +1 under the framework: the 24.2% average is above the 15% bar."},
            "charts": [{"title": "ROTIC by fiscal year", "criterion": "rotic_level", "chart_type": "line", "unit": "%",
                        "series": [{"name": "ROTIC", "points": [{"x": "FY2024", "ref": "rot:rotic_fy2024"},
                                                                {"x": "FY2025", "ref": "rot:rotic_fy2025"}]}]}]},
        "rotic_vs_peers": {
            "conclusions": [{"key": "share", "criteria": ["rotic_vs_peers"], "text": PLANTED_SHARE,
                             "data_ids": ["D-037"]}],
            "score": {"criterion": "rotic_vs_peers", "score": 1, "confidence": "low", "conclusion_keys": ["share"],
                      "text": "ROTIC vs peers scores +1: the company leads its market."}},
        "secular_growth": {
            "conclusions": [{"key": "mkt", "criteria": ["secular_growth"], "data_ids": ["D-035", "D-036", "D-999"],
                             "text": ("A market report projects 5.4% annual growth for laboratory analytical "
                                      "instruments from 2025 to 2030, and says most industry estimates fall "
                                      "between 5% and 6% per year.")}],
            "score": {"criterion": "secular_growth", "score": 0, "confidence": "medium", "conclusion_keys": ["mkt"],
                      "text": "Secular growth scores 0: market growth estimates of 5% to 6% a year do not point to "
                              "a secular decline."},
            "charts": [{"title": "Revenue, FY2021 to FY2025", "criterion": "secular_growth", "chart_type": "bar",
                        "unit": "USD", "scale": "million",
                        "series": [{"name": "Revenue",
                                    "points": [{"x": f"FY{y}", "ref": f"D-00{y - 2020}"} for y in range(2021, 2026)]
                                    + [{"x": "FY2026", "ref": "D-999"}]}]}]},
        "leverage": {
            "conclusions": [{"key": "debt", "criteria": ["leverage"], "text": PLANTED_ADJECTIVE,
                             "data_ids": ["D-028", "D-031"],
                             "computations": [{"name": "debt_to_fcf", "expr": "D-028 / D-031", "display": "2.5"}]}],
            "score": {"criterion": "leverage", "score": 0, "confidence": "medium", "conclusion_keys": ["debt"],
                      "text": "Leverage scores 0: total debt is 2.5 times annual free cash flow."}},
        "liquidity": {
            "conclusions": [{"key": "cov", "criteria": ["liquidity"], "text": WRONG_DISPLAY,
                             "data_ids": ["D-030", "D-031", "D-033"],
                             "computations": [{"name": "coverage", "expr": "(D-031 * 5) / D-030", "display": "5.8"}]}],
            "score": {"criterion": "liquidity", "score": 0, "confidence": "medium", "conclusion_keys": ["cov"],
                      "rule_input": "cov:coverage", "text": "Liquidity scores 0 under the framework: coverage is 5.8."}},
        "research_stage": {
            "conclusions": [],
            "score": {"criterion": "research_stage", "score": 1, "confidence": "high",
                      "text": "Research stage scores +1: followed closely for years."}},
    }


def fixture_draft_fake(report_items: dict, prompts: list[str] | None = None):
    parts = _fixture_parts(report_items)

    def fake(system: str, user: str, schema: dict) -> dict:
        if prompts is not None:
            prompts.append(user)
        out = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            p = parts[crit["criterion"]]
            out["conclusions"] += p["conclusions"]
            out["scores"].append(p["score"])
            out["charts"] += p.get("charts", [])
        return out

    return fake


def strict_judge(prompts: list[str] | None = None):
    """The module's simulated judge, plus: a market-share claim needs evidence that mentions a share."""
    def fake(system: str, user: str, schema: dict) -> dict:
        if prompts is not None:
            prompts.append(user)
        claim = judge.parse_block(user, "claim") or {}
        evidence = json.dumps(judge.parse_block(user, "evidence") or []).lower()
        text = claim.get("text", "")
        if "share of" in text and "share" not in evidence:
            span = text[text.index("second-largest"):].rstrip(".") if "second-largest" in text else text
            return {"verdict": "UNSUPPORTED", "unsupported_spans": [span], "reason": "Nothing cited states market share."}
        return judge.fake_judge(system, user, schema)

    return fake


@pytest.fixture
def planted(settings, fixture_bundle, fixture_report):
    """A context whose drafter plants errors, with the stricter simulated judge."""
    report_items = {i.id: i for i in fixture_report.items}
    drafts: list[str] = []
    judged: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), fixture_draft_fake(report_items, drafts))
    register_fake(tool_name(judge.JudgeVerdict), strict_judge(judged))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    # The fixture has no peer data, so let the news item serve rotic_vs_peers too: that group then gets a
    # drafter, which plants the market-share claim.
    ctx.state.items["D-037"].criteria = ["secular_growth", "rotic_vs_peers"]
    ctx.test_prompts = {"draft": drafts, "judge": judged}  # type: ignore[attr-defined]
    return ctx


# ---------------------------------------------------------------------------
# The whole reasoning pass on the fixture's verified data
# ---------------------------------------------------------------------------


async def test_default_fakes_produce_a_valid_final_report(settings, fixture_bundle, fixture_report):
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await run_reasoning(ctx)
    state = ctx.state
    report = m.AgentReport.model_validate(json.loads(state.report().model_dump_json()))
    assert report.status == "final" and report.rollup is not None and report.certainty is not None
    assert report.generated_at and report.markdown_path and report.markdown_path.endswith("09-Analysis/agent-report.md")
    assert sorted(by_criterion(state)) == sorted(SCREEN)          # exactly one score per in-scope criterion
    assert len(state.extrapolations()) == len(SCREEN)
    assert all(i.verification.status != "pending" for i in state.items.values())
    assert all(i.certainty is not None for i in state.items.values())
    assert any(e.type == "report.ready" for e in state.events)
    assert state.conclusions() and state.charts()
    # every model call went through the gateway and was metered
    calls = [e for e in state.events if e.type == "gateway.request" and e.data.action == "model.invoke"]
    assert calls and all(e.data.decision == "allowed" for e in calls)
    roles = {state.agents[e.agent_id].role for e in calls}
    assert roles >= {"drafter", "verifier"}


async def test_research_stage_is_a_declared_hole_not_a_guess(planted):
    await run_reasoning(planted)
    state = planted.state
    e = by_criterion(state)["research_stage"]
    assert e.score is None and "coverage history" in (e.na_reason or "")
    assert e.verification.status in ("pass", "fixed")
    hole = next(h for h in state.holes if h.criterion == "research_stage")
    assert hole.item_id == e.id and "coverage history" in hole.reason
    assert "research_stage" in state.rollup.na_criteria


async def test_planted_unsupported_adjective_fails_judges_and_is_downgraded(planted):
    await run_reasoning(planted)
    state = planted.state
    c = conclusion_with(state, "conservative")
    first = c.verification.attempts[0]
    support = next(x for x in first.checks if x.check == "support")
    assert first.status == "fail" and support.result == "fail" and support.method == "llm_judge"
    assert (support.votes_for, support.votes_total) == (0, 3)
    assert support.unsupported_spans == ["conservative"] and support.agent_id == f"verifier-{c.id}"
    assert first.action == "downgrade" and first.before == PLANTED_ADJECTIVE
    assert c.verification.status == "fixed" and "conservative" not in c.text
    assert c.verification.attempts[-1].status == "fixed"
    final = next(x for x in c.verification.checks if x.check == "support")
    assert final.result == "pass" and final.votes_for == 3
    # three isolated judges per round, numbered on: 1 to 3 first, 4 to 6 after the repair
    judges = sorted(a for a in state.agents if a.startswith(f"verifier-{c.id}-"))
    assert judges == [f"verifier-{c.id}-{k}" for k in range(1, 7)]
    assert all(state.agents[a].role == "verifier" and state.agents[a].status == "done" for a in judges)
    assert any(a.role == "repairer" for a in state.agents.values())
    assert c.certainty is not None and c.certainty.band != "none"


async def test_planted_market_share_claim_becomes_a_hole(planted):
    await run_reasoning(planted)
    state = planted.state
    c = conclusion_with(state, "second-largest")
    assert c.verification.status == "fail"
    assert c.verification.attempts[-1].action == "delete"
    assert c.certainty is not None and c.certainty.band == "none"
    assert any(h.item_id == c.id and h.criterion is None for h in state.holes)
    # the score that rested on it loses its support and is not scored
    e = by_criterion(state)["rotic_vs_peers"]
    assert e.score is None and c.id not in e.conclusion_ids
    assert any(a.action == "not_determinable" for a in e.verification.attempts)
    assert any(h.criterion == "rotic_vs_peers" and h.item_id == e.id for h in state.holes)


async def test_code_repairs_recompute_display_and_threshold(planted):
    await run_reasoning(planted)
    state = planted.state
    cov = conclusion_with(state, "payments 5.8 times")
    assert cov.verification.attempts[0].action == "recompute"
    assert cov.computations[0].display == "5.9" and "5.9 times" in cov.text      # 5.85 rounds half up
    assert cov.verification.status == "fixed"
    liq = by_criterion(state)["liquidity"]
    assert liq.score == 1 and liq.verification.status == "fixed"
    assert any(a.action == "recompute" for a in liq.verification.attempts)
    assert next(x for x in liq.verification.checks if x.check == "threshold_rule").result == "pass"
    # leverage only failed because its conclusion had not passed yet: checked again, never rewritten
    lev = by_criterion(state)["leverage"]
    assert lev.verification.status == "fixed" and lev.score == 0
    assert not any(a.action for a in lev.verification.attempts)


async def test_draft_drops_bad_refs_and_assigns_ids_by_code(planted):
    await draft.run(planted)
    state = planted.state
    mkt = conclusion_with(state, "5.4% annual growth")
    assert "D-999" not in mkt.data_ids and mkt.data_ids == ["D-035", "D-036"]
    rev = next(g for g in state.charts() if g.title.startswith("Revenue"))
    assert [p.ref for p in rev.series[0].points] == ["D-001", "D-002", "D-003", "D-004", "D-005"]
    assert [p.y for p in rev.series[0].points] == [1710.0, 1905.0, 2050.0, 2213.0, 2412.0]
    assert (rev.unit, rev.scale) == ("USD", "million")
    rot = next(g for g in state.charts() if g.title.startswith("ROTIC"))
    assert [p.y for p in rot.series[0].points] == [23.3, 25.1] and rot.unit == "%"
    ids = sorted(i.id for i in state.items.values() if i.kind != "data")
    assert ids[0] == "C-001" and all(i.split("-")[0] in ("C", "E", "G") for i in ids)
    # code lowers data coverage for a 10-year criterion with three fiscal years of data
    assert by_criterion(state)["rotic_level"].confidence == "low"
    assert by_criterion(state)["research_stage"].score is None
    warn = [e for e in state.events if e.type == "agent.log" and "Code rejected" in e.data.text]
    assert warn and any("D-999" in e.data.text for e in warn)
    # the drafter saw verified data only: no quotes, no offsets
    prompt = planted.test_prompts["draft"][0]
    assert "char_start" not in prompt and "quote" not in drafter.data_rows(prompt)[0]


async def test_judges_see_only_the_claim_and_its_cited_items(planted):
    await draft.run(planted)
    await verify.run(planted)
    state = planted.state
    c = conclusion_with(state, "conservative")
    prompts = [p for p in planted.test_prompts["judge"] if "conservative" in p]
    assert len(prompts) == 3 and len(set(prompts)) == 1
    cited = {e["id"] for e in judge.parse_block(prompts[0], "evidence")}
    assert cited == set(c.data_ids) == {"D-028", "D-031"}
    assert "<rubric>" not in prompts[0]
    score_prompts = [p for p in planted.test_prompts["judge"] if '"kind": "score"' in p]
    assert score_prompts and all("<rubric>" in p for p in score_prompts)


async def test_rollup_matches_hand_computation(planted):
    """After repair: rotic_level +1, secular_growth 0, leverage 0, liquidity +1, rotic_vs_peers and
    research_stage N/A. Compounder (2) mean 0.5, Financial Strength (1) mean 0.5, so
    weighted average = (2 x 0.5 + 1 x 0.5) / 3 = 0.50, normalized (0.5 + 1) / 2 = 75%, percent of max 50%."""
    await run_reasoning(planted)
    r = planted.state.rollup
    cats = {c.category: c for c in r.categories}
    assert cats["compounder"].score == 0.5 and cats["compounder"].scored == 2
    assert cats["financial_strength"].score == 0.5
    assert cats["dominant_business"].score is None and cats["dominant_business"].na_criteria == ["rotic_vs_peers"]
    assert (r.weighted_average, r.normalized_pct, r.percent_of_max_pct) == (0.5, 75, 50)
    assert (r.scored, r.in_scope, r.na_criteria) == (4, 6, ["rotic_vs_peers", "research_stage"])
    rc = planted.state.report_certainty
    assert (rc.coverage_scored, rc.coverage_in_scope) == (4, 6)
    assert rc.high + rc.medium + rc.low + rc.none == len(planted.state.items)


def _e(criterion: str, score: int | None, status: str = "pass") -> m.Extrapolation:
    crit = RUBRIC["criteria"][criterion]
    return m.Extrapolation(id=f"E-{criterion}", category=crit["category"], criterion=criterion, score=score,
                           na_reason=None if score is not None else "n/a", text="t", conclusion_ids=[],
                           rule=m.ScoreRule(type=crit["scoring"]), confidence="high",
                           verification=m.Verification(status=status))


def test_rollup_screen_example():
    """dominant (2) -1.0, compounder (2) 0.5, financial (1) 0.5: (-2 + 1 + 0.5) / 5 = -0.10, normalized 45%."""
    es = [_e("rotic_vs_peers", -1), _e("rotic_level", 1), _e("secular_growth", 0), _e("leverage", 0),
          _e("liquidity", 1), _e("research_stage", None), _e("leverage", 1, status="fail")]
    r = finalize.build_rollup(es, SCREEN)
    assert (r.weighted_average, r.normalized_pct, r.percent_of_max_pct) == (-0.1, 45, 0)
    assert r.na_criteria == ["research_stage"] and r.scored == 5


def test_rollup_rounds_half_up():
    """Full depth, five categories scored. Compounder mean 2/3 shows 0.7. Weighted:
    (2 x 0.5 + 2 x 0.7 + 2 x -1.0 + 1 x 0.0 + 1 x 1.0) / 8 = 1.4 / 8 = 0.175, which rounds half up to 0.18
    (Python's round gives 0.17). normalized (1.18 / 2) x 100 = 59, percent of max 18."""
    es = [_e("strategy", 1), _e("demand_side", 0), _e("rotic_level", 1), _e("secular_growth", 1),
          _e("capital_allocation", 0), _e("governance", -1), _e("competitor_behavior", 0), _e("leverage", 1)]
    r = finalize.build_rollup(es, finalize.in_scope("full"))
    cats = {c.category: c.score for c in r.categories}
    assert cats["compounder"] == 0.7 and cats["dominant_business"] == 0.5
    assert (r.weighted_average, r.normalized_pct, r.percent_of_max_pct) == (0.18, 59, 18)
    assert r.scored == 8 and r.in_scope == 27 and len(r.na_criteria) == 19


async def test_holes_cover_failed_sources_and_stay_stable(planted):
    await run_reasoning(planted)
    state = planted.state
    src = [h for h in state.holes if h.criterion is None and h.item_id is None]
    assert len(src) == 1 and src[0].reason.startswith("S-004") and "HTTP 404" in src[0].reason
    ids = [h.hole_id for h in state.holes]
    assert ids == sorted(ids) and len(set(ids)) == len(ids) and ids[0] == "HOLE-01"
    before = {(h.hole_id, h.criterion, h.item_id) for h in state.holes}
    finalize.finalize_report(planted)                       # a second finalize keeps the same ids
    assert {(h.hole_id, h.criterion, h.item_id) for h in state.holes} == before


async def test_complete_stage_writes_vault_files(planted):
    await run_reasoning(planted)
    state, vault = planted.state, planted.vault
    report = m.AgentReport.model_validate_json(vault.get_text(planted.run_id, finalize.AGENT_JSON))
    assert report.status == "final" and len(report.items) == len(state.items)
    md = vault.get_text(planted.run_id, finalize.AGENT_MD)
    assert "## Holes" in md and "Weighted average: **+0.50**" in md
    data = json.loads(vault.get_text(planted.run_id, finalize.DATA_CHECKS))
    reasoning = json.loads(vault.get_text(planted.run_id, finalize.REASONING_CHECKS))
    assert len(data["items"]) == len(state.data_items())
    assert {x["id"] for x in reasoning["items"]} == {i.id for i in state.items.values() if i.kind != "data"}
    assert state.summary is None                            # no brief in this test, so no human-report files
    with pytest.raises(Exception):
        vault.get_text(planted.run_id, finalize.HUMAN_MD)


async def test_drafter_failure_leaves_every_criterion_na(settings, fixture_bundle, fixture_report):
    register_fake(tool_name(drafter.DraftOutput), lambda s, u, sc: {"conclusions": "nope"})
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    es = by_criterion(ctx.state)
    assert sorted(es) == sorted(SCREEN) and all(e.score is None for e in es.values())
    assert "failed validation" in es["leverage"].na_reason
    assert not ctx.state.conclusions()
    assert all(a.status == "failed" for a in ctx.state.agents.values() if a.role == "drafter")


async def test_stop_halts_the_judges(planted):
    await draft.run(planted)
    planted.gateway.set_kill_switch("test stop")
    with pytest.raises(StopRequested):
        await verify.run(planted)
    assert not any(e.type == "gateway.request" and e.data.decision == "allowed" and e.data.role == "verifier"
                   for e in planted.state.events)


async def test_blocked_conclusion_is_left_alone(planted):
    """A revoked source behind a conclusion blocks it. Repair leaves it, finalize declares it."""
    await draft.run(planted)
    src = next(s for s in planted.state.sources if s.source_id == "S-005")
    src.status = "revoked"
    await verify.run(planted)
    state = planted.state
    mkt = conclusion_with(state, "5.4% annual growth")
    assert mkt.verification.status == "blocked"
    await repair.run(planted)
    assert state.items[mkt.id].verification.status == "blocked" and not state.items[mkt.id].verification.attempts
    assert any(h.item_id == mkt.id for h in state.holes)


def test_tally_two_of_three():
    c = m.Conclusion(id="C-001", category="compounder", criteria=["rotic_level"], text="Revenue rose strongly.",
                     data_ids=["D-001"], verification=m.Verification(status="pending"))
    yes = judge.JudgeVerdict(verdict="SUPPORTED", reason="ok")
    no = judge.JudgeVerdict(verdict="UNSUPPORTED", unsupported_spans=["strongly", "not in claim"], reason="adds strength")
    r = verify.tally(c, [yes, yes, no], "m")
    assert (r.result, r.votes_for, r.votes_total) == ("pass", 2, 3) and "Dissent" in r.detail
    r = verify.tally(c, [yes, no, no], "m")
    assert (r.result, r.votes_for) == ("fail", 1) and r.unsupported_spans == ["strongly"]
    r = verify.tally(c, [yes, None, None], "m")
    assert r.result == "blocked"                             # a missing vote never counts as support
    r = verify.tally(c, [no, None, None], "m")
    assert r.result == "blocked"                             # two missing votes could still have passed it
    r = verify.tally(c, [no, no, None], "m")
    assert r.result == "fail"


def test_redisplay_keeps_the_format():
    assert repair.redisplay(Decimal("5.85"), "5.8") == "5.9"
    assert repair.redisplay(Decimal("0.24245"), "24.0%") == "24.2%"
    assert repair.redisplay(Decimal("1234500000"), "€1,000.0 million") == "€1,234.5 million"
    assert repair.replace_number("cover 2.5 times, not 12.5%", "2.5", "2.6") == "cover 2.6 times, not 12.5%"


async def test_complete_stage_writes_the_brief_after_summarize(planted):
    """Full order from draft to complete, with B-brief's summarize stages (fakes, simulated) in between."""
    from backend.axelrod.stages import summarize, verify_summary

    stages = (("draft", draft), ("verify", verify), ("repair", repair), ("summarize", summarize),
              ("verify_summary", verify_summary), ("complete", finalize))
    for stage, mod in stages:
        planted.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
        try:
            await mod.run(planted)
        except NotImplementedError:
            pytest.skip(f"{stage} is not built yet")
    state = planted.state
    assert state.summary is not None
    md = planted.vault.get_text(planted.run_id, finalize.HUMAN_MD)
    assert md.startswith("# ") and state.summary.markdown_path and state.summary.markdown_path.endswith(finalize.HUMAN_MD)
    brief = m.HumanReport.model_validate_json(planted.vault.get_text(planted.run_id, finalize.HUMAN_JSON))
    assert brief.markdown_path == state.summary.markdown_path
    reasoning = json.loads(planted.vault.get_text(planted.run_id, finalize.REASONING_CHECKS))
    assert reasoning["sentences"]


async def test_orchestrator_runs_the_reasoning_stages(planted, monkeypatch):
    from backend.axelrod import orchestrator

    monkeypatch.setattr(orchestrator, "STAGES", [("draft", "draft"), ("verify", "verify"), ("repair", "repair"),
                                                 ("complete", "finalize")])
    await orchestrator.run_pipeline(planted)
    state = planted.state
    assert state.run.status == "done", state.run.error
    assert state.report_status() == "final" and state.report().rollup.weighted_average == 0.5
    types = [e.type for e in state.events]
    assert types.index("report.ready") < types.index("run.completed")


async def test_holes_for_failed_extractor_pages_and_data_items(planted):
    """A page whose extractor failed gave no data, and a data item still failing after extract's recite
    is never cited by a draft. Both are declared as holes."""
    from backend.axelrod.agents.base import AgentRunner

    runner = AgentRunner(planted, "extractor", "Extract S-002 page 3", ["S-002"], "small")
    planted.emit("agent.log", m.AgentLogData(text="extract_page blocked by the gateway: budget"),
                 message="blocked", agent_id=runner.agent_id, level="warn")
    runner.finish("failed")
    d = planted.state.items["D-024"]
    d.verification = m.Verification(status="fail", checks=[m.CheckResult(
        check="value_in_quote", method="code", result="fail", detail="'42,000' is not in the corrected quote")])
    await run_reasoning(planted)
    state = planted.state
    assert all("D-024" not in c.data_ids for c in state.conclusions())
    assert state.items["D-024"].verification.status == "fail"           # never repaired by the reasoning stages
    hole = next(h for h in state.holes if h.item_id == "D-024")
    assert "value_in_quote" in hole.reason
    agent_hole = next(h for h in state.holes if h.reason.startswith(runner.agent_id))
    assert "S-002 page 3" in agent_hole.reason and "budget" in agent_hole.reason
    before = [h.hole_id for h in state.holes]
    finalize.finalize_report(planted)
    assert [h.hole_id for h in state.holes] == before


# ---------------------------------------------------------------------------
# B-fix-draft: compute on a short series instead of refusing, and declare every derived number
# ---------------------------------------------------------------------------


def _fictional(base: m.DataItem, iid: str, statement: str, number: float, period: str) -> m.DataItem:
    """A fictional data item for a test (simulated), copied from a fixture item so its citations validate."""
    v = base.value.model_copy(update={"number": number, "as_reported": str(int(number)), "period": period})
    return base.model_copy(update={"id": iid, "statement": statement, "value": v})


_ROW_KINDS = (("ebit", "operating income"), ("amort", "amortization"), ("assets", "total assets"),
              ("cash", "cash and cash"), ("goodwill", "goodwill"), ("nibcl", "non-interest-bearing"))


def _row_kind(row: dict) -> str | None:
    s = row["statement"].lower()
    return next((k for k, w in _ROW_KINDS if w in s), None)


def series_draft_fake(prompts: list[str] | None = None):
    """Simulated drafter that follows the multi-year rules: ROTIC for every fiscal year the rows allow, a mean
    over those years as the rule input, the score from the bar, and confidence medium (code must lower it).
    It computes only from expr_value in the rows, so the test also shows that value is enough to compute with."""
    from backend.axelrod.verify import compute
    from backend.axelrod.verify.numbers import half_up

    def amount(x: Decimal) -> str:
        return f"${Decimal(half_up(x / 1000000, 1)):,} million"

    def pct(x: Decimal) -> str:
        return f"{half_up(x * 100, 1)}%"

    def fake(system: str, user: str, schema: dict) -> dict:
        if prompts is not None:
            prompts.append(user)
        rows = drafter.data_rows(user)
        env = {r["id"]: Decimal(r["expr_value"]) for r in rows if "expr_value" in r}
        by: dict[tuple[str, int], str] = {}
        tax = None
        for r in rows:
            if r.get("unit") == "%" and "tax" in r["statement"].lower():
                tax = r["id"]
            kind, period = _row_kind(r), str(r.get("period") or "")
            if kind and period.startswith("FY"):
                by.setdefault((kind, int(period[2:6])), r["id"])
        comps: list[dict] = []
        cited: list[str] = [tax] if tax else []

        def add(name: str, expr: str, show) -> None:
            env[name] = compute.safe_eval(expr, env)
            comps.append({"name": name, "expr": expr, "display": show(env[name])})

        def ticap(y: int) -> str | None:
            ids = [by.get((k, y)) for k in ("assets", "cash", "goodwill", "nibcl")]
            if None in ids:
                return None
            if f"ticap_fy{y}" not in env:
                cited.extend(ids)
                add(f"ticap_fy{y}", " - ".join(ids), amount)
            return f"ticap_fy{y}"

        years = []
        for y in sorted({y for k, y in by if k == "ebit"}):
            ebit, amort = by.get(("ebit", y)), by.get(("amort", y))
            start, end = ticap(y - 1), ticap(y)
            if not (ebit and amort and tax and start and end):
                continue
            cited.extend([ebit, amort])
            add(f"nopat_fy{y}", f"({ebit} + {amort}) * (1 - {tax})", amount)
            add(f"rotic_fy{y}", f"nopat_fy{y} / (({start} + {end}) / 2)", pct)
            years.append(y)
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            cid = crit["criterion"]
            if cid != "rotic_level" or not years:
                out["scores"].append({"criterion": cid, "score": None, "text": "Not scored.", "confidence": "low",
                                      "na_reason": "Not part of this simulated test draft."})
                continue
            names = [f"rotic_fy{y}" for y in years]
            add("rotic_mean", f"({' + '.join(names)}) / {len(names)}", pct)
            mean = env["rotic_mean"]
            score = 1 if mean > Decimal("0.15") else -1 if mean < Decimal("0.10") else 0
            display = {c["name"]: c["display"] for c in comps}
            shown = ", ".join(f"{display[n]} in FY{y}" for n, y in zip(names, years))
            out["conclusions"].append({
                "key": "rot", "criteria": ["rotic_level"], "data_ids": list(dict.fromkeys(cited)),
                "computations": comps,
                "text": f"ROTIC was {shown}, a mean of {display['rotic_mean']} over three fiscal years, using the "
                        f"21% normalized tax rate."})
            out["scores"].append({"criterion": "rotic_level", "score": score, "confidence": "medium",
                                  "conclusion_keys": ["rot"], "rule_input": "rot:rotic_mean",
                                  "text": f"ROTIC level scores {score:+d} under the framework on three fiscal years: "
                                          f"the {display['rotic_mean']} mean is above the 15% bar."})
        return out

    return fake


@pytest.fixture
def three_years(settings, fixture_bundle, fixture_report):
    """The fixture plus fictional FY2023 EBIT and amortization and FY2022 balances (simulated test data), so
    ROTIC can be computed for three fiscal years: FY2023, FY2024 and FY2025."""
    prompts: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), series_draft_fake(prompts))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    items = ctx.state.items
    extra = [
        _fictional(items["D-006"], "D-101", "Operating income (GAAP EBIT) for FY2023 was $420 million.", 420e6, "FY2023"),
        _fictional(items["D-008"], "D-102", "Amortization of acquired intangibles for FY2023 was $62 million.", 62e6, "FY2023"),
        _fictional(items["D-010"], "D-103", "Total assets for FY2022 was $4,700 million.", 4700e6, "FY2022"),
        _fictional(items["D-013"], "D-104", "Cash and cash equivalents for FY2022 was $600 million.", 600e6, "FY2022"),
        _fictional(items["D-016"], "D-105", "Goodwill for FY2022 was $1,840 million.", 1840e6, "FY2022"),
        _fictional(items["D-019"], "D-106", "Accounts payable and accrued liabilities (non-interest-bearing current "
                                            "liabilities) for FY2022 was $610 million.", 610e6, "FY2022"),
    ]
    for d in extra:
        items[d.id] = d
    ctx.test_prompts = prompts  # type: ignore[attr-defined]
    return ctx


async def test_three_year_series_is_scored_with_low_coverage(three_years):
    """A 3-year ROTIC series gets a score, not N/A. The score follows from the bar on the declared mean, and
    code lowers the data coverage (the drafter said medium), which lowers the certainty."""
    await run_reasoning(three_years)
    state = three_years.state
    e = by_criterion(state)["rotic_level"]
    assert e.score is not None and e.na_reason is None
    c = state.items[e.rule.input.split(":")[0]]
    assert e.rule.input.endswith(":rotic_mean") and c.id in e.conclusion_ids
    assert [x.name for x in c.computations if x.name.startswith("rotic_fy")] == [
        "rotic_fy2023", "rotic_fy2024", "rotic_fy2025"]
    # (380.78 / 1720 + 418.7 / 1795 + 464.52 / 1847.5) / 3 = 23.5%, above the 15% bar: +1 by the rubric
    assert next(x.display for x in c.computations if x.name == "rotic_mean") == "23.5%" and e.score == 1
    assert next(x for x in e.verification.checks if x.check == "threshold_rule").result == "pass"
    assert c.verification.status == "pass" and e.verification.status == "pass"
    assert e.confidence == "low"                                 # lowered by code, never raised
    assert e.certainty.completeness < 1.0 and "data coverage low" in e.certainty.basis
    assert "rotic_level" not in state.rollup.na_criteria
    logs = [ev.data.text for ev in state.events if ev.type == "agent.log"]
    assert any("data coverage lowered from medium to low (four of ten fiscal years: 2022, 2023, 2024, 2025)" in t
               for t in logs)
    assert any(t.startswith("Code noted: rotic_level: the score rests on data from four of ten fiscal years")
               for t in logs)
    # the drafter got the rubric's metric definitions, the plan's needs for its criteria and the years asked
    prompt = three_years.test_prompts[0]
    crit = {x["criterion"]: x for x in judge.parse_block(prompt, "criteria")}
    assert crit["rotic_level"]["years_asked"] == 10 and "not N/A" in crit["rotic_level"]["short_series"]
    assert "<metrics>" in prompt and "NOPAT / average TICAP" in prompt
    needs = [json.loads(x) for x in prompt.split("<needs>\n")[1].split("\n</needs>")[0].splitlines()]
    assert [n["need_id"] for n in needs] == ["N-01"] and needs[0]["question"]   # the fixture's ROTIC need only


async def test_short_series_coverage_counts_only_the_rule_inputs_years(three_years):
    """Coverage counts the years behind the computation the bar is applied to, not every year cited."""
    await draft.run(three_years)
    state = three_years.state
    c = next(iter(state.conclusions()))
    one_year = draft.inputs_of(c, "rotic_fy2025")
    assert set(one_year) == {"D-007", "D-009", "D-022", "D-011", "D-014", "D-017", "D-020",
                             "D-012", "D-015", "D-018", "D-021"}
    assert {state.items[i].value.period for i in one_year} == {"FY2024", "FY2025", None}   # the tax rate has none
    assert set(draft.inputs_of(c, "rotic_mean")) == set(c.data_ids)
    assert draft.inputs_of(c, "no_such_name") == []
    b = draft.Builder(three_years, {d.id: d for d in state.data_items()})
    b.conclusions = {c.id: c}
    assert b._years([c.id], f"{c.id}:rotic_fy2025") == {2024, 2025}
    assert b._years([c.id], None) == {2022, 2023, 2024, 2025}
    assert b._coverage("rotic_level", "high", [c.id], f"{c.id}:rotic_fy2025") == (
        "low", "rotic_level: data coverage lowered from high to low (two of ten fiscal years: 2024, 2025)")
    assert b._coverage("leverage", "high", [c.id]) == ("high", None)          # not a 10-year criterion


def test_rows_carry_code_computed_expression_values(fixture_report):
    """expr_value is what an id means in an expr: base units, a percentage as a fraction, the sign as reported."""
    items = {i.id: i for i in fixture_report.items}
    assert draft.data_row(items["D-028"])["expr_value"] == "1150000000"      # 1,150 USD million
    assert draft.data_row(items["D-022"])["expr_value"] == "0.21"            # 21%
    tax = items["D-028"].model_copy(update={"value": items["D-028"].value.model_copy(
        update={"number": -403.0, "as_reported": "(403)"})})
    assert draft.data_row(tax)["expr_value"] == "-403000000"
    assert "expr_value" not in draft.data_row(items["D-027"])                # a fact with no number
    assert "quote" not in draft.data_row(items["D-028"])


def test_row_cap_keeps_the_best_tiers_in_id_order(fixture_report):
    data = [i for i in fixture_report.items if i.kind == "data"]
    mine = [d for d in data if "secular_growth" in d.criteria]
    rows, left = draft.group_rows(data, ["secular_growth"])
    assert left == 0 and [r["id"] for r in rows] == [d.id for d in mine]
    rows, left = draft.group_rows(data, ["secular_growth"], limit=3)
    assert len(rows) == 3 and left == len(mine) - 3
    kept = [r["id"] for r in rows]
    assert kept == sorted(kept)
    rank = {d.id: draft._TIER_RANK[d.source_tier] for d in mine}
    assert all(rank[d.id] >= max(rank[k] for k in kept) for d in mine if d.id not in kept)


SUM_TEXT = "Scheduled principal payments of $400 million and the $500 million revolving facility add up to $900 million."


def sums_draft_fake(system: str, user: str, schema: dict) -> dict:
    """Simulated drafter: the same sum stated twice, once undeclared and once as a declared computation."""
    out: dict = {"conclusions": [], "scores": [], "charts": []}
    for crit in judge.parse_block(user, "criteria") or []:
        if crit["criterion"] == "liquidity":
            out["conclusions"] += [
                {"key": "bad", "criteria": ["liquidity"], "data_ids": ["D-030", "D-032"], "text": SUM_TEXT},
                {"key": "good", "criteria": ["liquidity"], "data_ids": ["D-030", "D-032"], "text": SUM_TEXT,
                 "computations": [{"name": "principal_and_facility", "expr": "D-030 + D-032",
                                   "display": "$900 million"}]}]
        out["scores"].append({"criterion": crit["criterion"], "score": None, "text": "Not scored.",
                              "confidence": "low", "na_reason": "Not part of this simulated test draft."})
    return out


async def test_undeclared_sum_fails_numbers_traced_and_declared_one_passes(settings, fixture_bundle, fixture_report):
    register_fake(tool_name(drafter.DraftOutput), sums_draft_fake)
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    await verify.run(ctx)
    bad, good = ctx.state.conclusions()
    assert bad.text == good.text and not bad.computations and good.computations
    traced = {c.id: next(x for x in c.verification.checks if x.check == "numbers_traced") for c in (bad, good)}
    assert traced[bad.id].result == "fail" and "'$900 million'" in traced[bad.id].detail
    assert traced[bad.id].unsupported_spans == ["$900 million"]
    assert traced[good.id].result == "pass"
    assert next(x for x in good.verification.checks if x.check == "computation").result == "pass"
    assert bad.verification.status == "fail" and good.verification.status == "pass"


def test_typed_amounts_are_found_and_small_constants_allowed():
    assert draft.typed_amounts("D-115 + 393000000 - D-123") == ["393000000"]   # the R0 rehearsal's add-back
    assert draft.typed_amounts("(1 - 0.25) * D-007") == ["0.25"]               # an assumed tax rate
    assert draft.typed_amounts("(D-011 + D-012) / 2") == []
    assert draft.typed_amounts("(D-031 * 5) / D-030") == []
    assert draft.typed_amounts("rotic_fy2025 * 100 - 1") == []


COVERAGE_BAD = ("Free cash flow of $468 million covers the $400 million of scheduled principal payments 1.3 times, "
                "and principal plus the $500 million facility total $900 million.")
COVERAGE_GOOD = ("Free cash flow of $468 million covers the $400 million of scheduled principal payments 1.2 times, "
                 "and principal plus the $500 million facility total $900 million.")


def revising_draft_fake(prompts: list[str], *, revision_ok: bool = True):
    """Simulated drafter: a first draft with a wrong display (468 / 400 is 1.17, shown as 1.3) and an undeclared
    sum. Shown code's pre-check, it returns the corrected draft (or, with revision_ok False, an invalid one)."""
    def fake(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        revising = "<code_check>" in user
        if revising and not revision_ok:
            return {"conclusions": "nope"}
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            if crit["criterion"] != "liquidity":
                out["scores"].append({"criterion": crit["criterion"], "score": None, "text": "Not scored.",
                                      "confidence": "low", "na_reason": "Not part of this simulated test draft."})
                continue
            comps = [{"name": "coverage", "expr": "D-031 / D-030", "display": "1.2" if revising else "1.3"}]
            if revising:
                comps.append({"name": "principal_and_facility", "expr": "D-030 + D-032", "display": "$900 million"})
            out["conclusions"].append({"key": "cov", "criteria": ["liquidity"], "data_ids": ["D-030", "D-031", "D-032"],
                                       "text": COVERAGE_GOOD if revising else COVERAGE_BAD, "computations": comps})
            out["scores"].append({"criterion": "liquidity", "score": 0, "confidence": "medium",
                                  "conclusion_keys": ["cov"], "rule_input": "cov:coverage",
                                  "text": f"Liquidity scores 0 under the framework: coverage is "
                                          f"{'1.2' if revising else '1.3'}, between the 1x and 2x bars."})
        return out

    return fake


def _drafter_of(state, criterion: str) -> m.Agent:
    return next(a for a in state.agents.values() if a.role == "drafter" and criterion in a.task)


async def test_code_precheck_sends_the_draft_back_once(settings, fixture_bundle, fixture_report):
    """Code runs the verifier's own checks on the draft first. The drafter sees what failed, with code's value
    for the wrong display, and revises once. The verify stage then checks everything again."""
    prompts: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), revising_draft_fake(prompts))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    state = ctx.state
    liq = [p for p in prompts if '"criterion": "liquidity"' in p]
    assert len(liq) == 2 and "<code_check>" not in liq[0] and "<code_check>" in liq[1]
    block = liq[1].split("<code_check>\n")[1].split("\n</code_check>")[0]
    found = [json.loads(x) for x in block.splitlines()]
    assert {p["check"] for p in found} == {"computation", "numbers_traced"}
    assert any("rounds half up to 1.2" in p["problem"] and p["conclusion"] == "cov" for p in found)
    untraced = next(p for p in found if "'$900 million'" in p["problem"])
    assert untraced["where"] == ["...al plus the $500 million facility total $900 million...."]
    assert "Formulas belong in expr" in untraced["fix"]
    assert "C-9" not in liq[1].split("<code_check>")[1]          # temporary ids never reach the model
    agent = _drafter_of(state, "liquidity")
    calls = [e for e in state.events if e.type == "gateway.request" and e.agent_id == agent.agent_id]
    assert len(calls) == 2 and agent.status == "done"
    logs = [e.data.text for e in state.events if e.type == "agent.log" and e.agent_id == agent.agent_id]
    assert any(t.startswith("Code pre-check found 2 problem(s)") for t in logs)
    assert any(t.startswith("Revised draft: 0 pre-check problem(s) left of 2") for t in logs)
    await verify.run(ctx)
    c = next(iter(state.conclusions()))
    assert c.text == COVERAGE_GOOD and c.verification.status == "pass"
    assert by_criterion(state)["liquidity"].verification.status == "pass"


def test_fill_displays_uses_code_values_for_revised_computations(fixture_report):
    """A computation the drafter changed in its revision gets code's display, in its own format, in the
    conclusion and in the score that cites it. One sent back unchanged stays for the checks to catch."""
    verified = {i.id: i for i in fixture_report.items if i.kind == "data"}
    out = drafter.DraftOutput.model_validate({
        "conclusions": [{"key": "cov", "criteria": ["liquidity"], "data_ids": ["D-030", "D-031", "D-032"],
                         "text": "Coverage is 1.3 times, and principal plus the facility total $905 million.",
                         "computations": [{"name": "coverage", "expr": "D-031 / D-030", "display": "1.3"},
                                          {"name": "total", "expr": "D-030 + D-032", "display": "$905 million"}]}],
        "scores": [{"criterion": "liquidity", "score": 0, "conclusion_keys": ["cov"], "rule_input": "cov:coverage",
                    "text": "Liquidity scores 0: coverage is 1.3."}]})
    got, notes = draft.fill_displays(out, verified, set(verified))
    assert got.conclusions[0].text == "Coverage is 1.2 times, and principal plus the facility total $900 million."
    assert [x.display for x in got.conclusions[0].computations] == ["1.2", "$900 million"]
    assert got.scores[0].text == "Liquidity scores 0: coverage is 1.2."
    assert notes == ["cov:coverage 1.3 -> 1.2", "cov:total $905 million -> $900 million"]
    kept, notes = draft.fill_displays(out, verified, set(verified), skip={("cov", "coverage", "D-031 / D-030", "1.3")})
    assert [x.display for x in kept.conclusions[0].computations] == ["1.3", "$900 million"] and len(notes) == 1


async def test_invalid_revision_keeps_the_first_draft(settings, fixture_bundle, fixture_report):
    prompts: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), revising_draft_fake(prompts, revision_ok=False))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    await verify.run(ctx)
    state = ctx.state
    c = next(iter(state.conclusions()))
    assert c.text == COVERAGE_BAD and c.verification.status == "fail"          # caught by verify, as before
    agent = _drafter_of(state, "liquidity")
    assert agent.status == "done"
    assert any(e.type == "agent.log" and e.data.text.startswith("Revision not used") for e in state.events)


async def test_typed_amount_computation_is_dropped_by_code(settings, fixture_bundle, fixture_report):
    def fake(system: str, user: str, schema: dict) -> dict:
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            if crit["criterion"] == "liquidity":
                out["conclusions"].append({
                    "key": "t", "criteria": ["liquidity"], "data_ids": ["D-031"],
                    "text": "Free cash flow of $468 million less a $100 million cushion leaves $368 million.",
                    "computations": [{"name": "after_cushion", "expr": "D-031 - 100000000", "display": "$368 million"}]})
            out["scores"].append({"criterion": crit["criterion"], "score": None, "text": "Not scored.",
                                  "confidence": "low", "na_reason": "Not part of this simulated test draft."})
        return out

    register_fake(tool_name(drafter.DraftOutput), fake)
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    c = next(iter(ctx.state.conclusions()))
    assert c.computations == []
    assert any(e.type == "agent.log" and "after_cushion dropped (it types 100000000" in e.data.text
               for e in ctx.state.events)


def test_precheck_gives_the_unrounded_value_behind_a_threshold(settings, fixture_bundle, fixture_report):
    """A display such as "10.0%" can hide a value just under a bar, so the pre-check names the exact value."""
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    verified = {d.id: d for d in draft.usable_data(ctx.state)}
    out = drafter.DraftOutput.model_validate({
        "conclusions": [{"key": "cov", "criteria": ["liquidity"], "data_ids": ["D-030", "D-031"],
                         "text": "Free cash flow covers scheduled principal 1.2 times.",
                         "computations": [{"name": "coverage", "expr": "D-031 / D-030", "display": "1.2"}]}],
        "scores": [{"criterion": "liquidity", "score": 1, "confidence": "medium", "conclusion_keys": ["cov"],
                    "rule_input": "cov:coverage", "text": "Liquidity scores +1: coverage is 1.2."}]})
    found = draft.precheck(ctx.state, ["leverage", "liquidity"], out, verified, set(verified))
    thr = next(p for p in found if p.get("check") == "threshold_rule")
    assert thr["score"] == "liquidity" and thr["exact_value"] == "1.17"
    assert "cov:coverage" in thr["problem"] and "C-9" not in thr["problem"]


def test_revision_merges_only_what_changed():
    D = drafter.DraftOutput.model_validate
    old = D({"conclusions": [{"key": "a", "text": "A.", "data_ids": ["D-001"]},
                             {"key": "b", "text": "B.", "data_ids": ["D-002"]},
                             {"key": "c", "text": "C.", "data_ids": ["D-003"]}],
             "scores": [{"criterion": "leverage", "score": 0, "text": "L."},
                        {"criterion": "liquidity", "score": 1, "text": "Q."}],
             "charts": [{"title": "T", "unit": "%", "series": [{"name": "s", "points": [{"x": "FY2025", "ref": "D-001"}]}]}]})
    new = D({"conclusions": [{"key": "b", "text": "B fixed.", "data_ids": ["D-002"]},
                             {"key": "c", "text": "", "data_ids": []}],
             "scores": [{"criterion": "liquidity", "score": 0, "text": "Q fixed."}], "charts": []})
    got = draft.merge_revision(old, new)
    assert [(c.key, c.text) for c in got.conclusions] == [("a", "A."), ("b", "B fixed.")]
    assert [(s.criterion, s.score, s.text) for s in got.scores] == [("leverage", 0, "L."), ("liquidity", 0, "Q fixed.")]
    assert [g.title for g in got.charts] == ["T"]


def test_concepts_for_the_inputs_index():
    """Statements from the R0 rehearsal's SHL and SEC items (real text), and lines that must stay out."""
    assert draft.concept_of("Siemens Healthineers' income before income taxes was €1,928 million in FY2023.") == "pretax_income"
    assert draft.concept_of("Income tax expenses were €403 million in fiscal year 2023.") == "income_tax"
    assert draft.concept_of("Earnings before interest and taxes were €2,135 million in fiscal year 2023.") == "ebit"
    assert draft.concept_of("Amortization, depreciation and other effects from IFRS 3 purchase price allocation "
                            "adjustments were −€393 million in FY2023.") == "amortization"
    assert draft.concept_of("GE HealthCare short-term debt and current maturities of long-term debt at Dec 31, 2025 "
                            "were USD 508 million (us-gaap DebtCurrent, 10-K).") == "short_term_debt"
    assert draft.concept_of("Non-controlling interests were €16 million in fiscal year 2023.") == "nci_income"
    assert draft.concept_of("Non-controlling interests were €51 million as of September 30, 2025.") is None
    assert draft.concept_of("Total segments' adjusted EBIT was €3,302 million in FY2023.") is None
    assert draft.concept_of("Current liabilities to the Siemens Group were €3,192 million as of September 30, 2025.") is None
    assert draft.concept_of("Trade payables due in fiscal year 2026 were €2,277 million.") is None
    src = lambda t: m.Source.model_validate({**_fixture_source(), "title": t})  # noqa: E731
    assert draft.company_of(src("SEC XBRL company facts: GE HealthCare (peer)"), "Siemens Healthineers") == "GE HealthCare"
    assert draft.company_of(src("SEC XBRL company facts (peer: Harbor Scientific)"), "Northwind") == "Harbor Scientific"
    assert draft.company_of(src("Form 10-K, fiscal 2025"), "Northwind") == "Northwind"
    assert draft.company_of(None, "Northwind") == "Northwind"


def _fixture_source() -> dict:
    from mock import build_fixtures
    return dict(build_fixtures.build()["sources"][0])


async def test_inputs_index_pairs_year_end_balances(three_years):
    """One line per company and fiscal year, from code, so the drafter pairs the right year-ends."""
    state = three_years.state
    mine, _ = draft.group_items(draft.usable_data(state), ["rotic_level"])
    idx = {(r["company"], r["fiscal_year"]): r for r in draft.inputs_index(state, mine)}
    who = state.run.company
    assert sorted(y for c, y in idx if c == who) == [2022, 2023, 2024, 2025]
    assert idx[(who, 2022)] == {"company": who, "fiscal_year": 2022, "total_assets": ["D-103"], "cash": ["D-104"],
                                "goodwill": ["D-105"], "trade_payables": ["D-106"]}
    assert idx[(who, 2025)]["ebit"] == ["D-007"] and idx[(who, 2025)]["amortization"] == ["D-009"]
    # an amortization reported as a deduction, as in SHL's reports: the index flags its sign
    neg = [d.model_copy(update={"value": d.value.model_copy(update={"number": -62e6})}) if d.id == "D-102" else d
           for d in mine]
    flagged = {(r["company"], r["fiscal_year"]): r for r in draft.inputs_index(state, neg)}
    assert flagged[(who, 2023)]["reported_negative"] == ["D-102"]
    assert all("reported_negative" not in r for k, r in flagged.items() if k != (who, 2023))
    await draft.run(three_years)
    prompt = three_years.test_prompts[0]
    lines = [json.loads(x) for x in prompt.split("<inputs_by_year>\n")[1].split("\n</inputs_by_year>")[0].splitlines()]
    assert [(r["company"], r["fiscal_year"]) for r in lines] == sorted(idx)
    secular = [p for p in three_years.test_prompts if '"criterion": "secular_growth"' in p]
    assert all("<inputs_by_year>" not in p for p in secular)        # only groups that compute returns get it


def test_drafter_prompt_asks_to_compute_and_declare():
    """The rules the R0 rehearsal showed were missing: compute on a short series, declare every derived number
    (with a worked example), and read signs and scales from expr_value."""
    s = drafter.SYSTEM
    assert "Never return a null score only because the series is shorter" in s
    assert "Return null only when not even one fiscal year of the metric can be computed" in s
    assert "must be declared as a computation" in s and "Worked example" in s
    assert '"expr": "-D-043 / D-044"' in s and "expr_value" in s
    assert "latest fiscal year they have in common" in s
