"""B-fix-holes: the brief's gap section is a short counted summary, and content is trimmed before it.

Offline (FAKE_MODELS=1) on two recorded SHL runs, both read only:
- G5, `backend/data/replay/run_20261108_pin2ve` (13 holes; its old brief spent 11 of 24 sentences on holes).
- R0b, `backend/.data/r0b` (25 holes; 25 of 33 sentences were hole bullets and Financial Strength was cut).
  Not committed, so those tests skip when it is absent.
Each test works on a copy in a temp DATA_DIR. Planted drafts and judge fakes below are simulated.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import re
from pathlib import Path

import pytest

from backend.axelrod import bedrock
from backend.axelrod.agents import summarizer
from backend.axelrod.agents.base import tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import summarize, verify_summary
from backend.axelrod.store import Store
from backend.axelrod.store.state import RunState
from backend.axelrod.verify.numbers import numbers_in_text

BRIEF = tool_name(summarizer.BriefDraft)
JUDGE = tool_name(summarizer.FaithfulnessVerdict)
REWRITE = tool_name(summarizer.BriefRewrite)

PIN2VE = Path("backend/data/replay/run_20261108_pin2ve/state.json")
R0B = Path("backend/.data/r0b")
GAP = summarize.HOLE_HEADING
CAT_LABEL = {c["id"]: c["label"] for c in RUBRIC["categories"]}


@pytest.fixture(autouse=True)
def _default_fakes():
    yield
    bedrock.register_fake(BRIEF, summarizer.fake_brief)
    bedrock.register_fake(JUDGE, summarizer.fake_judge)
    bedrock.register_fake(REWRITE, summarizer.fake_rewrite)


# ---------------------------------------------------------------------------
# Recorded runs, loaded once, copied per test
# ---------------------------------------------------------------------------


@functools.cache
def _stored_json(name: str) -> str | None:
    """The recorded state as JSON, with its old brief and its events left out."""
    if name == "pin2ve":
        state = RunState.model_validate_json(PIN2VE.read_text(encoding="utf-8"))
    else:
        if not (R0B / "runs").is_dir():
            return None
        from backend.axelrod.config import load_settings

        r0b = load_settings(env_file=None, environ={"STORE": "local", "DATA_DIR": str(R0B), "FAKE_MODELS": "1"})
        states = Store(r0b)._load_local()          # reads only
        if not states:
            return None
        state = states[0]
    state.events = []
    state.summary = None
    state.summary_ready = False
    return state.model_dump_json()


def report_ctx(settings, bundle: dict, report: m.AgentReport) -> RunContext:
    """A run holding the mock fixture's final report (fictional company), ready for summarize."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=report.company, ticker=report.ticker))
    state.sources = [m.Source.model_validate(s) for s in copy.deepcopy(bundle["sources"])]
    state.items = {i.id: i for i in report.items}
    state.holes = list(report.holes)
    state.rollup = report.rollup
    state.report_ready = True
    state.as_of = "2026-11-08"
    return RunContext.create(state, store, store.vault, settings)


def stored_ctx(settings, name: str) -> RunContext:
    raw = _stored_json(name)
    if raw is None:
        pytest.skip(f"{R0B} is not here (it is never committed)")
    state = RunState.model_validate_json(raw)
    store = Store(settings)
    store.put(state)
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy(emit=False)      # the recorded policy names the recorded model ids, not the fakes'
    return ctx


def sents(brief: m.HumanReport) -> list[m.SummarySentence]:
    return [s for _, s in summarize.all_sentences(brief.sections)]


def gap_sents(brief: m.HumanReport) -> list[m.SummarySentence]:
    return [s for sec in brief.sections for _, s in summarize.all_sentences([sec])
            if summarize.is_gap_section(sec) or summarize.hole_only(s)]


def covered(brief: m.HumanReport, items: dict) -> set[str]:
    """Categories with a content sentence of their own (the bottom line and the gaps do not count)."""
    out = set()
    for i, sec in enumerate(brief.sections):
        if i == 0 or summarize.is_gap_section(sec):
            continue
        for _, s in summarize.all_sentences([sec]):
            out |= {items[r].category for r in s.derived_from if r in items}
    return out


def logs(ctx: RunContext) -> str:
    return " | ".join(e.data.text for e in ctx.state.events if e.type == "agent.log")


# ---------------------------------------------------------------------------
# The whole brief on recorded runs (fake models)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["pin2ve", "r0b"])
async def test_recorded_run_gives_a_short_gap_summary_and_every_category(settings, name):
    ctx = stored_ctx(settings, name)
    st = ctx.state
    await summarize.run(ctx)
    await verify_summary.run(ctx)
    brief = st.summary
    m.HumanReport.model_validate(brief.model_dump(mode="json"))
    assert brief.status == "final" and brief.stats.est_pages <= 3.0
    gaps = gap_sents(brief)
    assert 2 <= len(gaps) <= 5
    assert brief.sections[-1].heading == GAP
    assert brief.holes == st.holes                               # the full register stays on the brief
    content, gap = summarize.split_counts(brief.sections)
    assert gap == len(gaps) and content > gap
    want = {CAT_LABEL[c] for c in {RUBRIC["criteria"][k]["category"] for k in RUBRIC["depths"]["screen"]}}
    assert {c["category"] for c in summarizer.categories_in_scope(st)} == want
    assert {CAT_LABEL[c] for c in covered(brief, st.items)} >= want   # Financial Strength included
    for s in sents(brief):
        assert s.verification.status in ("pass", "fixed"), (s.id, s.text, s.verification.checks)
    lead = gaps[0]
    assert set(lead.derived_from) == {h.hole_id for h in st.holes}
    assert summarize.count_words(len(st.holes)) in lead.text
    for s in gaps:
        assert all(r.startswith("HOLE-") for r in s.derived_from)
        assert not re.search(r"\d", s.text) and numbers_in_text(s.text) == []
        nt = {c.check: c for c in s.verification.checks}["numbers_traced"]
        assert (nt.result, nt.detail) == ("pass", "No numbers to trace")
    print(f"\n{name}: {content} content and {gap} gap sentences, {brief.stats.words} words, "
          f"est_pages {brief.stats.est_pages}, {len(st.holes)} holes")


@pytest.mark.parametrize("name", ["pin2ve", "r0b"])
async def test_wordy_draft_loses_content_first_and_keeps_every_category(settings, name):
    """R0b's failure (simulated): a draft far over three pages. Before, Financial Strength was cut whole while
    the hole bullets stayed. Now extra content goes first, each category keeps a sentence, the gaps stay."""
    ctx = stored_ctx(settings, name)
    st = ctx.state

    def wordy(system: str, user: str, schema: dict) -> dict:
        out = summarizer.fake_brief(system, user, schema)
        p = summarizer._tagged(user, "report")
        for sec in out["sections"]:
            cat = next((c for c, lbl in CAT_LABEL.items() if lbl == sec["heading"]), None)
            extra = [{"text": i["text"], "mode": "copied", "derived_from": [i["id"]]} for i in p["items"]
                     if i["category"] == cat and i["kind"] == "data"][:25]
            if extra:
                sec["blocks"].append({"type": "bullets", "sentences": extra})
        return out

    bedrock.register_fake(BRIEF, wordy)
    await summarize.run(ctx)
    cut_note = next(e.data.text for e in st.events if e.type == "agent.log" and "cut to fit" in e.data.text)
    assert int(cut_note.split()[0]) >= 20
    brief = st.summary
    assert brief.stats.est_pages <= 3.0
    assert len(gap_sents(brief)) == 5                              # content was cut, the gap summary was not
    want = {RUBRIC["criteria"][k]["category"] for k in RUBRIC["depths"]["screen"]}
    assert covered(brief, st.items) >= want
    await verify_summary.run(ctx)
    assert st.summary.stats.est_pages <= 3.0 and covered(st.summary, st.items) >= want


async def test_pin2ve_gap_sentences_read_as_written(settings):
    ctx = stored_ctx(settings, "pin2ve")
    texts = [s.text for s in summarize.gap_sentences(ctx.state)]
    assert texts == [
        "The full hole register lists thirteen gaps for this run.",
        "Secular growth and Leverage are not scored because their scores did not pass verification.",
        "Research stage is left to the analyst, because it rests on the analyst's own coverage history, "
        "which no source in this run can state.",
        "Five data items and four conclusions failed verification, so they are left out of the scores "
        "(six for Secular growth, one for ROTIC level, one for ROTIC vs peers and one for Leverage).",
        "One planned source, a planted test page (simulated), was quarantined after the prompt-attack scan.",
    ]


async def test_r0b_style_draft_with_one_bullet_per_hole_is_replaced(settings):
    """The R0b summarizer wrote 25 hole bullets (simulated here): code puts its five-sentence summary instead."""
    ctx = stored_ctx(settings, "pin2ve")

    def per_hole(system: str, user: str, schema: dict) -> dict:
        out = summarizer.fake_brief(system, user, schema)
        p = summarizer._tagged(user, "report")
        out["sections"] = [s for s in out["sections"] if s["heading"] != GAP]
        out["sections"].append({"heading": GAP, "blocks": [{"type": "bullets", "sentences": [
            {"text": "A data point was excluded.", "mode": "derived", "derived_from": [h["id"]]}
            for h in p["holes"]]}]})
        out["sections"][1]["blocks"].append({"type": "paragraph", "sentences": [
            {"text": "Leverage could not be scored.", "mode": "derived", "derived_from": ["HOLE-02"]}]})
        return out

    bedrock.register_fake(BRIEF, per_hole)
    await summarize.run(ctx)
    brief = ctx.state.summary
    assert [s.text for s in gap_sents(brief)] == [s.text for s in summarize.gap_sentences(ctx.state)]
    assert "A data point was excluded." not in {s.text for s in sents(brief)}
    assert "Leverage could not be scored." not in {s.text for s in sents(brief)}
    text = logs(ctx)
    assert "cite only holes, and code writes the gap summary" in text
    assert "gap sentence(s) were replaced by code's 5-sentence gap summary" in text
    assert "content and 5 gap sentence(s), 13 hole(s) in the register" in text


async def test_draft_without_a_gap_section_gets_none_added(settings):
    ctx = stored_ctx(settings, "pin2ve")

    def no_gaps(system: str, user: str, schema: dict) -> dict:
        out = summarizer.fake_brief(system, user, schema)
        out["sections"] = [s for s in out["sections"] if s["heading"] != GAP]
        return out

    bedrock.register_fake(BRIEF, no_gaps)
    await summarize.run(ctx)
    assert not gap_sents(ctx.state.summary)
    assert "the draft has no gap section, so none was added" in logs(ctx)


async def test_summarizer_is_told_the_gap_summary_and_the_categories(settings):
    seen: list[str] = []

    def spy(system: str, user: str, schema: dict) -> dict:
        seen.append(system + "\n" + user)
        return summarizer.fake_brief(system, user, schema)

    ctx = stored_ctx(settings, "pin2ve")
    bedrock.register_fake(BRIEF, spy)
    await summarize.run(ctx)
    p = summarizer._tagged(seen[0], "report")
    assert [g["text"] for g in p["gap_summary"]] == [s.text for s in summarize.gap_sentences(ctx.state)]
    assert [c["category"] for c in p["categories_in_scope"]] == [
        "Dominant Business", "Compounder", "Financial Strength", "Research Stage"]
    gap_words = sum(len(g["text"].split()) for g in p["gap_summary"])
    assert p["word_budget"] == summarize.word_budget(2, reserved_words=gap_words)
    assert "copied exactly as given" in seen[0] and "not counting the gap_summary sentences" in seen[0]
    assert "one per hole" not in seen[0]


# ---------------------------------------------------------------------------
# Judges see a count made by code
# ---------------------------------------------------------------------------


async def test_judges_of_a_counted_gap_sentence_see_the_code_tally(settings):
    prompts: list[str] = []

    def spy(system: str, user: str, schema: dict) -> dict:
        prompts.append(user)
        return summarizer.fake_judge(system, user, schema)

    ctx = stored_ctx(settings, "pin2ve")
    bedrock.register_fake(JUDGE, spy)
    await summarize.run(ctx)
    await verify_summary.run(ctx)
    lead = "The full hole register lists thirteen gaps for this run."
    mine = [p for p in prompts if f"<sentence>\n{lead}\n</sentence>" in p]
    assert len(mine) == 3
    ev = summarizer._tagged(mine[0], "evidence")
    assert ev[0]["id"] == "hole_count" and ev[0]["holes"] == 13 and ev[0]["sources_not_used"] == 1
    assert ev[0]["items_that_failed_verification"] == {"data items": 5, "conclusions": 4}
    assert {e["id"] for e in ev[1:]} == {h.hole_id for h in ctx.state.holes}
    e003 = next(e for e in ev if e["id"] == "HOLE-01")
    assert (e003["item_kind"], e003["item_status"]) == ("extrapolation", "fail")


def test_tally_survives_the_evidence_limit():
    holes = [m.Hole(hole_id=f"HOLE-{k:02d}", item_id=None, reason=f"S-{k:03d} (a source) could not be fetched.")
             for k in range(1, 61)]
    ev = summarizer.evidence_for([h.hole_id for h in holes], {}, holes, None)
    assert len(ev) == summarizer.EVIDENCE_LIMIT
    assert ev[0]["id"] == "hole_count" and ev[0]["holes"] == 60 and ev[0]["sources_not_used"] == 60
    one = summarizer.evidence_for(["HOLE-01"], {}, holes, None)
    assert [e["id"] for e in one] == ["HOLE-01"]                 # a single hole needs no count


async def test_a_gap_sentence_still_faces_its_judges(settings):
    """Judges that reject a gap sentence's count (simulated) get it weakened like any other sentence."""
    lead = "The full hole register lists thirteen gaps for this run."

    def reject_count(system: str, user: str, schema: dict) -> dict:
        bad = "thirteen" in user.split("<sentence>\n", 1)[1].split("\n</sentence>", 1)[0]
        return {"supported": not bad, "unsupported_spans": ["thirteen"] if bad else [], "reason": "Count is off."}

    ctx = stored_ctx(settings, "pin2ve")
    bedrock.register_fake(JUDGE, reject_count)
    await summarize.run(ctx)
    sid = next(s.id for s in sents(ctx.state.summary) if s.text == lead)
    await verify_summary.run(ctx)
    first = next(e.data for e in ctx.state.events if e.type == "summary.verified" and e.data.sentence_id == sid)
    faith = {c.check: c for c in first.verification.checks}["faithfulness"]
    assert (first.verification.status, faith.result, faith.votes_for) == ("fail", "fail", 0)
    kept = next(s for s in sents(ctx.state.summary) if s.id == sid)
    assert kept.text == "The full hole register lists gaps for this run."
    assert kept.verification.status == "fixed" and kept.verification.attempts[0].action == "downgrade"


# ---------------------------------------------------------------------------
# The gap sentences themselves
# ---------------------------------------------------------------------------


def test_count_words():
    words = {0: "zero", 1: "one", 13: "thirteen", 20: "twenty", 25: "twenty-five", 99: "ninety-nine",
             100: "one hundred", 353: "three hundred fifty-three", 1201: "one thousand two hundred one"}
    for n, w in words.items():
        assert summarize.count_words(n) == w


def _hole(k: int, *, criterion=None, item_id=None, reason="x") -> m.Hole:
    return m.Hole(hole_id=f"HOLE-{k:02d}", criterion=criterion, item_id=item_id, reason=reason)


async def test_every_hole_kind_fits_five_sentences(settings, fixture_bundle, fixture_report):
    """Sources revoked and blocked, a failed extractor agent, an unverified item, a missing score."""
    ctx = report_ctx(settings, fixture_bundle, fixture_report)
    st = ctx.state
    srcs = st.sources_by_id()
    srcs["S-006"].status, srcs["S-008"].status, srcs["S-009"].status = "revoked", "revoked", "blocked"
    st.items["C-005"] = st.items["C-005"].model_copy(update={"verification": m.Verification(status="blocked")})
    st.holes = list(st.holes) + [
        _hole(5, criterion="leverage", reason="No score was drafted for this criterion."),
        _hole(6, item_id="C-005", reason="Conclusion could not be verified (source revoked: S-006)."),
        _hole(7, reason="S-006 (Lab Business Weekly) was revoked by the analyst."),
        _hole(8, reason="S-008 (Federal Register) was revoked by the analyst."),
        _hole(9, reason="S-009 (r/labrats) was blocked by the gateway."),
        _hole(10, reason="extractor-07 failed: Extract S-002 pages 40 to 44, so it gave no data items."),
        _hole(11, reason="Something else entirely."),
    ]
    gaps = summarize.gap_sentences(st)
    assert len(gaps) == 5
    texts = [g.text for g in gaps]
    assert texts[0] == "The full hole register lists eleven gaps for this run."
    assert "ROTIC vs peers is not scored because the verified evidence does not determine it" in texts[1]
    assert "Leverage is not scored because no score was drafted for it" in texts[1]
    assert texts[2].startswith("Research stage is left to the analyst")
    assert texts[3].startswith("One conclusion failed verification, and one conclusion could not be verified, "
                               "so they are left out of the scores")
    assert texts[4] == ("Four planned sources were not used: one could not be used, two were revoked by the "
                        "analyst and one was blocked by the gateway, and one extractor agent failed, so its pages "
                        "gave no data items.")
    assert gaps[4].derived_from == ["HOLE-04", "HOLE-07", "HOLE-08", "HOLE-09", "HOLE-10"]
    assert "HOLE-11" in gaps[0].derived_from                      # an unsorted hole is still counted and cited
    for g in gaps:
        assert not re.search(r"\d", g.text)
        res = verify_summary.code_checks(st, g.model_copy(update={"id": "H-900"}))
        assert [c.result for c in res] == ["pass", "pass"], res


async def test_no_holes_means_no_gap_section(settings, fixture_bundle, fixture_report):
    ctx = report_ctx(settings, fixture_bundle, fixture_report)
    ctx.state.holes = []
    assert summarize.gap_sentences(ctx.state) == []
    await summarize.run(ctx)
    assert not gap_sents(ctx.state.summary)


# ---------------------------------------------------------------------------
# Trimming: content first, never a whole category, gaps down to two
# ---------------------------------------------------------------------------


def _sent(text: str, refs: list[str]) -> m.SummarySentence:
    return m.SummarySentence(id="", text=text, mode="derived", derived_from=refs,
                             verification=m.Verification(status="pending"))


def _long(k: int) -> str:
    return " ".join(["word"] * k) + "."


def _by_cat(report: m.AgentReport) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in report.items:
        out.setdefault(it.category, it.id)
    return out


def _gap_block(n: int) -> m.SummarySection:
    return m.SummarySection(heading=GAP, blocks=[m.BulletsBlock(sentences=[
        _sent(f"Gap sentence {summarize.count_words(k)} stays.", [f"HOLE-0{k}"]) for k in range(1, n + 1)])])


def test_trim_cuts_extra_content_before_any_gap_sentence(fixture_report):
    ids = _by_cat(fixture_report)
    items = {i.id: i for i in fixture_report.items}
    comp, fin, dom = ids["compounder"], ids["financial_strength"], ids["dominant_business"]
    secs = [m.SummarySection(heading="Bottom line", blocks=[m.ParagraphBlock(sentences=[_sent(_long(30), ["rollup"])])]),
            m.SummarySection(heading="Compounder", blocks=[m.ParagraphBlock(sentences=[
                _sent(_long(60), [comp]) for _ in range(25)])]),
            # A mixed section: its only Financial Strength sentence must survive, wherever it sits.
            m.SummarySection(heading="Mixed", blocks=[m.BulletsBlock(sentences=[
                _sent(_long(60), [fin]), *[_sent(_long(60), [comp]) for _ in range(5)]])]),
            m.SummarySection(heading="Dominant Business", blocks=[m.ParagraphBlock(sentences=[_sent(_long(40), [dom])])]),
            m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()]),
            _gap_block(5)]
    fitted, cut = summarize.fit_pages(secs, items=items)
    assert summarize.estimate_pages(fitted) <= 3.0 and cut
    assert all(not summarize.hole_only(s) for s in cut)           # content went first
    heads = [s.heading for s in fitted]
    assert heads == ["Bottom line", "Compounder", "Mixed", "Dominant Business", "Scorecard", GAP]
    kept = [s for _, s in summarize.all_sentences(fitted)]
    assert any(fin in s.derived_from for s in kept) and any(dom in s.derived_from for s in kept)
    assert sum(summarize.hole_only(s) for s in kept) == 5


def test_trim_takes_gaps_down_to_two_and_never_drops_a_category(fixture_report):
    ids = _by_cat(fixture_report)
    items = {i.id: i for i in fixture_report.items}
    secs = [m.SummarySection(heading="Bottom line", blocks=[m.ParagraphBlock(sentences=[
                _sent(_long(120), ["rollup"]), _sent(_long(120), ["rollup"])])])]
    for cat in ("dominant_business", "compounder", "financial_strength", "research_stage"):
        secs.append(m.SummarySection(heading=CAT_LABEL[cat], blocks=[m.ParagraphBlock(sentences=[
            _sent(_long(240), [ids[cat]])])]))
    secs += [m.SummarySection(heading="Scorecard", blocks=[m.ScorecardBlock()]), _gap_block(5)]
    fitted, cut = summarize.fit_pages(secs, items=items)
    kept = [s for _, s in summarize.all_sentences(fitted)]
    assert sum(summarize.hole_only(s) for s in kept) == summarize.HOLE_FLOOR
    assert [s.heading for s in fitted][1:5] == [CAT_LABEL[c] for c in
                                               ("dominant_business", "compounder", "financial_strength", "research_stage")]
    assert len([s for s in kept if "rollup" in s.derived_from]) == 1   # the bottom line went down to one
    assert summarize.estimate_pages(fitted) > 3.0                 # still long: categories are never dropped
    assert [s.text for s in cut[:3]] == ["Gap sentence five stays.", "Gap sentence four stays.",
                                         "Gap sentence three stays."]


def test_split_counts_and_gap_detection():
    secs = [m.SummarySection(heading="Bottom line", blocks=[m.ParagraphBlock(sentences=[_sent("A.", ["rollup"])])]),
            m.SummarySection(heading="what we could NOT determine", blocks=[m.BulletsBlock(sentences=[
                _sent("B.", ["HOLE-01"]), _sent("C.", ["HOLE-02", "E-005"])])])]
    assert summarize.is_gap_section(secs[1]) and not summarize.is_gap_section(secs[0])
    assert summarize.split_counts(secs) == (1, 2)


# ---------------------------------------------------------------------------
# Live (AXELROD_LIVE=1): summarize plus verify_summary on a copy of the G5 run
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_brief_on_a_copy_of_the_g5_run(settings):
    """Real Sonnet and Haiku through the gateway, local store and vault in a short DATA_DIR. The copy gets a
    fresh meter capped at $0.50, so the gateway itself enforces the spend limit."""
    data_dir = Path(os.environ.get("FIXHOLES_DATA_DIR") or "backend/.data/fixholes")
    live = settings.model_copy(update={"store": "local", "fake_models": False, "data_dir": data_dir})
    state = RunState.model_validate_json(_stored_json("pin2ve"))
    state.run.usage = m.Usage(budget_usd=0.50)
    state.run.notes = list(state.run.notes) + ["Test copy of run_20261108_pin2ve: brief re-written by B-fix-holes."]
    store = Store(live)
    store.put(state)
    ctx = RunContext.create(state, store, store.vault, live)
    await summarize.run(ctx)
    await verify_summary.run(ctx)
    brief = ctx.state.summary
    m.HumanReport.model_validate(brief.model_dump(mode="json"))
    content, gap = summarize.split_counts(brief.sections)
    cost = ctx.state.run.usage.cost_usd
    print(f"\n{brief.stats}\ncontent {content}, gap {gap}, holes {len(state.holes)}, cost ${cost:.4f}\n"
          f"categories {sorted(CAT_LABEL[c] for c in covered(brief, state.items))}")
    for sec in brief.sections:
        print(f"## {sec.heading}")
        for _, s in summarize.all_sentences([sec]):
            print(f"  {s.id} {s.mode} {s.verification.status} {s.derived_from}: {s.text}")
    print(logs(ctx))
    (data_dir / "fixholes_brief.json").write_text(json.dumps(brief.model_dump(mode="json"), indent=1,
                                                             ensure_ascii=False), encoding="utf-8")
    assert brief.status == "final" and brief.stats.est_pages <= 3.0
    assert gap <= 5 and content > gap
    assert cost < 0.50
