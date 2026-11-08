"""B-fix-score2: the follow-ups B-fix-score listed under needs. Offline (FAKE_MODELS=1, simulated drafters).

1. draft.py's pre-check runs drafter.direction_problems, so a judgment score whose text contradicts its sign goes
   back in the one revision round, and nothing extra is asked when the score agrees with its text.
2. Builder.key_ids swaps every drafter key in a score text for its conclusion id. The real E-002 text comes from
   the pre-R2 run run_20261108_5zqdvb (backend/.data/r2a_export, read only, never committed): the test using it
   skips when the export is missing.
3. The plan-gate digest states, from code, which in-scope criteria have no need and which needs have no source.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.axelrod import gates
from backend.axelrod.agents import drafter, judge
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import draft
from backend.axelrod.store import Store

REPO = Path(__file__).resolve().parents[2]
EXPORT = REPO / "backend" / ".data" / "r2a_export" / "state.json"
DRAFT = tool_name(drafter.DraftOutput)
COMPANY = "Northwind Instruments (fixture)"
TRAILS = "Northwind Instruments trails both peers on this measure (c_sites)."


@pytest.fixture(autouse=True)
def _fakes():
    """Every test starts and ends with the module fakes (simulated)."""
    drafter.register_fakes()
    judge.register_fakes()
    yield
    drafter.register_fakes()
    judge.register_fakes()


def make_ctx(settings, fixture_bundle, fixture_report) -> RunContext:
    """A real Store, Vault, Gateway and RunContext holding the fixture's plan and verified data (fictional)."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=COMPANY, ticker="NWI-X"))
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    state.needs = [m.DataNeed.model_validate(n) for n in fixture_bundle["needs"]]
    state.sources = [m.Source.model_validate(s) for s in fixture_bundle["sources"]]
    state.as_of = "2026-11-08"
    for item in fixture_report.items:
        if item.kind == "data":
            state.items[item.id] = item
    state.ids.counters["D"] = len(state.items)
    # The fixture has no peer data, so let the news item serve rotic_vs_peers too, as test_reason.py does.
    state.items["D-037"].criteria = ["secular_growth", "rotic_vs_peers"]
    return ctx


# ---------------------------------------------------------------------------
# 1. The direction pre-check
# ---------------------------------------------------------------------------


def peers_drafter(prompts: list[str], first: int, revised: int):
    """Simulated drafter: rotic_vs_peers gets one conclusion on D-037 and a judgment score with TRAILS. The
    first answer scores `first`; shown code's pre-check, it sends the score again as `revised`. Every other
    criterion is N/A, so it raises nothing."""
    def fake(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        revising = "<code_check>" in user
        out: dict = {"conclusions": [], "scores": [], "charts": []}
        for crit in judge.parse_block(user, "criteria") or []:
            c = crit["criterion"]
            if c != "rotic_vs_peers":
                out["scores"].append({"criterion": c, "score": None, "text": "Not scored.", "confidence": "low",
                                      "na_reason": "Not part of this simulated test draft."})
                continue
            if not revising:
                out["conclusions"].append({"key": "c_sites", "criteria": [c], "data_ids": ["D-037"],
                                           "text": "The five-year agreement covers consumables supply for 14 "
                                                   "manufacturing sites."})
            out["scores"].append({"criterion": c, "score": revised if revising else first, "confidence": "low",
                                  "conclusion_keys": ["c_sites"], "text": TRAILS})
        return out
    return fake


def _peer_prompts(prompts: list[str]) -> list[str]:
    return [p for p in prompts if '"criterion": "rotic_vs_peers"' in p]


def _score(state, criterion: str) -> m.Extrapolation:
    return next(e for e in state.extrapolations() if e.criterion == criterion)


def test_precheck_names_a_score_its_text_contradicts(settings, fixture_bundle, fixture_report) -> None:
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    verified = {d.id: d for d in draft.usable_data(ctx.state)}

    def out(score: int) -> drafter.DraftOutput:
        return drafter.DraftOutput.model_validate({
            "conclusions": [{"key": "c_sites", "criteria": ["rotic_vs_peers"], "data_ids": ["D-037"],
                             "text": "The five-year agreement covers consumables supply for 14 manufacturing sites."}],
            "scores": [{"criterion": "rotic_vs_peers", "score": score, "conclusion_keys": ["c_sites"], "text": TRAILS}]})

    found = draft.precheck(ctx.state, ["rotic_vs_peers"], out(1), verified, set(verified))
    assert [p["check"] for p in found] == ["score_direction"]
    assert found[0]["score"] == "rotic_vs_peers" and "'trails'" in found[0]["problem"]
    assert draft.precheck(ctx.state, ["rotic_vs_peers"], out(-1), verified, set(verified)) == []


async def test_a_contradicted_score_goes_back_once_and_comes_back_agreeing(settings, fixture_bundle,
                                                                            fixture_report) -> None:
    prompts: list[str] = []
    register_fake(DRAFT, peers_drafter(prompts, first=1, revised=-1))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    peers = _peer_prompts(prompts)
    assert len(peers) == 2 and "<code_check>" not in peers[0] and "<code_check>" in peers[1]
    block = peers[1].split("<code_check>\n")[1].split("\n</code_check>")[0]
    found = [json.loads(x) for x in block.splitlines()]
    assert [(p["score"], p["check"]) for p in found] == [("rotic_vs_peers", "score_direction")]
    assert "The score is +1" in found[0]["problem"] and drafter.DIRECTION_FIX in found[0]["problem"]
    e = _score(ctx.state, "rotic_vs_peers")
    assert e.score == -1 and judge.direction_conflict(e.score, e.text, COMPANY) is None
    assert "c_sites" not in e.text and f"({e.conclusion_ids[0]})" in e.text      # the key became its id
    aid = e.agent_id
    assert len([x for x in ctx.state.events if x.type == "gateway.request" and x.agent_id == aid]) == 2


async def test_no_new_model_call_when_the_score_agrees(settings, fixture_bundle, fixture_report) -> None:
    prompts: list[str] = []
    register_fake(DRAFT, peers_drafter(prompts, first=-1, revised=1))
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    await draft.run(ctx)
    assert len(_peer_prompts(prompts)) == 1 and not any("<code_check>" in p for p in prompts)
    e = _score(ctx.state, "rotic_vs_peers")
    assert e.score == -1
    assert len([x for x in ctx.state.events if x.type == "gateway.request" and x.agent_id == e.agent_id]) == 1


# ---------------------------------------------------------------------------
# 2. Every drafter key in a score text becomes its conclusion id
# ---------------------------------------------------------------------------


def builder(keys: dict[str, str], gi: int = 0) -> draft.Builder:
    b = draft.Builder(None, {})      # type: ignore[arg-type]   key_ids needs only the keys
    b.keys = {(gi, k): cid for k, cid in keys.items()}
    return b


def _export_e002() -> dict:
    if not EXPORT.exists():
        pytest.skip("backend/.data/r2a_export is missing (the pre-R2 run export, never committed)")
    return json.loads(EXPORT.read_text(encoding="utf-8"))["items"]["E-002"]


def test_the_real_e002_keys_all_become_ids() -> None:
    e002 = _export_e002()
    text = e002["text"]
    assert "(c_sh_fy2025_rotic)" in text                         # what run_20261108_5zqdvb shipped
    b = builder({"c_sh_fy2025_rotic": "C-005", "c_ge_fy2025": "C-008", "c_phi_fy2025": "C-010"})
    got = b.key_ids(0, text)
    assert "(C-005)" in got and "(C-008)" in got and "(C-010)" in got
    assert "c_" not in got and b.notes == {}
    assert got == (text.replace("c_sh_fy2025_rotic", "C-005").replace("c_ge_fy2025", "C-008")
                   .replace("c_phi_fy2025", "C-010"))
    assert set(e002["conclusion_ids"]) == {"C-005", "C-008", "C-010"}


def test_short_keys_and_a_prefix_pair_swap_cleanly() -> None:
    b = builder({"c1": "C-001", "c12": "C-012", "cn3": "C-003"})
    text = "ROTIC trails (c12) and (c1, cn3); see c1. Then c12, and [c1]: c12!"
    assert b.key_ids(0, text) == "ROTIC trails (C-012) and (C-001, C-003); see C-001. Then C-012, and [C-001]: C-012!"


def test_a_key_is_swapped_on_word_boundaries_only() -> None:
    b = builder({"c1": "C-001", "c_ge": "C-008"})
    text = "abc1 c1x c1-b c1_extra c_ge_fy2025 stay, (c1) and (c_ge) do not."
    assert b.key_ids(0, text) == "abc1 c1x c1-b c1_extra c_ge_fy2025 stay, (C-001) and (C-008) do not."


def test_a_key_that_is_a_plain_word_is_swapped_only_inside_parentheses() -> None:
    b = builder({"peers": "C-004"})
    assert b.key_ids(0, "The company trails both peers (peers).") == "The company trails both peers (C-004)."


def test_an_unknown_key_stays_and_is_noted_once() -> None:
    b = builder({"c_ge_fy2025": "C-008"})
    text = "SHL trails (c_sh_fy2025_rotic), GE leads (c_ge_fy2025), see (c_sh_fy2025_rotic) and (c9)."
    got = b.key_ids(0, text)
    assert got == "SHL trails (c_sh_fy2025_rotic), GE leads (C-008), see (c_sh_fy2025_rotic) and (c9)."
    b.key_ids(0, text)
    notes = b.notes[0]
    assert len(notes) == 2 and "'c_sh_fy2025_rotic'" in notes[0] and "'c9'" in notes[1]
    assert all("as written" in n for n in notes)


def test_keys_of_another_group_are_not_swapped() -> None:
    b = builder({"c1": "C-001"}, gi=1)
    assert b.key_ids(0, "Trails (c1).") == "Trails (c1)."
    assert b.key_ids(1, "Trails (c1).") == "Trails (C-001)."


def test_words_in_parentheses_are_not_noted() -> None:
    b = builder({})
    assert b.key_ids(0, "Leverage is low (cash rich, C-005, cagr 5%).") == "Leverage is low (cash rich, C-005, cagr 5%)."
    assert b.notes == {}


# ---------------------------------------------------------------------------
# 3. The plan-gate digest says, from code, what the plan leaves unmapped
# ---------------------------------------------------------------------------


def test_plan_digest_says_none_when_the_plan_links_up(settings, fixture_bundle, fixture_report) -> None:
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    text = gates.build_digest(ctx, "plan")
    assert "Criteria with no need: none." in text and "Needs with no source: none." in text
    assert len(text) < 2500


def test_plan_digest_names_a_broken_plan(settings, fixture_bundle, fixture_report) -> None:
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    st = ctx.state
    st.needs = [n for n in st.needs if n.need_id != "N-08"]            # research_stage loses its only need
    for s in st.sources:
        if s.source_id == "S-008":                                     # N-07's only source
            s.approval = "rejected"
    st.sources = [s for s in st.sources if s.source_id != "S-007"]    # N-02's only source
    text = gates.build_digest(ctx, "plan")
    assert "Criteria with no need: research_stage." in text
    assert "Needs with no source: N-02, N-07." in text
    assert gates.plan_links(st) == (["research_stage"], ["N-02", "N-07"])


def _stopped_pair(ctx: RunContext) -> None:
    """extractor-01 stopped by the analyst (its R-stop rule), extractor-02 ended by a whole-run STOP (no rule)."""
    st = ctx.state
    for aid in ("extractor-01", "extractor-02"):
        st.register_agent(m.Agent(agent_id=aid, role="extractor", model_id=ctx.settings.model_small, status="stopped",
                                  task=f"Read S-00{aid[-1]}", target_ids=[], started_at=now_iso()))
    ctx.gateway.build_policy()
    st.policy.rules.append(m.PolicyRule(rule_id="R-stop-extractor-01", effect="deny", action="model.invoke",
                                        roles=["extractor"], origin="user_agent_stop", agent_id="extractor-01",
                                        note="Agent stopped by the analyst", created_at=now_iso()))


def test_only_an_r_stop_rule_counts_as_stopped_by_the_analyst(settings, fixture_bundle, fixture_report) -> None:
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    _stopped_pair(ctx)
    assert gates.stopped_by_analyst(ctx.state, "extractor-01") and not gates.stopped_by_analyst(ctx.state, "extractor-02")
    text = gates.build_digest(ctx, "extract")
    assert "Stopped by the analyst: extractor-01." in text and "extractor-02" not in text.split("Stopped by")[1]
    orders = [SimpleNamespace(agent_id=a, instructions="Again, carefully.") for a in ("extractor-01", "extractor-02")]
    _, rejected = gates.validate_reworks(ctx, "extract", orders)
    assert "extractor-01: stopped by the analyst, and Bobby never undoes that" in rejected
    assert not any(r.startswith("extractor-02: stopped by the analyst") for r in rejected)


def test_a_whole_run_stop_alone_lists_no_one(settings, fixture_bundle, fixture_report) -> None:
    ctx = make_ctx(settings, fixture_bundle, fixture_report)
    _stopped_pair(ctx)
    ctx.state.policy.rules = [r for r in ctx.state.policy.rules if r.rule_id != "R-stop-extractor-01"]
    assert "Stopped by the analyst" not in gates.build_digest(ctx, "extract")


def test_the_real_pre_r2_plan_links_up() -> None:
    if not EXPORT.exists():
        pytest.skip("backend/.data/r2a_export is missing (the pre-R2 run export, never committed)")
    raw = json.loads(EXPORT.read_text(encoding="utf-8"))
    st = SimpleNamespace(needs=[m.DataNeed.model_validate(n) for n in raw["needs"]],
                         sources=[m.Source.model_validate(s) for s in raw["sources"]],
                         run=SimpleNamespace(depth=raw["run"]["depth"]))
    assert gates.plan_links(st) == ([], [])                           # live Bobby's "7 needs all mapped" was right
