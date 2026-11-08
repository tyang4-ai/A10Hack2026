"""B-brief: the 3-page brief, offline (FAKE_MODELS=1), from the mock fixture's verified agent report.

The fixture is the mock's fictional company. Every planted sentence below is simulated: a test fake adds
it to the offline summarizer's brief, and a test judge fake dissents on the planted words, so the tests
prove the harness (checks, votes, repairs, deletion, certainty), not a model's judgement.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter

import pytest

from backend.axelrod import bedrock
from backend.axelrod.agents import summarizer
from backend.axelrod.agents.base import tool_name
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext, StopRequested
from backend.axelrod.stages import summarize, verify_summary
from backend.axelrod.store import Store

BRIEF = tool_name(summarizer.BriefDraft)
JUDGE = tool_name(summarizer.FaithfulnessVerdict)
REWRITE = tool_name(summarizer.BriefRewrite)

C007 = "The $500 million revolving credit facility was undrawn at September 30, 2025 and expires in 2028."


@pytest.fixture(autouse=True)
def _default_fakes():
    """Each test may swap a fake. Put the module's offline fakes back afterwards."""
    yield
    bedrock.register_fake(BRIEF, summarizer.fake_brief)
    bedrock.register_fake(JUDGE, summarizer.fake_judge)
    bedrock.register_fake(REWRITE, summarizer.fake_rewrite)


def report_ctx(settings, bundle: dict, report: m.AgentReport) -> RunContext:
    """A real Store, RunContext and Gateway holding a final report (items, holes, rollup)."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=report.company, ticker=report.ticker))
    state.sources = [m.Source.model_validate(s) for s in copy.deepcopy(bundle["sources"])]
    state.items = {i.id: i for i in report.items}
    state.holes = list(report.holes)
    state.rollup = report.rollup
    state.report_certainty = report.certainty
    state.report_ready = True
    state.as_of = "2026-11-08"
    state.run.stage = "summarize"
    return RunContext.create(state, store, store.vault, settings)


@pytest.fixture
def ctx(settings, fixture_bundle, fixture_report) -> RunContext:
    return report_ctx(settings, fixture_bundle, fixture_report)


async def make_brief(ctx: RunContext) -> m.HumanReport:
    await summarize.run(ctx)
    await verify_summary.run(ctx)
    return ctx.state.summary


def evs(ctx: RunContext, type_: str) -> list:
    return [e for e in ctx.state.events if e.type == type_]


def sents(brief: m.HumanReport) -> list[m.SummarySentence]:
    return [s for _, s in summarize.all_sentences(brief.sections)]


def written(ctx: RunContext, text: str) -> m.SummarySentence:
    """The sentence as summary.sentence streamed it, found by its text."""
    return next(e.data.sentence for e in evs(ctx, "summary.sentence") if e.data.sentence.text == text)


def verified(ctx: RunContext, sid: str) -> list[m.SummaryVerifiedData]:
    return [e.data for e in evs(ctx, "summary.verified") if e.data.sentence_id == sid]


def plant(*planted: dict):
    """Summarizer fake: the offline brief plus planted sentences in the first section (simulated)."""
    def fn(system: str, user: str, schema: dict) -> dict:
        out = summarizer.fake_brief(system, user, schema)
        out["sections"][0]["blocks"].append({"type": "paragraph", "sentences": list(planted)})
        return out
    return fn


def flagging(*phrases: str):
    """Judge fake: dissents when the sentence holds a planted phrase, and names it as the unsupported span."""
    def fn(system: str, user: str, schema: dict) -> dict:
        sentence = user.split("<sentence>\n", 1)[1].split("\n</sentence>", 1)[0]
        spans = [p for p in phrases if p in sentence]
        return {"supported": not spans, "unsupported_spans": spans,
                "reason": "The evidence does not state this." if spans else "Stated in the evidence."}
    return fn


# ---------------------------------------------------------------------------
# The whole brief
# ---------------------------------------------------------------------------


async def test_fixture_report_gives_a_valid_final_brief(ctx: RunContext, fixture_report):
    brief = await make_brief(ctx)
    m.HumanReport.model_validate(brief.model_dump(mode="json"))
    assert brief.status == "final" and brief.run_id == ctx.run_id
    assert 0 < brief.stats.est_pages <= 3.0
    ids = {i.id for i in fixture_report.items} | {"rollup"} | {h.hole_id for h in fixture_report.holes}
    all_s = sents(brief)
    assert all_s and brief.stats.sentences == len(all_s)
    assert brief.stats.words == sum(len(s.text.split()) for s in all_s)
    for s in all_s:
        assert re.fullmatch(r"H-\d{3}", s.id)
        assert set(s.derived_from) <= ids
        assert s.verification.status in ("pass", "fixed"), (s.id, s.verification.checks)
        assert s.certainty is not None and s.certainty.band != "none"
    assert [s.id for s in all_s] == [f"H-{n:03d}" for n in range(1, len(all_s) + 1)]
    assert any(isinstance(b, m.ScorecardBlock) for sec in brief.sections for b in sec.blocks)
    assert sum(isinstance(b, m.ChartBlock) for sec in brief.sections for b in sec.blocks) <= 2
    assert brief.sections[0].heading == "Bottom line"
    assert brief.sections[-1].heading == "What we could not determine"
    assert brief.holes == fixture_report.holes
    c = brief.certainty
    assert c is not None and c.mean is not None
    assert (c.coverage_scored, c.coverage_in_scope) == (fixture_report.rollup.scored, fixture_report.rollup.in_scope)
    assert c.high + c.medium + c.low + c.none == len(all_s)


async def test_events_stream_then_verify_then_ready(ctx: RunContext):
    brief = await make_brief(ctx)
    written_ = evs(ctx, "summary.sentence")
    finals = [e for e in evs(ctx, "summary.verified") if e.data.certainty is not None]
    assert [e.data.sentence.id for e in written_] == [s.id for s in sents(brief)]
    assert all(e.data.sentence.verification.status == "pending" for e in written_)
    assert {e.data.sentence_id for e in finals} == {s.id for s in sents(brief)}
    ready = evs(ctx, "summary.ready")
    assert len(ready) == 1 and ready[0].seq > max(e.seq for e in finals) > max(e.seq for e in written_)
    assert (ready[0].data.sentences, ready[0].data.words) == (brief.stats.sentences, brief.stats.words)
    assert ctx.state.summary_ready and ctx.state.human_report().status == "final"
    agents = ctx.state.agents
    summ = [a for a in agents.values() if a.role == "summarizer"]
    assert len(summ) == 1 and summ[0].status == "done" and summ[0].agent_id == "summarizer-01"
    judges = [a for a in agents.values() if a.role == "verifier"]
    assert judges and all(re.fullmatch(r"verifier-H-\d{3}-[123]", a.agent_id) for a in judges)
    assert all(a.status == "done" and a.usage.model_calls >= 1 for a in judges)
    derived = [s for s in sents(brief) if s.mode == "derived"]
    assert len(judges) == 3 * len(derived)


async def test_copied_sentences_are_proven_by_code_and_derived_ones_by_three_judges(ctx: RunContext):
    brief = await make_brief(ctx)
    for s in sents(brief):
        names = [c.check for c in s.verification.checks]
        if s.mode == "copied":
            assert names == ["citations_resolve", "copy_exact"]
            assert s.certainty.basis.startswith("copied verbatim")
        else:
            assert names == ["citations_resolve", "numbers_traced", "faithfulness"]
            faith = s.verification.checks[-1]
            assert (faith.method, faith.votes_for, faith.votes_total) == ("llm_judge", 3, 3)
            assert faith.agent_id == f"verifier-{s.id}"


async def test_vault_record_and_markdown(ctx: RunContext):
    brief = await make_brief(ctx)
    rec = json.loads(ctx.vault.get_text(ctx.run_id, verify_summary.CHECKS_RELPATH))
    assert set(rec["sentences"]) == {s.id for s in sents(brief)}
    assert all(v["in_brief"] for v in rec["sentences"].values())
    md = verify_summary.render_markdown(brief, rollup=ctx.state.rollup, items=ctx.state.items)
    assert md.startswith(f"# {brief.title}")
    assert "## Bottom line" in md and "| Category | Weight |" in md and "Weighted average +0.50" in md
    first = sents(brief)[0]
    assert f"[{first.id}: {', '.join(first.derived_from)}; certainty {first.certainty.score}]" in md
    assert brief.disclaimer in md


async def test_summarizer_sees_only_verified_items(ctx: RunContext):
    seen: list[str] = []

    def spy(system: str, user: str, schema: dict) -> dict:
        seen.append(user)
        return summarizer.fake_brief(system, user, schema)

    bedrock.register_fake(BRIEF, spy)
    await summarize.run(ctx)
    payload = summarizer._tagged(seen[0], "report")
    ids = {i["id"] for i in payload["items"]}
    assert "C-009" not in ids                                  # failed: only its hole is shown
    assert "second-largest share" not in seen[0]
    assert {h["id"] for h in payload["holes"]} == {h.hole_id for h in ctx.state.holes}
    assert ids == {i.id for i in ctx.state.items.values() if i.verification.status in ("pass", "fixed")}
    assert payload["rollup"]["weighted_average"] == ctx.state.rollup.weighted_average


# ---------------------------------------------------------------------------
# Planted failures (simulated)
# ---------------------------------------------------------------------------


async def test_planted_unfaithful_sentence_fails_faithfulness_and_is_weakened(ctx: RunContext):
    bad = "Total debt was $1,150 million, and management expects to repay it by fiscal 2027."
    span = "and management expects to repay it by fiscal 2027"
    bedrock.register_fake(BRIEF, plant({"text": bad, "mode": "derived", "derived_from": ["C-005"]}))
    bedrock.register_fake(JUDGE, flagging(span))
    brief = await make_brief(ctx)
    sid = written(ctx, bad).id
    first, final = verified(ctx, sid)[0], verified(ctx, sid)[-1]
    faith = {c.check: c for c in first.verification.checks}["faithfulness"]
    assert first.verification.status == "fail"
    assert {c.check: c.result for c in first.verification.checks} == {
        "citations_resolve": "pass", "numbers_traced": "pass", "faithfulness": "fail"}
    assert (faith.votes_for, faith.votes_total, faith.unsupported_spans) == (0, 3, [span])
    assert final.verification.status == "fixed"
    att = final.verification.attempts
    assert att[0].action == "downgrade" and att[0].before == bad and att[0].after == "Total debt was $1,150 million."
    assert att[-1].status == "fixed"
    kept = next(s for s in sents(brief) if s.id == sid)
    assert kept.text == "Total debt was $1,150 million." and kept.derived_from == ["C-005"]
    assert brief.stats.fixed >= 1 and brief.stats.est_pages <= 3.0
    assert any(a.role == "repairer" and a.target_ids == [sid] for a in ctx.state.agents.values())


async def test_unfaithful_sentence_that_cannot_be_weakened_is_deleted(ctx: RunContext):
    bad = "Northwind will become the market leader."
    bedrock.register_fake(BRIEF, plant({"text": bad, "mode": "derived", "derived_from": ["C-008"]}))
    bedrock.register_fake(JUDGE, flagging("will become the market leader"))
    brief = await make_brief(ctx)
    sid = written(ctx, bad).id
    final = verified(ctx, sid)[-1]
    assert final.verification.status == "fail" and final.certainty.band == "none"
    assert final.verification.attempts[-1].action == "delete"
    assert sid not in {s.id for s in sents(brief)}
    assert brief.stats.failed == 1
    assert bad not in verify_summary.render_markdown(brief)
    rec = json.loads(ctx.vault.get_text(ctx.run_id, verify_summary.CHECKS_RELPATH))["sentences"][sid]
    assert rec["removed_by_repair"] and not rec["in_brief"]


async def test_second_repair_falls_back_to_the_cited_items_own_words(ctx: RunContext, fixture_report):
    bad = "Revenue rose every year, and demand will keep growing."
    bedrock.register_fake(BRIEF, plant({"text": bad, "mode": "derived", "derived_from": ["C-008"]}))
    bedrock.register_fake(JUDGE, flagging("and demand will keep growing", "and demand keeps growing"))
    bedrock.register_fake(REWRITE, lambda s, u, sc: {"action": "rewrite",
                                                     "text": "Revenue rose every year, and demand keeps growing."})
    brief = await make_brief(ctx)
    sid = written(ctx, bad).id
    kept = next(s for s in sents(brief) if s.id == sid)
    c008 = next(i for i in fixture_report.items if i.id == "C-008")
    assert (kept.text, kept.mode, kept.derived_from) == (c008.text, "copied", ["C-008"])
    att = kept.verification.attempts
    assert [a.action for a in att] == ["downgrade", "recite", None]
    assert [a.status for a in att] == ["fail", "fail", "fixed"]
    assert kept.verification.status == "fixed"


async def test_copied_sentence_with_one_changed_word_fails_copy_exact(ctx: RunContext):
    changed = C007.replace("undrawn", "drawn")
    s = m.SummarySentence(id="H-900", text=changed, mode="copied", derived_from=["C-007"],
                          verification=m.Verification(status="pending"))
    res = verify_summary.code_checks(ctx.state, s)
    cx = {c.check: c for c in res}["copy_exact"]
    assert cx.result == "fail" and cx.diff and any(d.op != "equal" for d in cx.diff)
    ok = s.model_copy(update={"text": C007})
    assert {c.check: c.result for c in verify_summary.code_checks(ctx.state, ok)} == {
        "citations_resolve": "pass", "copy_exact": "pass"}


async def test_planted_changed_copy_is_recited_to_the_exact_item_text(ctx: RunContext):
    changed = C007.replace("undrawn", "drawn")
    bedrock.register_fake(BRIEF, plant({"text": changed, "mode": "copied", "derived_from": ["C-007"]}))
    brief = await make_brief(ctx)
    sid = written(ctx, changed).id
    first = verified(ctx, sid)[0]
    assert first.verification.status == "fail"
    assert {c.check: c.result for c in first.verification.checks}["copy_exact"] == "fail"
    kept = next(s for s in sents(brief) if s.id == sid)
    assert kept.text == C007 and kept.verification.status == "fixed"
    assert kept.verification.attempts[0].action == "recite"


async def test_wrong_number_is_caught_by_numbers_traced(ctx: RunContext):
    bad = "Total debt was $1,250 million at the end of fiscal 2025."
    bedrock.register_fake(BRIEF, plant({"text": bad, "mode": "derived", "derived_from": ["C-005"]}))
    brief = await make_brief(ctx)
    sid = written(ctx, bad).id
    first = verified(ctx, sid)[0]
    nt = {c.check: c for c in first.verification.checks}["numbers_traced"]
    assert nt.result == "fail" and nt.unsupported_spans == ["$1,250 million"]
    assert "faithfulness" not in {c.check for c in first.verification.checks}   # judges only after code passes
    final = verified(ctx, sid)[-1]
    assert final.verification.attempts[0].action == "recompute"
    assert final.verification.status in ("fixed", "fail")
    if final.verification.status == "fixed":
        assert "$1,250" not in next(s for s in sents(brief) if s.id == sid).text


async def test_citation_that_does_not_resolve_is_dropped(ctx: RunContext):
    text = "The revolving credit facility was undrawn."
    bedrock.register_fake(BRIEF, plant({"text": text, "mode": "derived", "derived_from": ["C-007", "D-999"]}))
    brief = await make_brief(ctx)
    sid = written(ctx, text).id
    first = verified(ctx, sid)[0]
    cr = first.verification.checks[0]
    assert (cr.check, cr.result) == ("citations_resolve", "fail") and "D-999" in cr.detail
    kept = next(s for s in sents(brief) if s.id == sid)
    assert kept.derived_from == ["C-007"] and kept.verification.status == "fixed"
    assert kept.verification.attempts[0].action == "downgrade"


async def test_two_of_three_judges_pass_a_sentence_with_lower_certainty(ctx: RunContext):
    calls: Counter = Counter()

    def two_of_three(system: str, user: str, schema: dict) -> dict:
        key = user.split("<sentence>", 1)[1]
        calls[key] += 1
        ok = calls[key] % 3 != 0
        return {"supported": ok, "unsupported_spans": [], "reason": "ok" if ok else "Not stated."}

    bedrock.register_fake(JUDGE, two_of_three)
    brief = await make_brief(ctx)
    derived = [s for s in sents(brief) if s.mode == "derived"]
    assert derived
    for s in derived:
        faith = s.verification.checks[-1]
        assert (faith.result, faith.votes_for, faith.votes_total) == ("pass", 2, 3)
        assert s.verification.status == "pass"
        assert s.certainty.support == round(2 / 3, 4)


async def test_one_of_three_judges_is_not_enough(ctx: RunContext):
    calls: Counter = Counter()

    def one_of_three(system: str, user: str, schema: dict) -> dict:
        key = user.split("<sentence>", 1)[1]
        calls[key] += 1
        return {"supported": calls[key] % 3 == 1, "unsupported_spans": [], "reason": "Not stated."}

    bedrock.register_fake(JUDGE, one_of_three)
    await summarize.run(ctx)
    derived = [s.id for _, s in summarize.all_sentences(ctx.state.summary.sections) if s.mode == "derived"]
    await verify_summary.run(ctx)
    assert derived
    for sid in derived:
        faith = {c.check: c for c in verified(ctx, sid)[0].verification.checks}["faithfulness"]
        assert (faith.result, faith.votes_for, faith.votes_total) == ("fail", 1, 3)


async def test_judges_that_cannot_answer_leave_the_sentence_blocked_not_passed(ctx: RunContext):
    def broken(system: str, user: str, schema: dict) -> dict:
        return {"supported": "maybe"}       # invalid twice: AgentOutputError, no vote

    bedrock.register_fake(JUDGE, broken)
    brief = await make_brief(ctx)
    derived = [s for s in sents(brief) if s.mode == "derived"]
    assert derived
    for s in derived:
        faith = s.verification.checks[-1]
        assert (faith.result, faith.votes_total) == ("blocked", 0)
        assert s.verification.status == "blocked" and s.certainty.band == "none"
        assert not s.verification.attempts                  # blocked is never repaired


async def test_judge_sees_only_its_sentence_and_cited_evidence(ctx: RunContext):
    prompts: list[str] = []

    def spy(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        return summarizer.fake_judge(system, user, schema)

    text = "Total debt was $1,150 million."
    bedrock.register_fake(BRIEF, plant({"text": text, "mode": "derived", "derived_from": ["C-005"]}))
    bedrock.register_fake(JUDGE, spy)
    await make_brief(ctx)
    mine = [p for p in prompts if f"<sentence>\n{text}\n</sentence>" in p]
    assert len(mine) == 3
    evidence = summarizer._tagged(mine[0], "evidence")
    ids = [e["id"] for e in evidence]
    c005 = ctx.state.items["C-005"]
    assert ids[0] == "C-005" and set(ids) == {"C-005", *c005.data_ids}
    assert "scores +0.50" not in mine[0]            # no other brief sentence


# ---------------------------------------------------------------------------
# Three pages
# ---------------------------------------------------------------------------


def _s(text: str, refs: list[str], sid: str = "") -> m.SummarySentence:
    return m.SummarySentence(id=sid, text=text, mode="derived", derived_from=refs,
                             verification=m.Verification(status="pending"))


def test_page_estimate_and_trim_keep_bottom_line_holes_and_scorecard():
    long = " ".join(["word"] * 40) + "."
    secs = [m.SummarySection(heading="Bottom line", blocks=[m.ParagraphBlock(sentences=[_s(long, ["rollup"])])])]
    for k in range(6):
        secs.append(m.SummarySection(heading=f"Part {k}", blocks=[
            m.ParagraphBlock(sentences=[_s(long, ["C-001"]) for _ in range(5)]),
            m.ChartBlock(chart_id=f"G-00{k}", caption=_s("A chart.", [f"G-00{k}"]))]))
    secs.append(m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()]))
    secs.append(m.SummarySection(heading="What we could not determine",
                                 blocks=[m.BulletsBlock(sentences=[_s("A gap stays declared.", ["HOLE-01"])])]))
    assert summarize.estimate_pages(secs) > 3.0
    fitted, cut = summarize.fit_pages(secs)
    assert summarize.estimate_pages(fitted) <= 3.0 and cut
    heads = [s.heading for s in fitted]
    assert heads[0] == "Bottom line" and heads[-1] == "What we could not determine" and "Scorecard" in heads
    assert sum(isinstance(b, m.ChartBlock) for s in fitted for b in s.blocks) <= 2
    assert summarize.estimate_pages(secs) > 3.0          # the input is not changed


def test_page_estimate_formula():
    secs = [m.SummarySection(heading="A", blocks=[m.ParagraphBlock(sentences=[_s(" ".join(["w"] * 450), ["C-001"])])]),
            m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()])]
    # 450 words = 1 page, + 0.35 scorecard + 2 x 0.04 headings + 0.15 frame = 1.58 -> 1.6
    assert summarize.estimate_pages(secs) == 1.6
    assert 500 <= summarize.word_budget(2) <= 800


async def test_oversized_draft_is_cut_to_three_pages_before_streaming(ctx: RunContext, fixture_report):
    c001 = next(i for i in fixture_report.items if i.id == "C-001")

    def wordy(system: str, user: str, schema: dict) -> dict:
        out = summarizer.fake_brief(system, user, schema)
        filler = [{"text": c001.text, "mode": "copied", "derived_from": ["C-001"]} for _ in range(60)]
        out["sections"].insert(1, {"heading": "Everything", "blocks": [{"type": "bullets", "sentences": filler}]})
        return out

    bedrock.register_fake(BRIEF, wordy)
    await summarize.run(ctx)
    assert ctx.state.summary.stats.est_pages <= 3.0
    assert len(evs(ctx, "summary.sentence")) == ctx.state.summary.stats.sentences
    assert any("cut to fit" in e.data.text for e in evs(ctx, "agent.log"))
    await verify_summary.run(ctx)
    assert ctx.state.summary.stats.est_pages <= 3.0


async def test_code_drops_uncheckable_blocks_and_adds_the_scorecard(ctx: RunContext):
    def messy(system: str, user: str, schema: dict) -> dict:
        return {"sections": [
            {"heading": "Bottom line", "blocks": [{"type": "paragraph", "sentences": [
                {"text": C007, "mode": "copied", "derived_from": ["C-007"]},
                {"text": "This cites nothing.", "mode": "derived", "derived_from": []}]}]},
            {"heading": "Charts", "blocks": [
                {"type": "chart", "chart_id": "G-404", "caption": {"text": "x", "mode": "derived", "derived_from": ["G-404"]}},
                {"type": "chart", "chart_id": "G-001"}]},
        ]}

    bedrock.register_fake(BRIEF, messy)
    await summarize.run(ctx)
    brief = ctx.state.summary
    assert [s.text for s in sents(brief)] == [C007]
    assert [sec.heading for sec in brief.sections] == ["Bottom line", "Scorecard"]
    notes = " | ".join(e.data.text for e in evs(ctx, "agent.log"))
    assert "citing nothing" in notes and "G-404 dropped" in notes and "G-001 dropped: no caption" in notes


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def test_stop_before_summarize(ctx: RunContext):
    ctx.gateway.build_policy()
    ctx.gateway.set_kill_switch("test stop")
    with pytest.raises(StopRequested):
        await summarize.run(ctx)
    assert ctx.state.summary is None


async def test_stop_while_judging_stops_the_stage_and_its_judges(ctx: RunContext):
    await summarize.run(ctx)
    ctx.gateway.build_policy()

    def stop_then_answer(system: str, user: str, schema: dict) -> dict:
        ctx.gateway.set_kill_switch("stopped mid-judging")
        return summarizer.fake_judge(system, user, schema)

    bedrock.register_fake(JUDGE, stop_then_answer)
    with pytest.raises(StopRequested):
        await verify_summary.run(ctx)
    judges = [a for a in ctx.state.agents.values() if a.role == "verifier"]
    assert judges and all(a.status == "stopped" for a in judges)
    assert not evs(ctx, "summary.ready")


async def test_both_stages_through_the_orchestrator(ctx: RunContext, monkeypatch):
    from backend.axelrod import orchestrator

    monkeypatch.setattr(orchestrator, "STAGES", [("summarize", "summarize"), ("verify_summary", "verify_summary")])
    await orchestrator.run_pipeline(ctx)
    st = ctx.state
    assert st.run.status == "done", st.run.error
    stages = [e.data.stage for e in evs(ctx, "run.status")]
    assert stages[:2] == ["summarize", "verify_summary"]
    assert {e.stage for e in evs(ctx, "summary.sentence")} == {"summarize"}
    assert {e.stage for e in evs(ctx, "summary.verified")} == {"verify_summary"}
    saved = ctx.store.get(ctx.run_id).human_report()
    assert saved.status == "final" and saved.stats.est_pages <= 3.0


# ---------------------------------------------------------------------------
# Live (AXELROD_LIVE=1): real Bedrock through the gateway, local store and vault
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_brief_from_the_fixture_report(settings, fixture_bundle, fixture_report, tmp_path):
    """Summarizer, judges and repairers on real models. The store is local, so only Bedrock is live."""
    live = settings.model_copy(update={"store": "local", "data_dir": tmp_path, "fake_models": False})
    ctx = report_ctx(live, fixture_bundle, fixture_report)
    brief = await make_brief(ctx)
    m.HumanReport.model_validate(brief.model_dump(mode="json"))
    all_s = sents(brief)
    ok = [s for s in all_s if s.verification.status in ("pass", "fixed")]
    print(f"\n{brief.stats}\ncertainty {brief.certainty}\ncost ${ctx.state.run.usage.cost_usd:.4f}")
    for s in all_s:
        print(f"{s.id} {s.mode} {s.verification.status} {s.derived_from}: {s.text}")
    assert brief.status == "final" and brief.stats.est_pages <= 3.0
    assert all_s and len(ok) >= 0.8 * len(all_s)
