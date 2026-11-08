"""B-fix-polish: the gap summary is always in the brief, numbers in words are traced, ranges vouch for both
bounds, the register's Markdown heading, and exact quotes on the demo paths. All offline (FAKE_MODELS=1).

Data: the mock fixture (fictional company), the committed G5 replay `backend/data/replay/run_20261108_pin2ve`
(read only, copied into a temp DATA_DIR), and the planted pages of test_planted.py (simulated, fictional company).
Range items, planted sentences and fakes below are simulated. Run with -s to see the planted line.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter

import test_planted as tp
from backend.axelrod import bedrock, demo
from backend.axelrod.agents import summarizer
from backend.axelrod.agents.base import tool_name
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.ids import new_run_id, now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import summarize, verify_summary
from backend.axelrod.store import Store
from backend.axelrod.store.state import RunState
from backend.axelrod.verify import checks

BACKEND = Path(__file__).resolve().parents[1]
PIN2VE = BACKEND / "data" / "replay" / "run_20261108_pin2ve" / "state.json"
G5B = BACKEND / ".data" / "g5b_report.json"          # the EC2 run run_20261108_0murfv, never committed
BRIEF = tool_name(summarizer.BriefDraft)
JUDGE = tool_name(summarizer.FaithfulnessVerdict)
REWRITE = tool_name(summarizer.BriefRewrite)
GAP = summarize.HOLE_HEADING
ITEM = TypeAdapter(m.Item)


@pytest.fixture(autouse=True)
def _default_fakes():
    yield
    bedrock.register_fake(BRIEF, summarizer.fake_brief)
    bedrock.register_fake(JUDGE, summarizer.fake_judge)
    bedrock.register_fake(REWRITE, summarizer.fake_rewrite)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def report_ctx(settings, bundle: dict, report: m.AgentReport) -> RunContext:
    """A run holding the mock fixture's final report (fictional company), ready for summarize."""
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


def pin2ve_state() -> RunState:
    state = RunState.model_validate_json(PIN2VE.read_text(encoding="utf-8"))
    state.events, state.summary, state.summary_ready = [], None, False
    return state


def pin2ve_ctx(settings) -> RunContext:
    state = pin2ve_state()
    store = Store(settings)
    store.put(state)
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy(emit=False)      # the recorded policy names the recorded model ids, not the fakes'
    return ctx


def no_gap_draft(system: str, user: str, schema: dict) -> dict:
    """Summarizer fake (simulated): the offline brief without any gap section."""
    out = summarizer.fake_brief(system, user, schema)
    out["sections"] = [s for s in out["sections"] if s["heading"] != GAP]
    return out


def logs(ctx: RunContext) -> str:
    return " | ".join(e.data.text for e in ctx.state.events if e.type == "agent.log")


def gap_texts(brief: m.HumanReport) -> list[str]:
    return [s.text for sec in brief.sections if sec.heading == GAP for _, s in summarize.all_sentences([sec])]


def nt(results: list[m.CheckResult]) -> m.CheckResult:
    return next(c for c in results if c.check == "numbers_traced")


def traced(text_: str, quote: str) -> m.CheckResult:
    """numbers_traced of a text against the numbers one quote vouches for."""
    return checks._numbers_traced(text_, checks._text_cands("Q quote", quote), what="sentence")


# ---------------------------------------------------------------------------
# 1. The brief always carries code's gap summary when the run has holes
# ---------------------------------------------------------------------------


async def test_draft_without_gap_section_still_gets_code_gap_summary(settings, fixture_bundle, fixture_report):
    ctx = report_ctx(settings, fixture_bundle, fixture_report)
    bedrock.register_fake(BRIEF, no_gap_draft)
    await summarize.run(ctx)
    want = [g.text for g in summarize.gap_sentences(ctx.state)]
    assert want and ctx.state.summary.sections[-1].heading == GAP
    assert gap_texts(ctx.state.summary) == want
    assert f"the draft has no gap section, so code added its {len(want)}-sentence gap summary" in logs(ctx)
    await verify_summary.run(ctx)
    brief = ctx.state.summary
    assert brief.status == "final" and brief.sections[-1].heading == GAP and brief.stats.est_pages <= 3.0
    assert gap_texts(brief) == want
    assert all(s.verification.status in ("pass", "fixed") for sec in brief.sections if sec.heading == GAP
               for _, s in summarize.all_sentences([sec]))


async def test_recorded_run_without_drafted_gaps_gets_the_five_sentence_summary(settings):
    ctx = pin2ve_ctx(settings)
    bedrock.register_fake(BRIEF, no_gap_draft)
    await summarize.run(ctx)
    brief = ctx.state.summary
    assert brief.sections[-1].heading == GAP
    assert gap_texts(brief) == [g.text for g in summarize.gap_sentences(ctx.state)]
    assert len(gap_texts(brief)) == 5 and brief.stats.est_pages <= 3.0
    assert "content and 5 gap sentence(s), 13 hole(s) in the register" in logs(ctx)


async def test_run_without_holes_has_no_gap_section(settings, fixture_bundle, fixture_report):
    ctx = report_ctx(settings, fixture_bundle, fixture_report)
    ctx.state.holes = []
    await summarize.run(ctx)
    assert GAP not in [sec.heading for sec in ctx.state.summary.sections]
    assert "gap summary" not in logs(ctx)


def test_place_gaps_appends_whatever_the_draft_says():
    gaps = [summarize._gap("The full hole register lists two gaps for this run.",
                           [m.Hole(hole_id="HOLE-01", reason="x"), m.Hole(hole_id="HOLE-02", reason="y")])]
    empty = summarizer.BriefDraft.model_validate({"sections": []})
    secs: list[m.SummarySection] = []
    notes = summarize.place_gaps(secs, empty, gaps)
    assert [s.heading for s in secs] == [GAP] and "code added its 1-sentence gap summary" in notes[0]
    assert summarize.place_gaps([], empty, []) == []


# ---------------------------------------------------------------------------
# 2. numbers_traced reads amounts written as words. Counts stay exempt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase, raw, number, pct, word", [
    ("covering twenty percent of the scale", "twenty percent", "20", True, None),
    ("zero percent of the maximum", "zero percent", "0", True, None),
    ("twenty-five per cent", "twenty-five per cent", "25", True, None),
    ("four point five percent", "four point five percent", "4.5", True, None),
    ("about three billion", "three billion", "3", False, "billion"),
    ("one point two billion euros", "one point two billion euros", "1.2", False, "billion"),
    ("more than twenty-five thousand rights", "twenty-five thousand", "25", False, "thousand"),
    ("fifteen hundred million", "fifteen hundred million", "1500", False, "million"),
    ("half a billion euros", "half a billion euros", "0.5", False, "billion"),
    ("a million more", "a million", "1", False, "million"),
    ("five euros per share", "five euros", "5", False, None),
    ("two percentage points", "two percentage points", "2", False, None),
    ("one third of revenue", "one third", "33", True, None),
    ("two-thirds of sales", "two-thirds", "67", True, None),
    ("a quarter of revenue", "a quarter", "25", True, None),
    ("over half of the world population", "half", "50", True, None),
])
def test_amounts_in_words_are_read(phrase, raw, number, pct, word):
    said = checks._said_in(phrase)
    assert len(said) == 1
    t = said[0].reads[0]
    assert (said[0].tok.raw, str(t.number), t.pct, t.word) == (raw, number, pct, word)


@pytest.mark.parametrize("phrase", [
    "The full hole register lists thirteen gaps for this run.",
    "Five data items and four conclusions failed verification (six for Secular growth, one for ROTIC level).",
    "One planned source, a planted test page (simulated), was quarantined after the prompt-attack scan.",
    "two of three judges agreed", "over three years", "a five-year plan", "three hundred and five data items",
    "Revenue grew for a third year in a row.", "in the third quarter", "the first half of fiscal 2025",
    "over the last three quarters", "three quarters of fiscal 2026", "at one point the company", "someone, none",
    "the One Big Beautiful Bill Act",
])
def test_counts_and_ordinals_in_words_are_not_traced(phrase):
    assert checks._said_in(phrase) == []
    assert traced(phrase, "").detail == "No numbers to trace"


def test_recorded_gap_summary_has_nothing_to_trace():
    state = pin2ve_state()
    for s in summarize.gap_sentences(state):
        res = checks.check_sentence(s, state.items, state.holes, rollup=state.rollup, rubric=RUBRIC)
        assert (nt(res).result, nt(res).detail) == ("pass", "No numbers to trace"), s.text


def test_h027_twenty_percent_traces_to_the_rollup_and_a_wrong_one_fails():
    """Seen live (H-027): 'twenty percent' was never checked. It is right for pin2ve (normalized_pct 20)."""
    state = pin2ve_state()
    text_ = ("Siemens Healthineers receives a weighted average score of -0.6 on this framework, with three of six "
             "in-scope criteria scored, covering twenty percent of the normalized scale and zero percent of the "
             "maximum possible score.")
    s = m.SummarySentence(id="H-927", text=text_, mode="derived", derived_from=["rollup"],
                          verification=m.Verification(status="pending"))
    ok = nt(checks.check_sentence(s, state.items, state.holes, rollup=state.rollup, rubric=RUBRIC))
    assert ok.result == "pass" and "(3 checked)" in ok.detail
    wrong = state.rollup.model_copy(update={"normalized_pct": 25})
    bad = nt(checks.check_sentence(s, state.items, state.holes, rollup=wrong, rubric=RUBRIC))
    assert bad.result == "fail" and bad.unsupported_spans == ["twenty percent"]


def test_fractions_trace_to_percent_or_ratio_at_their_precision():
    assert traced("Consumables were about one third of revenue.", "Consumables were 33.4% of revenue").result == "pass"
    assert traced("Consumables were about one third of revenue.", "a ratio of 0.334").result == "pass"
    assert traced("Consumables were about one third of revenue.", "Consumables were 35% of revenue").result == "fail"
    assert traced("33%", "roughly one-third of total revenue").result == "pass"     # words in a quote vouch too
    assert traced("twenty percent", "twenty percent of revenue").result == "pass"


def test_negative_amount_in_words_needs_its_sign():
    cands = [checks._Cand(Decimal(-148), Decimal(-148_000_000), False, "D-907")]
    ok = checks._numbers_traced("Financial income, net was minus one hundred forty-eight million euros.", cands,
                                what="conclusion")
    bad = checks._numbers_traced("Financial income, net was one hundred forty-eight million euros.", cands,
                                 what="conclusion")
    assert ok.result == "pass"
    assert bad.result == "fail" and "is shown without its minus sign: D-907 is -148" in bad.detail


# Planted errors in words, next to clean twins (simulated, the planted pages' fictional company).
WORD_CASES = [
    tp.Case("percent in words the data does not say", "numbers_traced",
            tp.conclusion("C-931", "Comparable revenue grew five point one percent in fiscal year 2025.", ["D-906"]),
            tp.conclusion("C-931", "Comparable revenue grew twenty percent in fiscal year 2025.", ["D-906"])),
    tp.Case("scale in words off by a billion", "numbers_traced",
            tp.conclusion("C-932", "Free cash flow was about two billion euros in fiscal year 2025.", ["D-905"]),
            tp.conclusion("C-932", "Free cash flow was about three billion euros in fiscal year 2025.", ["D-905"])),
    tp.Case("fraction in words the computation does not give", "numbers_traced",
            tp.conclusion("C-933", "Total financial debt was about two thirds of revenue in fiscal year 2025.",
                          ["D-903", "D-904", "D-901"], [("debt_to_revenue", "(D-903 + D-904) / D-901", "0.67")]),
            tp.conclusion("C-933", "Total financial debt was about three quarters of revenue in fiscal year 2025.",
                          ["D-903", "D-904", "D-901"], [("debt_to_revenue", "(D-903 + D-904) / D-901", "0.67")])),
    tp.Case("minus dropped from an amount in words", "numbers_traced",
            tp.conclusion("C-934", "Financial income, net was minus one hundred forty-eight million euros in fiscal "
                                   "year 2025.", ["D-907"]),
            tp.conclusion("C-934", "Financial income, net was one hundred forty-eight million euros in fiscal year "
                                   "2025.", ["D-907"])),
]


def test_planted_errors_in_words_caught_and_clean_twins_pass():
    results = [(case, *tp._outcome(case)) for case in WORD_CASES]
    caught = sum(r[1] for r in results)
    clean = sum(r[2] for r in results)
    print(f"\nplanted (numbers in words): {caught}/{len(WORD_CASES)} caught, clean: {clean}/{len(WORD_CASES)} pass")
    bad = [f"{case.name}: {why}" for case, c, ok, why in results if not (c and ok)]
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------------------
# 2b. Rubric thresholds trace in any equal format. Ranges vouch for both bounds (EC2 run_20261108_0murfv)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bar, result", [("10%", "pass"), ("10.0%", "pass"), ("10.00%", "pass"),
                                         ("10 percent", "pass"), ("ten percent", "pass"), ("ten per cent", "pass"),
                                         ("11%", "fail"), ("eleven percent", "fail")])
def test_rubric_threshold_traces_in_any_equal_format(bar, result):
    """rotic_level's rubric says '< 10%'. The comparison is numeric, so every way of writing ten percent traces."""
    c = tp.conclusion("C-935", f"Comparable revenue grew 5.1% in fiscal year 2025, below the {bar} bar.", ["D-906"],
                      criteria=("rotic_level",))
    assert nt(tp.run_item(c)).result == result


def test_secular_growth_has_no_numeric_threshold():
    """The EC2 failure ('5.0%' in C-002 and C-004) was not a rubric number: secular_growth is judged, with none."""
    crit = RUBRIC["criteria"]["secular_growth"]
    assert crit["scoring"] == "judgment"
    assert not any(re.search(r"\d", str(crit.get(k) or "")) for k in ("label", "plus_one", "zero", "minus_one"))


def range_item(iid: str, as_reported: str, number: float = 4.0) -> m.DataItem:
    """A verified outlook range (simulated), as the SHL extractor stores one: value is the first bound."""
    quote = f"For fiscal year 2027, comparable revenue growth is expected to be between {as_reported}."
    return m.DataItem(
        id=iid, category="compounder", criteria=["secular_growth"], need_id="N-01",
        statement=f"Comparable revenue growth is expected to be between {as_reported} in fiscal year 2027.",
        value=m.Value(number=number, as_reported=as_reported, unit="%", scale="one", period="FY2027"),
        citations=[m.Citation(doc_id=tp.DOC, locator=m.TextLocator(page=1, char_start=0, char_end=len(quote)),
                              quote=quote, role="evidence")],
        source_id="S-901", source_tier="priority", extracted_by="agent", agent_id="extractor-01",
        verification=m.Verification(status="pass"))


def outlook(cid: str, text_: str, did: str) -> m.Conclusion:
    return m.Conclusion(id=cid, category="compounder", criteria=["secular_growth"], text=text_, data_ids=[did],
                        agent_id="drafter-01", verification=m.Verification(status="pending"))


@pytest.mark.parametrize("as_reported, said, result", [
    ("4.0% and 5.0%", "between 4.0% and 5.0%", "pass"),
    ("4.0% and 5.0%", "between 4% and 5%", "pass"),
    ("4.0% and 5.0%", "between 4.0% and 6.0%", "fail"),
    ("4% to 5%", "from 4% to 5%", "pass"),
    ("4–5%", "between 4% and 5%", "pass"),
])
def test_range_value_vouches_for_both_bounds(as_reported, said, result):
    d = range_item("D-936", as_reported)
    c = outlook("C-936", f"Management expects comparable revenue growth {said} in fiscal year 2027.", "D-936")
    res = nt(checks.check_conclusion(c, {**tp.ITEMS, d.id: d}, tp.SOURCES, rubric=RUBRIC))
    assert res.result == result, res.detail
    if result == "fail":
        assert res.unsupported_spans == ["6.0%"]


@pytest.mark.parametrize("as_reported, other", [("4.0% and 5.0%", "5.0"), ("4% to 5%", "5"), ("4–5%", "5"),
                                                ("4 – 5%", "5"), ("4.0%–5.0%", "5.0"), ("−4% to −2%", "-2"),
                                                ("between 4% and 5%", "5")])
def test_range_bound_keeps_its_printed_sign(as_reported, other):
    first = checks.scan_numbers(as_reported.removeprefix("between "), include_labels=True)[0].number
    v = m.Value(number=float(first), as_reported=as_reported, unit="%", scale="one", period="FY2027")
    assert [str(c.number) for c in checks._range_cands("D-1", v)] == [other]


def test_two_numbers_that_are_not_a_range_add_nothing():
    row = m.Value(number=23630, as_reported="23,630 23,375", unit="EUR", scale="million", period="FY2026")
    assert checks._range_cands("D-1", row) == []
    note = m.Value(number=1894, as_reported="1,894 (2024: 1,667)", unit="EUR", scale="million", period="FY2025")
    assert checks._range_cands("D-1", note) == []


@pytest.mark.skipif(not G5B.exists(), reason="backend/.data/g5b_report.json is never committed")
def test_ec2_run_conclusions_with_the_outlook_range_now_trace():
    """run_20261108_0murfv (read only): C-002 and C-004 failed numbers_traced on '5.0%' from 'between 4.0% and
    5.0%', the upper bound of D-211 and D-219 (as_reported '4.0% and 5.0%')."""
    report = m.AgentReport.model_validate_json(G5B.read_text(encoding="utf-8"))
    items = {i.id: i for i in report.items}
    for cid in ("C-002", "C-004"):
        res = nt(checks.check_conclusion(items[cid], items, {}, rubric=RUBRIC))
        assert res.result == "pass", (cid, res.detail)


# ---------------------------------------------------------------------------
# 3. The register's heading in human-report.md
# ---------------------------------------------------------------------------


async def test_markdown_register_heading_says_it_is_outside_the_brief(settings, fixture_bundle, fixture_report):
    ctx = report_ctx(settings, fixture_bundle, fixture_report)
    await summarize.run(ctx)
    await verify_summary.run(ctx)
    md = verify_summary.render_markdown(ctx.state.summary, rollup=ctx.state.rollup, items=ctx.state.items)
    assert "\n## Holes register (full list, not part of the three-page brief)\n" in md
    assert not re.search(r"^## Holes$", md, re.MULTILINE)
    assert md.index(f"## {GAP}") < md.index("## Holes register")
    assert all(f"- {h.hole_id}: {h.reason}" in md for h in fixture_report.holes)


# ---------------------------------------------------------------------------
# 4. Demo paths store the exact span text
# ---------------------------------------------------------------------------


def fixture_state(bundle: dict) -> RunState:
    """The mock's finished report (fictional company) as a live RunState with stored page texts."""
    b = copy.deepcopy({k: v for k, v in bundle.items() if k != "page_text"})
    now = now_iso()
    run = m.Run(run_id=new_run_id(), company=b["company"], ticker=b["ticker"], status="done", stage="complete",
                created_at=now, updated_at=now, created_via="api", notes="test fixture (fictional company)",
                counts=m.RunCounts(), usage=m.Usage(budget_usd=5.0))
    state = RunState(
        run=run, sources=[m.Source.model_validate(s) for s in b["sources"]],
        documents=[m.Document.model_validate(d) for d in b["docs"]],
        pages={d["doc_id"]: [bundle["page_text"](d["doc_id"], p) for p in range(1, d["pages"] + 1)]
               for d in b["docs"]},
        items={i["id"]: ITEM.validate_python(i) for i in b["items"]})
    state.recount()
    return state


def spread(item: m.DataItem) -> m.DataItem:
    """Every text quote with its first space doubled: equal after normalization, not the same characters."""
    new = item.model_copy(deep=True)
    new.citations = [c.model_copy(update={"quote": c.quote.replace(" ", "  ", 1)})
                     if isinstance(c.locator, m.TextLocator) else c for c in new.citations]
    return new


def exact(state: RunState, item: m.DataItem) -> bool:
    pages = demo._pages(state)
    return all(c.quote == checks.span_text(pages, c.doc_id, c.locator)
               for c in item.citations if isinstance(c.locator, m.TextLocator))


def with_headers(state: RunState) -> m.DataItem:
    """D-028 ("Total debt 1,150", unit header "Contractual obligations (In millions)") plus the period line its
    page prints, so it passes today's period check (the fixture predates it). Simulated, fictional company."""
    it = state.items["D-028"]
    page = state.pages["DOC-001"][2]
    q = "September 30, 2025"
    s = page.index(q)
    cit = m.Citation(doc_id="DOC-001", locator=m.TextLocator(page=3, char_start=s, char_end=s + len(q)), quote=q,
                     role="period_context")
    it = it.model_copy(update={"citations": it.citations + [cit]})
    res = demo._code_checks(it, state.items, state.sources_by_id(), demo._pages(state))
    it = it.model_copy(update={"verification": m.Verification(status=checks.status_from(res), checks=res)})
    assert it.verification.status == "pass" and exact(state, it)
    state.items[it.id] = it
    return it


@pytest.fixture
def fx_ctx(settings, tmp_path, fixture_bundle) -> RunContext:
    state = fixture_state(fixture_bundle)
    store = Store(settings, replay_dir=tmp_path / "replay")
    store.put(state)
    return RunContext.create(state, store, store.vault, settings)


async def test_analyst_edit_stores_the_exact_span_text(fx_ctx: RunContext):
    st = fx_ctx.state
    item = with_headers(st)
    st.items[item.id] = spread(item)
    assert not exact(st, st.items[item.id])
    done = await demo.patch_item(fx_ctx, item.id, m.ItemPatch(text=item.statement.rstrip(".") + ", as listed."))
    assert done.verification.status == "pass", [c.detail for c in done.verification.checks if c.result != "pass"]
    assert exact(st, done) and exact(st, st.items[item.id])
    assert {c.role for c in done.citations} == {"evidence", "unit_context", "period_context"}


async def test_injected_errors_are_recited_with_the_exact_span_text(fx_ctx: RunContext):
    st = fx_ctx.state
    with_headers(st)
    for iid, it in list(st.items.items()):
        if it.kind == "data":
            st.items[iid] = spread(it)
    resp = await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=10), repair_delay=0)
    assert "D-028" in resp.item_ids
    fixed = [st.items[i] for i in resp.item_ids]
    assert all(it.verification.status == "fixed" for it in fixed)
    assert all(exact(st, it) for it in fixed), [it.id for it in fixed if not exact(st, it)]
