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
