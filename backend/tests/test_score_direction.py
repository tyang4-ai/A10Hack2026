"""B-fix-score: a score's direction must follow from its text and the rubric, and Bobby's plan gate. Offline
(FAKE_MODELS=1) unless marked live.

The score and conclusion texts below are real: rotic_vs_peers (E-002) and the three ROTIC conclusions it cites,
copied from the pre-R2 run run_20261108_5zqdvb on EC2 (backend/.data/r2a_report.json). That run scored +1 on a
text that says Siemens Healthineers "trails both peers". The judges, Bobby and the repairer answer from fakes
(simulated) unless a test registers its own. Tests that read the full export (backend/.data/r2a_export, read
only, never committed) skip when it is missing. The live tests re-judge E-002 with Haiku and re-run the plan gate
with the recorded digest, on a copy in a short DATA_DIR (backend/.data/fixscore). Run with -s to see them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.axelrod import gates
from backend.axelrod.agents import bobby, drafter, judge, repairer
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import repair, verify
from backend.axelrod.stages.finalize import CertaintyBook
from backend.axelrod.store import Store
from backend.axelrod.store.state import RunState
from backend.axelrod.verify import checks

REPO = Path(__file__).resolve().parents[2]
EXPORT = REPO / "backend" / ".data" / "r2a_export"
JUDGE = tool_name(judge.JudgeVerdict)
REPAIR = tool_name(repairer.RepairOutput)
GATE = tool_name(bobby.GateCall)


@pytest.fixture(autouse=True)
def _fakes():
    """Every test starts and ends with the module fakes (simulated)."""
    for mod in (judge, repairer, bobby, drafter):
        mod.register_fakes()
    yield
    for mod in (judge, repairer, bobby, drafter):
        mod.register_fakes()


# ---------------------------------------------------------------------------
# Real text from run_20261108_5zqdvb (pre-R2, EC2)
# ---------------------------------------------------------------------------

E002_TEXT = ("For fiscal year 2025, Siemens Healthineers ROTIC was 11.5% (c_sh_fy2025_rotic), compared with GE "
             "HealthCare's 24.6% (c_ge_fy2025) and Philips's 16.8% (c_phi_fy2025), both computed the same way but "
             "from peers' own reported currencies and fiscal years ending three months later; Siemens Healthineers "
             "trails both peers on this measure.")

DATA_IDS = {"C-005": ["D-164", "D-128", "D-209", "D-131", "D-376", "D-274", "D-334", "D-442", "D-394", "D-295", "D-356"],
            "C-008": ["D-005", "D-010", "D-020", "D-015", "D-023", "D-027", "D-032", "D-036", "D-040", "D-024", "D-028",
                      "D-033", "D-037", "D-041"],
            "C-010": ["D-047", "D-053", "D-065", "D-059", "D-071", "D-078", "D-085", "D-092", "D-099", "D-072", "D-079",
                      "D-086", "D-093", "D-100"]}
CONCLUSIONS = {
    "C-005": ("Siemens Healthineers ROTIC in fiscal year 2025 was 11.5%, computed the same way, with EBIT plus purchase "
              "price allocation amortization less tax at the year's effective rate, over average total assets less "
              "cash, goodwill and trade payables at the start and end of the year.",
              [("tax_rate_fy25", "-D-209/D-131", 0.24044865054328776, "24.0%"),
               ("nopat_fy25", "D-164 - D-128 - tax_rate_fy25*(D-164-D-128)", 2666784787.942517, "€2,667 million"),
               ("ticap_start_fy25", "D-394 - D-295 - D-356 - D-442", 23414000000.0, "€23,414 million"),
               ("ticap_end_fy25", "D-376 - D-274 - D-334 - D-442", 22775000000.0, "€22,775 million"),
               ("avg_ticap_fy25", "(ticap_start_fy25 + ticap_end_fy25)/2", 23094500000.0, "€23,095 million"),
               ("rotic_fy25", "nopat_fy25/avg_ticap_fy25", 0.1154727224205987, "11.5%")]),
    "C-008": ("GE HealthCare ROTIC in fiscal year 2025, computed the same way, was 24.6%.",
              [("tax_rate_ge25", "D-020/D-015", 0.2218208092485549, "22.2%"),
               ("nopat_ge25", "D-005 + D-010 - tax_rate_ge25*(D-005+D-010)", 2376559248.5549135, "USD 2,377 million"),
               ("ticap_start_ge25", "D-023 - D-027 - D-032 - (D-036-D-040)", 9028000000.0, "USD 9,028 million"),
               ("ticap_end_ge25", "D-024 - D-028 - D-033 - (D-037-D-041)", 10328000000.0, "USD 10,328 million"),
               ("avg_ticap_ge25", "(ticap_start_ge25+ticap_end_ge25)/2", 9678000000.0, "USD 9,678 million"),
               ("rotic_ge25", "nopat_ge25/avg_ticap_ge25", 0.245563055234027, "24.6%")]),
    "C-010": ("Philips ROTIC in fiscal year 2025, computed the same way, was 16.8%.",
              [("tax_rate_phi25", "D-065/D-059", 0.23857868020304568, "23.9%"),
               ("nopat_phi25", "D-047 + D-053 - tax_rate_phi25*(D-047+D-053)", 1437563451.7766497, "€1,438 million"),
               ("ticap_start_phi25", "D-071 - D-078 - D-085 - (D-092-D-099)", 8572000000.0, "€8,572 million"),
               ("ticap_end_phi25", "D-072 - D-079 - D-086 - (D-093-D-100)", 8521000000.0, "€8,521 million"),
               ("avg_ticap_phi25", "(ticap_start_phi25+ticap_end_phi25)/2", 8546500000.0, "€8,547 million"),
               ("rotic_phi25", "nopat_phi25/avg_ticap_phi25", 0.16820493205132508, "16.8%")]),
}


def conclusion(cid: str) -> m.Conclusion:
    text, comps = CONCLUSIONS[cid]
    return m.Conclusion(id=cid, category="dominant_business", criteria=["rotic_vs_peers"], text=text, data_ids=DATA_IDS[cid],
                        computations=[m.Computation(name=n, expr=x, result=r, display=d) for n, x, r, d in comps],
                        agent_id="drafter-01", verification=m.Verification(status="pass"))


def score(value: int | None, text: str = E002_TEXT, *, status: str = "pending") -> m.Extrapolation:
    crit = RUBRIC["criteria"]["rotic_vs_peers"]
    return m.Extrapolation(id="E-002", category="dominant_business", criterion="rotic_vs_peers", score=value,
                           text=text, conclusion_ids=["C-005", "C-008", "C-010"], confidence="low",
                           rule=m.ScoreRule(type=crit["scoring"], plus_one=crit["plus_one"],
                                            minus_one=crit["minus_one"], input=None),
                           agent_id="drafter-01", verification=m.Verification(status=status))


def make_ctx(settings, value: int | None = 1) -> RunContext:
    """A real Store, Vault, Gateway and RunContext holding E-002 and its three conclusions (simulated run)."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers", ticker="SHL"))
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    for cid in CONCLUSIONS:
        state.items[cid] = conclusion(cid)
    state.items["E-002"] = score(value)
    ctx.emit("run.status", {"status": "running", "stage": "verify"}, stage="verify", message="Stage verify")
    return ctx


def recording_judge(seen: list[tuple[str, str]], answer: dict | None = None):
    def fake(system: str, user: str, schema: dict) -> dict:  # simulated judge that records its view
        seen.append((system, user))
        return answer if answer is not None else judge.fake_judge(system, user, schema)
    return fake


# ---------------------------------------------------------------------------
# Task 1: the judges see the score, the rubric and the text, and answer both questions
# ---------------------------------------------------------------------------


async def test_the_judge_view_shows_score_rubric_and_text_and_asks_both_questions(settings) -> None:
    ctx = make_ctx(settings, 1)
    seen: list[tuple[str, str]] = []
    register_fake(JUDGE, recording_judge(seen))
    await verify.judge_claim(ctx, ctx.state.items["E-002"])
    assert len(seen) == 3 and len({u for _, u in seen}) == 1        # three isolated judges, one view
    system, user = seen[0]
    claim = judge.parse_block(user, "claim")
    assert (claim["kind"], claim["score"], claim["score_shown"], claim["scoring"]) == ("score", 1, "+1", "judgment")
    assert claim["text"] == E002_TEXT and claim["company"] == "Siemens Healthineers"
    assert claim["score_set_by"] == verify.SET_BY["judgment"]
    rub = judge.parse_block(user, "rubric")
    entry = RUBRIC["criteria"]["rotic_vs_peers"]
    for key in ("label", "scoring", "plus_one", "minus_one", "evidence_hint"):
        assert rub[key] == entry[key], key
    assert rub["direction"] == verify.DIRECTION_JUDGMENT and "behind peers, or weak, is -1" in rub["direction"]
    assert {e["id"] for e in judge.parse_block(user, "evidence")} == set(CONCLUSIONS)
    assert user.rstrip().endswith(judge.SCORE_QUESTION)
    assert "does the score follow from the text and the cited conclusions under the rubric" in user
    assert "cannot carry +1" in system and 'Set score_direction to "follows" or "wrong"' in system
    assert "leave unsupported_spans empty unless the text itself is unsupported" in system


async def test_a_conclusion_is_still_asked_one_question_without_a_rubric(settings) -> None:
    ctx = make_ctx(settings, 1)
    seen: list[tuple[str, str]] = []
    register_fake(JUDGE, recording_judge(seen))
    await verify.judge_claim(ctx, ctx.state.items["C-008"])
    user = seen[0][1]
    assert user.rstrip().endswith(judge.QUESTION) and "<rubric>" not in user
    assert "company" not in judge.parse_block(user, "claim")


def test_threshold_and_na_scores_show_how_they_were_set() -> None:
    crit = RUBRIC["criteria"]["rotic_level"]
    e = m.Extrapolation(id="E-001", category="compounder", criterion="rotic_level", score=-1, text="ROTIC level scores -1.",
                        conclusion_ids=["C-005"], confidence="low",
                        rule=m.ScoreRule(type="threshold", plus_one=crit["plus_one"], minus_one=crit["minus_one"],
                                         input="C-005:rotic_fy25"),
                        verification=m.Verification(status="pending"))
    view = verify.claim_view(e)
    assert (view["score_shown"], view["scoring"], view["score_set_by"]) == ("-1", "threshold", verify.SET_BY["threshold"])
    assert verify.rubric_view("rotic_level")["direction"] == verify.DIRECTION_THRESHOLD
    assert verify.claim_view(score(None))["score_shown"] == "N/A" and verify.claim_view(score(0))["score_shown"] == "0"


# ---------------------------------------------------------------------------
# Task 5: +1 with "trails both peers" fails support, -1 with the same text passes (simulated judges)
# ---------------------------------------------------------------------------


async def test_plus_one_with_trails_both_peers_fails_support(settings) -> None:
    ctx = make_ctx(settings, 1)
    e = ctx.state.items["E-002"]
    assert checks.status_from(verify.code_checks(ctx.state, e)) == "pass"   # code alone lets it through
    found = await verify.run_checks(ctx, e)
    res = found[-1]
    assert (res.check, res.method, res.result) == ("support", "llm_judge", "fail")
    assert (res.votes_for, res.votes_total) == (0, 3) and res.agent_id == "verifier-E-002"
    assert "Score direction wrong (+1)" in res.detail and "'trails'" in res.detail
    assert res.unsupported_spans == []        # the text is right, the score is wrong: nothing to cut from it


async def test_minus_one_with_the_same_text_passes(settings) -> None:
    ctx = make_ctx(settings, -1)
    res = (await verify.run_checks(ctx, ctx.state.items["E-002"]))[-1]
    assert (res.check, res.result, res.votes_for, res.votes_total) == ("support", "pass", 3, 3)


async def test_zero_with_the_same_text_is_left_to_the_judges(settings) -> None:
    """Code's direction words only catch a sign that contradicts the text. A 0 goes to the judges' rubric
    question (mixed or unclear is 0), which the simulated judge does not model."""
    assert judge.direction_conflict(0, E002_TEXT, "Siemens Healthineers") is None
    ctx = make_ctx(settings, 0)
    res = (await verify.run_checks(ctx, ctx.state.items["E-002"]))[-1]
    assert res.result == "pass"


def test_a_supported_verdict_with_a_wrong_direction_counts_against() -> None:
    """The judge's own direction answer wins over a slip in its verdict, for scores only."""
    e = score(1)
    slip = judge.JudgeVerdict(verdict="SUPPORTED", score_direction="wrong",
                              reason="The score +1 does not follow: the company trails both peers.")
    ok = judge.JudgeVerdict(verdict="SUPPORTED", score_direction="follows", reason="Matches.")
    res = verify.tally(e, [slip, slip, ok], "haiku")
    assert (res.result, res.votes_for, res.votes_total) == ("fail", 1, 3)
    assert res.detail.startswith("Not supported: 1 of 3") and "Score direction wrong (+1): The score +1" in res.detail
    passed = verify.tally(e, [ok, ok, slip], "haiku")
    assert passed.result == "pass" and passed.votes_for == 2 and "Score direction wrong" in passed.detail
    c = conclusion("C-008")                       # a conclusion's stray direction answer means nothing
    assert verify.tally(c, [slip, slip, ok], "haiku").result == "pass"


def test_the_verdict_parses_odd_direction_answers_without_a_retry() -> None:
    v = judge.JudgeVerdict.model_validate({"verdict": "UNSUPPORTED", "score_direction": " Wrong ", "reason": "x"})
    assert v.score_direction == "wrong"
    for odd in ("n/a", "", "not a score", None):
        assert judge.JudgeVerdict.model_validate({"verdict": "SUPPORTED", "score_direction": odd,
                                                  "reason": "x"}).score_direction is None
    assert judge.JudgeVerdict.model_validate({"verdict": "SUPPORTED", "reason": "x"}).score_direction is None


# ---------------------------------------------------------------------------
# Code's direction words (coarse: drafter pre-check and the simulated judge)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, way", [
    (E002_TEXT, "down"),
    ("Siemens Healthineers is ahead of both peers on ROTIC.", "up"),
    ("Both peers trail Siemens Healthineers on ROTIC.", "up"),               # the company is the object
    ("GE HealthCare is ahead of Siemens Healthineers.", "down"),
    ("Siemens Healthineers does not trail its peers.", None),              # negated: unclear
    ("Revenue growth was strong, but margins were weaker than in FY2024.", "mixed"),
    ("ROTIC was 11.5%, below the 15% bar and above the 10% bar.", None),    # no metric-free direction word
    ("The company's ROTIC lags both peers.", "down"),
])
def test_text_direction(text: str, way: str | None) -> None:
    assert judge.text_direction(text, "Siemens Healthineers AG")[0] == way


def test_company_words_drop_legal_suffixes_and_labels() -> None:
    assert judge.company_words("Siemens Healthineers AG") == {"siemens", "company"}
    assert judge.company_words("Northwind Instruments (fixture)") == {"northwind", "company"}
    assert judge.company_words(None) == {"company"}


def test_direction_conflict_is_only_a_contradicted_sign() -> None:
    co = "Siemens Healthineers"
    found = judge.direction_conflict(1, E002_TEXT, co)
    assert found and "+1" in found and "'trails'" in found
    assert judge.direction_conflict(-1, E002_TEXT, co) is None
    assert judge.direction_conflict(-1, "Siemens Healthineers outperforms both peers.", co)
    assert judge.direction_conflict(1, "Strong growth, weaker margins.", co) is None     # mixed
    assert judge.direction_conflict(None, E002_TEXT, co) is None


# ---------------------------------------------------------------------------
# Task 2: the drafter's prompt rule and the pre-check helper
# ---------------------------------------------------------------------------


def test_drafter_prompt_says_score_and_text_must_agree() -> None:
    assert "The score and its text must say the same thing." in drafter.SYSTEM
    assert "cannot carry +1" in drafter.SYSTEM and "mixed or unclear is 0" in drafter.SYSTEM


def test_direction_problems_send_a_contradiction_back_without_saying_which_way() -> None:
    def ds(crit: str, value: int | None, text: str = E002_TEXT) -> drafter.DraftScore:
        return drafter.DraftScore(criterion=crit, score=value, text=text, conclusion_keys=["c1"])

    scores = [ds("rotic_vs_peers", 1), ds("secular_growth", -1, "Secular growth is strong."),
              ds("leverage", -1), ds("rotic_level", 1),          # threshold: code sets it, not checked here
              ds("liquidity", 0), ds("research_stage", None)]
    probs = drafter.direction_problems(scores, None, "Siemens Healthineers AG")
    assert [(p["score"], p["check"]) for p in probs] == [("rotic_vs_peers", "score_direction"),
                                                          ("secular_growth", "score_direction")]
    first = probs[0]["problem"]
    assert first.startswith("The score is +1, but the text says 'trails'") and drafter.DIRECTION_FIX in first
    assert "-1" not in first and "should be" not in first     # names the conflict, never the answer
    assert drafter.direction_problems(scores, ["leverage"], "Siemens Healthineers AG") == []


# ---------------------------------------------------------------------------
# Task 3: repair stays weaken-only after a direction failure
# ---------------------------------------------------------------------------


async def judged_then_repaired(ctx: RunContext) -> m.Extrapolation:
    book = CertaintyBook(ctx.state)
    assert await verify.verify_item(ctx, "E-002", book) == "fail"
    await repair.repair_item(ctx, "E-002", book)
    return ctx.state.items["E-002"]


async def test_a_direction_failure_goes_na_with_the_default_repairer(settings) -> None:
    """The simulated repairer has no span to cut (the judges left them empty), so the score is not determinable."""
    e = await judged_then_repaired(make_ctx(settings, 1))
    assert e.score is None and e.verification.status in ("fixed", "pass")
    assert e.verification.attempts[0].action == "not_determinable"


async def test_repair_never_flips_the_sign(settings) -> None:
    def flipper(system: str, user: str, schema: dict) -> dict:  # simulated repairer that tries -1
        return {"action": "downgrade", "text": E002_TEXT, "keep_ids": [], "score": -1, "confidence": "low",
                "note": "The company trails both peers, so -1 (simulated repair)."}

    register_fake(REPAIR, flipper)
    e = await judged_then_repaired(make_ctx(settings, 1))
    assert e.score is None and "moves away from 0" in e.verification.attempts[0].note


async def test_repair_may_move_toward_zero(settings) -> None:
    def to_zero(system: str, user: str, schema: dict) -> dict:  # simulated repairer that weakens to 0
        return {"action": "downgrade", "text": E002_TEXT, "keep_ids": [], "score": 0, "confidence": "low",
                "note": "Weakened to 0 (simulated repair)."}

    register_fake(REPAIR, to_zero)
    e = await judged_then_repaired(make_ctx(settings, 1))
    assert e.score == 0 and e.verification.attempts[0].action == "downgrade"


# ---------------------------------------------------------------------------
# Task 4: Bobby's plan gate
# ---------------------------------------------------------------------------


def test_the_gate_prompt_says_pending_approval_is_no_reason_to_escalate() -> None:
    assert "pending approval is never a reason to escalate" in bobby.GATE_SYSTEM
    assert "a criterion with no need, or a need with no source" in bobby.GATE_SYSTEM


async def test_the_plan_gate_reaches_bobby_with_the_rule_and_proceeds(settings) -> None:
    ctx = make_ctx(settings, 1)
    ctx.emit("run.status", {"status": "running", "stage": "plan"}, stage="plan", message="Stage plan")
    gates.spawn_bobby(ctx)
    seen: list[tuple[str, str]] = []
    register_fake(GATE, lambda s, u, sch: seen.append((s, u)) or bobby.fake_gate(s, u, sch))
    mark = ctx.state.run.last_seq
    await gates.after_stage(ctx, "plan")
    [d] = [e.data for e in ctx.state.events if e.seq > mark and e.type == "agent.decision"]
    assert (d.stage, d.decision) == ("plan", "proceed")
    system, user = seen[0]
    assert "Stage gate: after plan." in user and "approves the priority sources" in user
    assert "pending approval is never a reason to escalate" in system


# ---------------------------------------------------------------------------
# The recorded run (backend/.data/r2a_export, read only): code checks pass, the direction fails
# ---------------------------------------------------------------------------


def export_state() -> RunState:
    if not (EXPORT / "state.json").is_file():
        pytest.skip("backend/.data/r2a_export is not on this machine")
    state = RunState.model_validate_json((EXPORT / "state.json").read_text(encoding="utf-8"))
    state.events = []
    return state


def recorded_models(settings, state: RunState):
    """The recorded policy's role scopes name the EC2 run's model ids, so the copy calls the same ones."""
    scopes = {r.role: r.models for r in state.policy.role_scopes}
    return settings.model_copy(update={"model_small": scopes["verifier"][0], "model_large": scopes["drafter"][0]})


async def test_the_recorded_e002_fails_on_direction_with_simulated_judges(settings) -> None:
    state = export_state()
    settings = recorded_models(settings, state)
    store = Store(settings)
    store.put(state)
    ctx = RunContext.create(state, store, store.vault, settings)
    e = state.items["E-002"]
    assert (e.criterion, e.score, e.text) == ("rotic_vs_peers", 1, E002_TEXT)
    assert checks.status_from(verify.code_checks(state, e)) == "pass"
    res = (await verify.run_checks(ctx, e))[-1]
    assert (res.check, res.result) == ("support", "fail") and "Score direction wrong (+1)" in res.detail
    # every other recorded score keeps its code-only verdict on direction: none is contradicted by its own words
    others = [x for x in state.extrapolations() if x.id != "E-002"]
    assert all(judge.direction_conflict(x.score, x.text, state.run.company) is None for x in others)


# ---------------------------------------------------------------------------
# Live (AXELROD_LIVE=1): Haiku re-judges E-002, Sonnet re-runs the plan gate, on a copy in backend/.data/fixscore
# ---------------------------------------------------------------------------


def live_ctx(budget: float) -> RunContext:
    from backend.axelrod.config import get_settings

    data_dir = Path(os.environ.get("FIXSCORE_DATA_DIR") or "backend/.data/fixscore")
    live = get_settings().model_copy(update={"store": "local", "fake_models": False, "data_dir": data_dir,
                                             "cache_models": "off"})
    state = export_state()
    state.run.usage = m.Usage(budget_usd=budget)     # a fresh meter: the gateway enforces the cap
    state.run.notes = list(state.run.notes) + ["Test copy of run_20261108_5zqdvb: re-judged by B-fix-score."]
    store = Store(live)
    store.put(state)
    return RunContext.create(state, store, store.vault, live)


def verdicts(ctx: RunContext, agent_ids: list[str]) -> list[dict]:
    out = []
    for aid in agent_ids:
        for line in ctx.vault.get_text(ctx.run_id, f"00-Meta/agents/{aid}.jsonl").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "response" and rec.get("tool_input"):
                out.append({"agent_id": aid, "cost_usd": rec.get("cost_usd"), **rec["tool_input"]})
    return out


@pytest.mark.live
async def test_live_rejudge_of_e002_fails_on_direction() -> None:
    ctx = live_ctx(0.05)
    e = ctx.state.items["E-002"]
    k0 = verify.next_judge_k(ctx.state, "E-002")
    res = await verify.judge_claim(ctx, e)
    found = verdicts(ctx, [f"verifier-E-002-{k0 + j}" for j in range(verify.JUDGES)])
    print(f"\nE-002 {judge.shown(e.score)} re-judged: {res.result} {res.votes_for}/{res.votes_total}\n{res.detail}")
    for v in found:
        print(f"  {v['agent_id']}: {v['verdict']} direction={v.get('score_direction')} ${v['cost_usd']:.4f} {v['reason']}")
    print(f"cost ${ctx.state.run.usage.cost_usd:.4f}")
    assert res.check == "support" and res.result == "fail"
    named = [v for v in found if v.get("score_direction") == "wrong"
             or any(w in v["reason"].lower() for w in ("direction", "trail", "does not follow"))]
    assert named, found
    assert ctx.state.run.usage.cost_usd < 0.05


@pytest.mark.live
async def test_live_rejudge_with_minus_one_and_the_same_text_passes() -> None:
    """The symmetric check: the same recorded text under -1, so a pass above is not a judge that fails everything."""
    ctx = live_ctx(0.05)
    e = ctx.state.items["E-002"].model_copy(update={"score": -1})
    ctx.state.items["E-002"] = e
    k0 = verify.next_judge_k(ctx.state, "E-002")
    res = await verify.judge_claim(ctx, e)
    found = verdicts(ctx, [f"verifier-E-002-{k0 + j}" for j in range(verify.JUDGES)])
    print(f"\nE-002 as -1 re-judged: {res.result} {res.votes_for}/{res.votes_total}\n{res.detail}")
    for v in found:
        print(f"  {v['agent_id']}: {v['verdict']} direction={v.get('score_direction')} {v['reason']}")
    print(f"cost ${ctx.state.run.usage.cost_usd:.4f}")
    assert res.result == "pass" and ctx.state.run.usage.cost_usd < 0.05


@pytest.mark.live
async def test_live_plan_gate_on_the_recorded_digest_proceeds() -> None:
    """The digest Bobby escalated on in run_20261108_5zqdvb, word for word, with the new gate prompt."""
    ctx = live_ctx(0.05)
    transcript = (EXPORT / "vault" / "00-Meta" / "agents" / "bobby.jsonl").read_text(encoding="utf-8")
    recorded = next(rec for rec in map(json.loads, transcript.splitlines()) if rec.get("kind") == "request")["user"]
    digest = recorded.split("<digest>\n", 1)[1].split("\n</digest>", 1)[0]
    assert digest.startswith("Stage gate: after plan.") and "approves the priority sources" in digest
    call = await bobby.gate_call(ctx, "plan", digest)
    print(f"\nplan gate: {call.decision}: {call.summary!r}, cost ${ctx.state.run.usage.cost_usd:.4f}")
    assert call.decision == "proceed"
    assert ctx.state.run.usage.cost_usd < 0.05
