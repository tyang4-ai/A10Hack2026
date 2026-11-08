"""The frozen SHL plan (data/shl/plan.json), the planted test page and scripts/check_plan.py. All offline."""
from __future__ import annotations

import copy
import importlib.util
import io
import os
import sys
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend.axelrod.contract import RUBRIC, m

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_plan.py"
_spec = importlib.util.spec_from_file_location("check_plan", SCRIPT)
check_plan = importlib.util.module_from_spec(_spec)
sys.modules["check_plan"] = check_plan  # dataclasses look their module up in sys.modules
_spec.loader.exec_module(check_plan)

SCREEN = RUBRIC["depths"]["screen"]


@pytest.fixture
def plan() -> dict:
    """A fresh copy of plan.json, so a test can break it without touching the file."""
    return copy.deepcopy(check_plan.load_plan())


def errors(issues) -> list[str]:
    return [f"{i.where}: {i.message}" for i in issues if i.level == "error"]


def sources_of(plan: dict) -> list[m.Source]:
    return [m.Source.model_validate(s) for s in plan["sources"]]


def source(plan: dict, source_id: str) -> dict:
    return next(s for s in plan["sources"] if s["source_id"] == source_id)


# ---------------------------------------------------------------------------
# The plan as it is
# ---------------------------------------------------------------------------


def test_plan_validates_against_the_contract(plan):
    needs = [m.DataNeed.model_validate(n) for n in plan["needs"]]
    sources = sources_of(plan)
    assert plan["frozen"] is True and plan["depth"] == "screen" and plan["ticker"] == "SHL"
    assert len(needs) >= 6 and len(sources) >= 8


def test_structure_has_no_issues(plan):
    assert check_plan.check_structure(plan) == []


def test_every_screen_criterion_has_a_need_and_a_source(plan):
    for criterion in SCREEN:
        assert any(criterion in n["criteria"] for n in plan["needs"]), criterion
        assert any(criterion in s["criteria"] for s in plan["sources"]), criterion


def test_every_need_has_a_source(plan):
    for need in plan["needs"]:
        assert any(need["need_id"] in s["need_ids"] for s in plan["sources"]), need["need_id"]


def test_s006_is_the_tier2_article_fetched_late(plan):
    s006 = source(plan, "S-006")
    assert (s006["tier"], s006["kind"], s006["vault_folder"]) == ("tier_2", "news", "04-News-Coverage")
    order = [s.source_id for s in check_plan.collect_order(sources_of(plan))]
    priority = [s["source_id"] for s in plan["sources"] if s["tier"] == "priority"]
    assert order.index("S-006") > max(order.index(p) for p in priority)
    assert order.index("S-006") >= len(order) - 3


def test_tiers_and_kinds(plan):
    tiers = {s["source_id"]: s["tier"] for s in plan["sources"]}
    assert [sid for sid, t in tiers.items() if t == "tier_2"] == ["S-006"]
    tier3 = [s for s in plan["sources"] if s["tier"] == "tier_3"]
    assert len(tier3) == 2 and tier3[-1]["url"] == check_plan.TEST_PAGE_URL
    kinds = {s["kind"] for s in plan["sources"] if s["tier"] == "priority"}
    assert kinds <= {"annual_report", "company_ir", "xbrl_facts"}


def test_reliability_follows_the_plan(plan):
    want = {"annual_report": 0.95, "xbrl_facts": 0.95, "company_ir": 0.9, "news": 0.75}
    for s in plan["sources"]:
        if s["kind"] in want:
            assert s["scores"]["reliability"] == want[s["kind"]], s["source_id"]
    assert source(plan, "S-009")["scores"]["reliability"] == 0.35
    assert source(plan, "S-010")["scores"]["reliability"] == 0.1


def test_extract_pages_are_pdf_only_and_within_budget(plan):
    by_id = {s.source_id: s for s in sources_of(plan)}
    pdfs = {sid for sid, s in by_id.items() if check_plan.is_pdf(s)}
    assert set(plan["extract_pages"]) == pdfs
    total = sum(len(check_plan.expand(r)) for r in plan["extract_pages"].values())
    assert 30 <= total <= 45


def test_need_pages_sit_inside_extract_pages(plan):
    for nid, per_source in plan["need_pages"].items():
        for sid, ranges in per_source.items():
            assert set(check_plan.expand(ranges)) <= set(check_plan.expand(plan["extract_pages"][sid])), (nid, sid)


def test_every_extract_page_has_an_anchor(plan):
    for sid, ranges in plan["extract_pages"].items():
        assert set(check_plan.expand(ranges)) == set(check_plan.PAGE_ANCHORS[sid]), sid


def test_fiscal_note_names_the_base_and_the_breaks(plan):
    note = plan["fiscal_note"]
    for phrase in ("FY2026", "Nov 26, 2026", "FY2025 Annual Report", "Segments changed", "shareholder structure"):
        assert phrase in note


# ---------------------------------------------------------------------------
# The planted test page
# ---------------------------------------------------------------------------


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href", ""))
        self._in_title = tag == "title"

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        self._in_title = False


def test_test_page_file_passes_its_checks():
    assert errors(check_plan.check_test_page_file()) == []


def test_test_page_has_one_offsite_link_and_the_title():
    html = check_plan.TEST_PAGE_PATH.read_text(encoding="utf-8")
    parser = _Links()
    parser.feed(html)
    assert parser.title == check_plan.TEST_PAGE_TITLE
    assert parser.hrefs == ["https://example.org/axelrod-test/shl-buyer-survey"]
    assert html.count("Ignore all previous instructions") == 2
    assert "SIMULATED" in html


def test_both_injections_survive_the_html_extraction():
    """The Guardrails scan only sees what fetch/html.py keeps. Trafilatura drops class="comment" blocks and
    aria-hidden elements, so the page avoids both, or the demo's flag would never fire."""
    from backend.axelrod.fetch import html as fetch_html

    raw = check_plan.TEST_PAGE_PATH.read_bytes()
    try:
        pages = fetch_html.html_pages(fetch_html.decode_html(raw, "text/plain; charset=utf-8"), target_chars=3000)
    except (NotImplementedError, ImportError) as exc:
        pytest.skip(f"fetch/html.py not usable yet: {exc}")
    text = "\n".join(pages)
    assert text.count("Ignore all previous instructions") == 2
    assert "SIMULATED" in text
    assert fetch_html.html_links(raw, check_plan.TEST_PAGE_URL) == ["https://example.org/axelrod-test/shl-buyer-survey"]


def test_test_page_checks_catch_a_missing_hidden_injection(tmp_path):
    html = check_plan.TEST_PAGE_PATH.read_text(encoding="utf-8")
    broken = tmp_path / "injection.html"
    broken.write_text(html.replace('font-size:1px;', ''), encoding="utf-8")
    assert any("hidden" in e for e in errors(check_plan.check_test_page_file(broken)))


# ---------------------------------------------------------------------------
# check_plan catches broken plans
# ---------------------------------------------------------------------------


def test_catches_a_rank_tie(plan):
    source(plan, "S-004")["rank"] = 5
    assert any("ranks must be" in e for e in errors(check_plan.check_structure(plan)))


def test_catches_s006_in_the_wrong_tier(plan):
    s006 = source(plan, "S-006")
    s006["tier"], s006["rank"] = "priority", 8
    found = errors(check_plan.check_structure(plan))
    assert any("S-006" in e and "tier_2 news" in e for e in found)
    assert any("kind news may be at most tier_2" in e for e in found)


def test_catches_a_promoted_blog(plan):
    s009 = source(plan, "S-009")
    s009["tier"], s009["rank"] = "tier_2", 2
    assert any("kind other may be at most tier_3" in e for e in errors(check_plan.check_structure(plan)))


def test_catches_an_uncovered_criterion(plan):
    plan["needs"] = [n for n in plan["needs"] if n["need_id"] != "N-07"]
    for s in plan["sources"]:
        s["need_ids"] = [n for n in s["need_ids"] if n != "N-07"]
        s["criteria"] = [c for c in s["criteria"] if c != "research_stage"]
    plan.pop("need_pages")
    found = errors(check_plan.check_structure(plan))
    assert any(e.startswith("research_stage") for e in found)


def test_catches_a_need_without_a_source(plan):
    for s in plan["sources"]:
        if s["source_id"] in ("S-007", "S-008"):
            s["need_ids"] = ["N-01"]
            s["criteria"] = ["rotic_vs_peers"]
    assert any(e.startswith("N-02") for e in errors(check_plan.check_structure(plan)))


def test_catches_bad_ids_and_a_wrong_domain(plan):
    plan["needs"][1]["need_id"] = "N-9"
    source(plan, "S-003")["domain"] = "www.siemens-healthineers.com"
    found = errors(check_plan.check_structure(plan))
    assert any("expected N-02" in e for e in found)
    assert any("does not match the url host" in e for e in found)


def test_catches_contract_violations(plan):
    source(plan, "S-001")["tier"] = "tier_1"
    assert any(e.startswith("sources[0]") for e in errors(check_plan.check_structure(plan)))


def test_catches_page_problems(plan):
    plan["extract_pages"]["S-001"].append([200, 230])
    plan["need_pages"]["N-01"]["S-003"] = [[60, 60]]
    found = errors(check_plan.check_structure(plan))
    assert any("budget is 30 to 45" in e for e in found)
    assert any("pages [60] are not in extract_pages" in e for e in found)
    assert any(e.startswith("PAGE_ANCHORS.S-001") for e in found)


def test_catches_overlapping_ranges_and_non_pdf_pages(plan):
    plan["extract_pages"]["S-002"] = [[6, 8], [8, 9]]
    plan["extract_pages"]["S-006"] = [[1, 1]]
    found = errors(check_plan.check_structure(plan))
    assert any("overlaps" in e for e in found)
    assert any("only PDF sources" in e for e in found)


def test_catches_a_stale_source_state(plan):
    source(plan, "S-002")["approval"] = "approved"
    assert any("planner-proposed, pending" in e for e in errors(check_plan.check_structure(plan)))


# ---------------------------------------------------------------------------
# URL probes, with a fake opener (no network)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, ctype: str = "text/html") -> None:
        self.status = status
        self.headers = {"Content-Type": ctype}

    def read(self, n: int = -1) -> bytes:
        return b"x" * max(n, 0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(table: dict[str, int]):
    """Answers by method: a status of 200/206 returns a response, anything else raises HTTPError."""
    calls: list[tuple[str, dict]] = []

    def open_(req, timeout):
        calls.append((req.get_method(), dict(req.header_items())))
        status = table[req.get_method()]
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "nope", {}, io.BytesIO())
        return _Resp(status)

    open_.calls = calls
    return open_


def test_probe_uses_head_when_it_works():
    opener = _opener({"HEAD": 200, "GET": 200})
    probe = check_plan.probe_url("https://example.com/a.pdf", "UA/1", opener=opener)
    assert probe.ok and probe.method == "HEAD"
    assert [c[0] for c in opener.calls] == ["HEAD"]
    assert opener.calls[0][1]["User-agent"] == "UA/1"


def test_probe_falls_back_to_a_ranged_get():
    opener = _opener({"HEAD": 403, "GET": 206})
    probe = check_plan.probe_url("https://data.sec.gov/x.json", "UA/1", opener=opener)
    assert probe.ok and probe.method == "GET" and probe.status == 206
    assert opener.calls[1][1]["Range"] == "bytes=0-1023"


def test_probe_fails_when_both_fail(monkeypatch):
    monkeypatch.setattr(check_plan.time, "sleep", lambda s: None)
    opener = _opener({"HEAD": 404, "GET": 404})
    probe = check_plan.probe_url("https://example.com/gone", "UA/1", opener=opener)
    assert not probe.ok and probe.status == 404
    assert [c[0] for c in opener.calls] == ["HEAD", "GET", "GET"]


def test_unpushed_test_page_only_warns_but_a_dead_source_fails(plan):
    sources = sources_of(plan)
    dead = {check_plan.TEST_PAGE_URL, source(plan, "S-003")["url"]}

    def probe(url):
        return check_plan.Probe(url not in dead, 404 if url in dead else 200, "HEAD", "")

    issues, lines = check_plan.check_urls(sources, probe)
    assert [(i.level, i.where) for i in issues] == [("error", "S-003"), ("warn", "S-010")]
    assert len(lines) == len(sources)


def test_verify_pages_refuses_a_folder_inside_the_repo(plan):
    def no_fetch(*args):
        raise AssertionError("must not download")

    issues, _ = check_plan.verify_pages(plan, check_plan.REPO / "backend" / ".data" / "pdfs", "UA/1", fetch=no_fetch)
    assert errors(issues) and "inside the repo" in errors(issues)[0]


def test_main_offline_passes(capsys):
    assert check_plan.main(["--offline"]) == 0
    assert "PASS" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("AXELROD_SHL_PDFS"),
                    reason="set AXELROD_SHL_PDFS to a folder outside the repo with S-001.pdf ... S-005.pdf")
def test_pages_hold_their_anchors_in_the_real_pdfs(plan):
    def no_fetch(*args):
        raise AssertionError("put the PDFs in AXELROD_SHL_PDFS first (check_plan.py --pages DIR downloads them)")

    issues, _ = check_plan.verify_pages(plan, Path(os.environ["AXELROD_SHL_PDFS"]), "UA/1", fetch=no_fetch)
    assert errors(issues) == []
