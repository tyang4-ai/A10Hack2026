"""B-baseline: scripts/baseline.py, offline with fake models (FAKE_MODELS=1, simulated).

Fictional data only: the planted statement pages of a fictional company (tests/data/planted/, B-verify's,
read only). The planted errors are the same kinds as B-verify's set (digit swap, dropped minus, wrong year,
wrong scale, flipped "not"), planted by the script's own code into copies of items. The real Store,
RunContext and Gateway run underneath. The live test at the bottom runs the script on one real S-002 page
(AXELROD_LIVE=1 only).
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from backend.axelrod import bedrock
from backend.axelrod.contract import RUBRIC, m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import extract as st
from backend.axelrod.store import Store

REPO = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / "data" / "planted"
PLANTED = [(DATA / f"annual_report_p{i}.txt").read_text(encoding="utf-8") for i in (1, 2, 3)]


def _load_script():
    """scripts/baseline.py as a module. Dataclasses look their module up in sys.modules, so register it first."""
    name = "baseline_script"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "backend" / "scripts" / "baseline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bl = _load_script()

NEEDS = [
    m.DataNeed(need_id="N-01", label="Returns on tangible capital", dimension="financials",
               question="EBIT, taxes, total assets, cash, goodwill and current liabilities by year?",
               data_types=["financial_filing"], criteria=["rotic_level", "rotic_vs_peers"], rationale="test"),
    m.DataNeed(need_id="N-05", label="Debt", dimension="financials", question="Financial debt and net debt?",
               data_types=["financial_filing"], criteria=["leverage"], rationale="test"),
]


@pytest.fixture(autouse=True)
def _restore_fakes():
    saved = dict(bedrock._FAKES)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


def source(sid: str = "S-901", *, title: str = "Meridian annual report FY2025 (simulated)",
           kind: str = "annual_report") -> m.Source:
    return m.Source(source_id=sid, title=title, url=f"https://example.com/{sid}", domain="example.com", kind=kind,
                    data_types=["financial_filing"], access=m.SourceAccess(), tier="priority", rank=1,
                    scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data",
                    need_ids=["N-01", "N-05"], criteria=["rotic_level", "leverage"], vault_folder="01-Annual-Reports",
                    proposed_by="planner", approval="auto_approved", status="fetched")


def make_ctx(settings) -> RunContext:
    """A fictional company's stored annual report pages, as collect would leave them."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Meridian Diagnostics AG", ticker="MDX",
                                                notes=f"{bl.NOTES_MARK}: test run (simulated)"))
    state.needs = list(NEEDS)
    state.extract_pages = {"S-901": [[1, 3]]}
    src = source()
    state.sources.append(src)
    state.documents.append(m.Document(
        doc_id="DOC-901", source_id="S-901", title="S-901 (simulated)", url="https://example.com/S-901",
        content_type="pdf", paginated=True, pages=len(PLANTED), sha256="0" * 64, bytes=sum(map(len, PLANTED)),
        fetched_at=now_iso(), vault_paths=m.DocVaultPaths(raw="a.pdf", text="a.md", meta="a.meta.json"),
        scan=m.ScanResult(scanner="none", verdict="skipped")))
    state.pages["DOC-901"] = list(PLANTED)
    src.doc_ids.append("DOC-901")
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    return ctx


def claim(quote: str, value_text: str = "", value: float | None = None, *, statement: str, period: str = "FY2025",
          unit: str = "EUR", scale: str = "million", document: str = "AR2025", page: int = 1) -> dict:
    return {"statement": statement, "value": value, "value_text": value_text, "unit": unit, "scale": scale,
            "period": period, "document": document, "page": page, "quote": quote}


REVENUE = claim("Revenue 4 18,742 17,906", "18,742", 18742, statement="Revenue was €18,742 million in fiscal year 2025.")
COST = claim("Cost of sales (10,233) (9,871)", "(10,233)", -10233,
             statement="Cost of sales was €10,233 million in fiscal year 2025.")
FCF = claim("Free cash flow was €2,160 million in fiscal year 2025 (2024: €1,953 million).", "2,160", 2160,
            statement="Free cash flow was €2,160 million in fiscal year 2025.", page=3)
DEBT = claim("Long-term financial debt 11,592 12,047", "11,592", 11592,
             statement="Long-term financial debt was €11,592 million at September 30, 2025.", period="2025-09-30",
             page=2)
MISSED = claim("The Diagnostics segment did not reach its margin target in fiscal year 2025.", period="",
               statement="The Diagnostics segment did not reach its margin target in fiscal year 2025.", page=3,
               unit="", scale="one")
CLEAN = [REVENUE, COST, FCF, DEBT, MISSED]


def checked(ctx: RunContext, claims: list[dict]):
    report = bl.BaselineReport.model_validate({"claims": claims})
    refs = bl.doc_refs(ctx.state, ["S-901"])
    return bl.check_baseline(report, refs, st.readable_pages(ctx.state), ctx.state.sources_by_id(), "drafter-01")


def pages_and_sources(ctx: RunContext):
    return st.readable_pages(ctx.state), ctx.state.sources_by_id()


# ---------------------------------------------------------------------------
# Documents, prompt and output model
# ---------------------------------------------------------------------------


def test_doc_tags_and_prompt(settings):
    ctx = make_ctx(settings)
    refs = bl.doc_refs(ctx.state, ["S-901"])
    assert [(r.tag, r.doc_id, r.source_id, r.pages) for r in refs] == [("AR2025", "DOC-901", "S-901", [1, 2, 3])]
    prompt = bl.build_prompt(ctx.state, refs, max_claims=40)
    assert "Write a BOFFIN screen of Meridian Diagnostics AG (MDX)" in prompt
    assert "quote each number exactly as it is printed" in prompt
    assert "=== AR2025 page 2 ===" in prompt and "=== end of AR2025 page 2 ===" in prompt
    assert "Cash and cash equivalents 1,327 1,148" in prompt and "up to 40 claims" in prompt
    for key in RUBRIC["depths"]["screen"]:
        assert f"- {key}: " in prompt
    assert "framework definitions below" not in prompt
    used: set[str] = set()
    q4 = source("S-902", title="Meridian Q4 FY2026 earnings release (simulated)", kind="company_ir")
    assert bl._tag_for(q4, used) == "Q4FY2026"
    assert bl._tag_for(source(), {"AR2025"}) == "AR2025-2"


def test_report_model_tolerates_model_quirks():
    r = bl.BaselineReport.model_validate({"claims": [{
        "statement": "Net income was €1,894 million.", "value": "1,894", "value_text": None, "scale": "Millions",
        "document": "AR2025", "page": "p. 1", "quote": "Net income 1,894 1,667", "period": None}],
        "criteria": [{"criterion": "leverage", "score": "+1"}, {"criterion": "liquidity", "score": "N/A"},
                     {"criterion": "rotic_level", "score": "−1"}]})
    c = r.claims[0]
    assert (c.value, c.value_text, c.scale, c.page, c.period) == (1894.0, "", "million", 1, "")
    assert [x.score for x in r.criteria] == [1, None, -1]
    assert bl.TOOL == "write_boffin_screen"


# ---------------------------------------------------------------------------
# Baseline claims through the same code checks
# ---------------------------------------------------------------------------


def test_claims_are_checked_by_code(settings):
    ctx = make_ctx(settings)
    claims = [
        REVENUE,                                                                   # 1 clean table row
        dict(REVENUE, page=2),                                                     # 2 right quote, wrong page
        claim("Revenue 4 18,724 17,906", "18,724", 18724, statement="Revenue was €18,724 million."),  # 3 misquote
        dict(REVENUE, value=18724.0),                                              # 4 value and text disagree
        dict(REVENUE, period="FY2024"),                                            # 5 wrong year column
        dict(REVENUE, document="AR1999"),                                          # 6 document never given
        FCF,                                                                       # 7 clean prose
        dict(REVENUE, scale="thousand"),                                           # 8 wrong scale
    ]
    got = checked(ctx, claims)
    assert [c.label for c in got] == [f"claim {i}" for i in range(1, 9)]
    assert [c.status for c in got] == ["pass", "pass", "fail", "fail", "fail", "fail", "pass", "fail"]
    roles = [cit.role for cit in got[0].item.citations]
    assert roles == ["evidence", "unit_context", "period_context"]  # headers added by code, from the page
    assert got[0].item.id == "BL-001" and got[0].item.citations[0].locator.page == 1
    assert got[1].page_off and got[1].found_page == 1 and got[1].cited_page == 2
    assert got[2].failed("quote_exact")
    assert got[3].failed("value_in_quote") and not got[3].failed("quote_exact")
    assert got[4].failed("period_match")
    assert got[5].item is None and got[5].failed("quote_exact") and "AR1999" in got[5].results[0].detail
    assert got[7].failed("unit_context")
    # claim 3: a quote not found gets no header lines, so its year is not named. claim 5: the 2024 column.
    assert bl.period_reasons(got) == {"not named": 1, "other year": 1, "other": 0}
    no_year = checked(ctx, [dict(FCF, period="FY2026")])
    assert bl.period_reasons(no_year) == {"not named": 1, "other year": 0, "other": 0}
    stats = bl.side_stats(got)
    assert (stats["items"], stats["pass"], stats["fail_any"], stats["fail_quote_exact"], stats["page_off"]) == (8, 3, 5, 2, 1)


# ---------------------------------------------------------------------------
# Planted errors (fictional items only)
# ---------------------------------------------------------------------------


def test_plant_functions_change_copies_only(settings):
    ctx = make_ctx(settings)
    rev, cost, fcf, debt, missed = (c.item for c in checked(ctx, CLEAN))
    planted, change = bl.plant_digit_swap(rev)
    assert planted.value.as_reported == "18,724" and planted.value.number == 18724
    assert planted.citations[0].quote == "Revenue 4 18,724 17,906" and "18,724" in planted.statement
    assert rev.citations[0].quote == "Revenue 4 18,742 17,906" and rev.value.number == 18742  # original untouched
    assert ctx.state.pages["DOC-901"] == PLANTED                                             # stored pages too
    assert "18,742" in change and "18,724" in change
    p, _ = bl.plant_dropped_minus(cost)
    assert (p.value.as_reported, p.value.number) == ("10,233", 10233)
    assert bl.plant_dropped_minus(rev) is None
    p, change = bl.plant_wrong_year(fcf)
    assert p.value.period == "FY2024" and "fiscal year 2024" in p.statement and change == "FY2025 to FY2024"
    assert bl.plant_wrong_year(debt)[0].value.period == "2024-09-30"
    p, _ = bl.plant_wrong_scale(rev)
    assert p.value.scale == "thousand" and "thousand" in p.statement
    pct = rev.model_copy(update={"value": rev.value.model_copy(update={"unit": "%", "scale": "one"})})
    assert bl.plant_wrong_scale(pct) is None
    p, _ = bl.plant_flipped_not(missed)
    assert p.citations[0].quote == "The Diagnostics segment did reach its margin target in fiscal year 2025."
    assert bl.plant_flipped_not(rev) is None  # a table row has no verb to negate
    assert bl.flip_not("The outlook is unchanged.") == "The outlook is not unchanged."
    assert bl.flip_not("Revenue 4 18,742 17,906") is None


def test_spread_is_even_and_deterministic():
    xs = list(range(10))
    assert bl.spread(xs, 3) == [0, 5, 9]
    assert bl.spread(xs, 20) == xs and bl.spread(xs, 1) == [0] and bl.spread(xs, 0) == [] and bl.spread([], 3) == []


def test_every_planted_kind_is_caught_and_bases_are_clean(settings):
    ctx = make_ctx(settings)
    bad = claim("Revenue 4 18,724 17,906", "18,724", 18724, statement="Revenue was €18,724 million.")
    got = checked(ctx, CLEAN + [bad])
    plants = bl.plant_side("baseline", got, *pages_and_sources(ctx), per_kind=5)
    by_kind = {kind: [p for p in plants if p.kind == kind] for kind, _, _ in bl.KINDS}
    assert all(by_kind[k] for k in by_kind), {k: len(v) for k, v in by_kind.items()}
    assert all(p.caught and p.target_hit for p in plants), [(p.kind, p.base, p.detail) for p in plants if not p.caught]
    assert "claim 6" not in {p.base for p in plants}  # a failing claim is never a base
    assert len(by_kind["flipped not"]) == 2  # the free cash flow sentence ("was") and the margin sentence ("not")
    assert len(by_kind["dropped minus"]) == 1  # cost of sales is the only negative
    assert bl.caught(plants) == (len(plants), len(plants))


def test_a_missed_plant_counts_as_missed(settings):
    """The known checker limit: a spaced dash reads as a possible range dash, so a dropped minus is accepted."""
    ctx = make_ctx(settings)
    ctx.state.pages["DOC-901"] = PLANTED[:2] + [PLANTED[2] + "\nThe currency effect was – 412 million in fiscal year 2025.\n"]
    spaced = claim("The currency effect was – 412 million in fiscal year 2025.", "– 412", -412, page=3,
                   statement="The currency effect was €-412 million in fiscal year 2025.", unit="EUR")
    got = checked(ctx, [spaced])
    assert got[0].status == "pass", [r.detail for r in got[0].results if r.result != "pass"]
    plants = [p for p in bl.plant_side("baseline", got, *pages_and_sources(ctx)) if p.kind == "dropped minus"]
    assert len(plants) == 1 and not plants[0].caught and plants[0].detail == "all four checks pass"


# ---------------------------------------------------------------------------
# Hand-check list
# ---------------------------------------------------------------------------


def test_hand_check_picks_key_numbers_and_compares_the_baseline(settings):
    ctx = make_ctx(settings)
    harness = [dataclasses.replace(c, side="harness", label=c.item.id, cited_page=None)
               for c in checked(ctx, [REVENUE, DEBT, FCF, COST])]
    baseline = checked(ctx, [dict(REVENUE, value=17906.0, value_text="17,906"), FCF])
    hc = bl.hand_check(harness, baseline, {"S-901": 0}, limit=3)
    assert len(hc) == 3 and [h.n for h in hc] == [1, 2, 3]
    by_metric = {h.metric: h for h in hc}
    assert by_metric["Revenue"].baseline.startswith("differs: 17,906 million (claim 1")
    assert by_metric["Free cash flow"].baseline == "same (claim 2)"
    assert by_metric["Revenue"].page == 1 and by_metric["Revenue"].quote == "Revenue 4 18,742 17,906"
    assert all(h.source_id == "S-901" for h in hc)
    full = bl.hand_check(harness, [], {"S-901": 0})
    assert len(full) == 4 and all(h.baseline == "not given" for h in full)
    # coverage both ways: the wrong-column revenue claim fails, so only free cash flow is shared
    assert bl.coverage(baseline, harness)[:2] == (1, 1)
    got, total, missing = bl.coverage(harness, baseline)
    assert (got, total) == (1, 4) and [c.item.statement.split()[0] for c in missing] == ["Revenue", "Long-term", "Cost"]
    # the same value and period on another page counts too: the revenue sentence on page 3
    prose = claim("Revenue rose to €18,742 million in fiscal year 2025, compared with €17,906 million in fiscal year "
                  "2024.", "18,742", 18742, statement="Revenue was €18,742 million in fiscal year 2025.", page=3)
    other_page = checked(ctx, [prose])
    assert other_page[0].status == "pass"
    assert bl.baseline_view(harness[0].item, other_page) == "same (claim 1, from S-901 p. 3)"
    assert bl.coverage(harness, other_page)[:2] == (1, 4)


# ---------------------------------------------------------------------------
# End to end offline: fake extractor and fake baseline (both simulated)
# ---------------------------------------------------------------------------


@pytest.fixture
async def kept_run(settings):
    ctx = make_ctx(settings)
    raw = await bl.compare(ctx, ["S-901"], max_tokens=4000)
    return ctx, raw


async def test_compare_end_to_end_offline(settings, kept_run):
    ctx, raw = kept_run
    assert raw["simulated"] is True and raw["baseline"]["error"] is None and raw["baseline"]["report"]["claims"]
    s = bl.analyse(ctx.state, raw, per_kind=2)
    assert s.simulated and s.n_pages == 3 and s.harness and s.baseline
    assert s.harness_usage.model_calls == 3 and s.baseline_usage.model_calls == 1
    assert s.model_large == settings.model_large and s.model_small == settings.model_small
    drafter = ctx.state.agents[raw["baseline"]["agent_id"]]
    assert drafter.role == "drafter" and drafter.status == "done"
    calls = [e for e in ctx.state.events if e.type == "gateway.request" and e.data.action == "model.invoke"]
    assert any(e.agent_id == drafter.agent_id and e.data.decision == "allowed" for e in calls)
    hx, hy = bl.caught(s.plants["harness"])
    bx, by = bl.caught(s.plants["baseline"])
    assert hy > 0 and by > 0 and (hx, bx) == (hy, by)
    assert s.hand and all(h.baseline.startswith("same") for h in s.hand)  # the fake reads the pages the same way
    md = bl.render_markdown(s)
    assert md.startswith("# Baseline evaluation")
    assert f"Experiment, run on {s.date}, not part of the demo run." in md
    assert "Simulated: model answers came from offline fakes" in md
    assert bl.headline(s) in md and "## Key numbers to hand-check" in md and "## Planted errors" in md
    assert "## Plain Claude numbers the harness has no item for\n\nNone." in md
    assert "—" not in md and ";" not in md
    doc = json.loads(bl.render_json(s))
    assert doc["label"].startswith("Experiment, run on") and doc["simulated"] is True
    assert doc["baseline"]["usage"]["model_calls"] == 1 and len(doc["plants"]) == hy + by


async def test_reuse_recomputes_without_models(settings, kept_run, tmp_path):
    ctx, raw = kept_run
    first = bl.analyse(ctx.state, raw, per_kind=2)
    bedrock._FAKES.clear()  # any model call would fail now
    state, raw2 = bl.load_kept(settings.data_dir)
    again = bl.analyse(state, raw2, per_kind=2)
    assert bl.headline(again) == bl.headline(first)
    assert bl.side_stats(again.baseline) == bl.side_stats(first.baseline)
    out = tmp_path / "out" / "results.md"
    code = bl.main(["--reuse", str(settings.data_dir), "--per-kind", "2", "--out", str(out)])
    assert code == 0
    md = out.read_text(encoding="utf-8")
    assert md.startswith("# Baseline evaluation") and "not part of the demo run" in md
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["run_id"] == ctx.run_id


def test_reuse_with_nothing_kept_is_an_error(settings, tmp_path, capsys):
    assert bl.main(["--reuse", str(tmp_path / "empty")]) == 2
    assert "no kept baseline run" in capsys.readouterr().err


async def test_failed_baseline_is_reported_not_hidden(settings):
    ctx = make_ctx(settings)
    bedrock.register_fake(bl.TOOL, lambda system, user, schema: {"claims": "not a list"})
    raw = await bl.compare(ctx, ["S-901"])
    assert raw["baseline"]["report"] is None and "failed validation twice" in raw["baseline"]["error"]
    assert ctx.state.agents[raw["baseline"]["agent_id"]].status == "failed"
    s = bl.analyse(ctx.state, raw)
    assert s.report is None and s.baseline == [] and s.plants["baseline"] == []
    assert "failed, so there is nothing to compare" in bl.headline(s)
    assert "The plain Claude call did not return a report" in bl.render_markdown(s)


def test_cut_plan_merges_sources_and_caps_pages():
    plan = json.loads((REPO / "backend" / "data" / "shl" / "plan.json").read_text(encoding="utf-8"))
    needs, sources, extract, need_pages = bl.cut_plan(plan, ["S-001", "S-003"], max_pages=2)
    assert [s.source_id for s in sources] == ["S-001", "S-003"]
    assert all(s.approval == "auto_approved" and s.status == "queued" for s in sources)
    assert set(extract) == {"S-001", "S-003"} and all(len(v) == 2 for v in extract.values())
    assert {n.need_id for n in needs} == set(sources[0].need_ids) | set(sources[1].need_ids)
    assert all(set(per) <= {"S-001", "S-003"} for per in need_pages.values())
    _, _, full, _ = bl.cut_plan(plan, list(bl.SOURCE_IDS))
    assert sum(hi - lo + 1 for v in full.values() for lo, hi in v) == 36


# ---------------------------------------------------------------------------
# Live: one real S-002 page each way (AXELROD_LIVE=1, a few cents)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_baseline_live_one_page(tmp_path, capsys):
    out = tmp_path / "live.md"
    code = bl.main(["--sources", "S-002", "--max-pages", "1", "--per-kind", "2", "--budget", "0.4",
                    "--max-tokens", "6000", "--out", str(out)])
    printed = capsys.readouterr().out
    with capsys.disabled():  # show the headline and the spend line with -s
        print("\n" + "\n".join(printed.strip().splitlines()[-2:]))
    assert code == 0, printed
    md = out.read_text(encoding="utf-8")
    assert "not part of the demo run" in md and "Simulated" not in md
    assert "Spent $" in printed
