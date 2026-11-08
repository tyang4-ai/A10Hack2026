"""Check the frozen SHL plan: contract validation, ids, tiers, ranks, criterion coverage, pages and every URL answering.

Usage (from the repo root):
  PY backend/scripts/check_plan.py               structure, the test page file, then every source URL
  PY backend/scripts/check_plan.py --offline     everything except the network
  PY backend/scripts/check_plan.py --pages DIR   also download the PDFs into DIR (outside the repo, never
                                                 committed) and check that every extract page holds its anchor
  PY backend/scripts/check_plan.py --archive DIR check pages against Gordon's local summer 2026 archive instead
                                                 of a download (read only, nothing is copied). Default: the
                                                 AXELROD_SHL_ARCHIVE environment variable

The URL checks are a dev preflight, not agent traffic: no agent runs and nothing is stored, so they use stdlib
urllib (HEAD, then a 1 KB ranged GET) instead of the gateway, which needs a run and has no HEAD. The planted
test page only warns until the orchestrator pushes the backend branch. Exit code 1 means at least one error.

Archive sources (S-011 and up, rationale "collected before the event (summer 2026 archive)") were collected
before the event and are served from the S3 document cache. A live URL of theirs that no longer answers, or an
s3:// URL, only warns: the run gets the bytes from the cache.

With TEST_PAGE_URL set (env or backend/.env), runs fetch the planted page from Axelrod's own API instead of
raw GitHub, so this script checks that URL instead: its shape offline, and online that it answers with the
page and the X-Axelrod-Simulated header. A failure there is an error, since the demo depends on it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))  # repo root, so `backend.axelrod` imports

from pydantic import ValidationError  # noqa: E402

from backend.axelrod.contract import RUBRIC, m  # noqa: E402

PLAN_PATH = REPO / "backend" / "data" / "shl" / "plan.json"
TEST_PAGE_PATH = REPO / "backend" / "data" / "test_pages" / "injection.html"
TEST_PAGE_URL = ("https://raw.githubusercontent.com/tyang4-ai/A10Hack2026/backend/"
                 "backend/data/test_pages/injection.html")
TEST_PAGE_TITLE = "Axelrod test page: planted prompt injection (simulated)"
SERVED_PATH = "/test-pages/injection.html"     # where api.py serves the page, under TEST_PAGE_URL's host
SIMULATED_HEADER = "X-Axelrod-Simulated"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
REVOKE_TARGET = "S-006"           # the demo revokes this tier_2 article mid-collect
PAGE_BUDGET = (30, 45)            # extract pages of the demo sources S-001 to S-010 (live-model cost of a run)
TIER_ORDER = {"priority": 0, "tier_2": 1, "tier_3": 2}

# The ten demo sources stay exactly as they are. The demo script depends on this fetch order among them, with
# S-006 (revoked), S-009 (blocked link-follow) and S-010 (flagged test page) fetched last.
DEMO_SOURCES = tuple(f"S-{i:03d}" for i in range(1, 11))
DEMO_FETCH_ORDER = ("S-001", "S-002", "S-007", "S-008", "S-003", "S-004", "S-005", "S-006", "S-009", "S-010")
DEMO_LAST = ("S-006", "S-009", "S-010")

# Archive sources: primary documents from Gordon's summer 2026 archive, served from the S3 document cache.
ARCHIVE_NOTE = "collected before the event (summer 2026 archive)"
FIRST_ARCHIVE_ID = 11
ARCHIVE_PAGE_MAX = 60             # extract pages from archive sources: about 60 at most, for cost per run
ARCHIVE_RELIABILITY = {"annual_report": 0.95, "sec_filing": 0.95, "xbrl_facts": 0.95, "company_ir": 0.9}
ARCHIVE_PREFIX = "archive/shl/"   # S3 key prefix the archive importer uses under VAULT_BUCKET
ARCHIVE_ENV = "AXELROD_SHL_ARCHIVE"

# Relative path in the archive of each plan PDF that has a local copy. Archive sources must have one (it is
# also the s3:// fallback key). S-001 and S-003 to S-005 are live sources whose bytes are in the archive too,
# so --archive can check their pages without a download. S-002 (Nov 5, 2026) postdates the archive.
ARCHIVE_FILES: dict[str, str] = {
    "S-001": "01-Annual-Reports/FY2025/FY2025_shl_annual-report.pdf",
    "S-003": "01-Annual-Reports/FY2023/FY2023_shl_annual-report.pdf",
    "S-004": "01-Annual-Reports/FY2021/FY2021_shl_annual-report.pdf",
    "S-005": "01-Annual-Reports/FY2019/FY2019_shl_annual-report.pdf",
    "S-011": "01-Annual-Reports/FY2024/FY2024_shl_annual-report.pdf",
    "S-012": "01-Annual-Reports/FY2022/FY2022_shl_annual-report.pdf",
    "S-013": "01-Annual-Reports/FY2020/FY2020_shl_annual-report.pdf",
    "S-014": "01-Annual-Reports/FY2018/FY2018_shl_annual-report.pdf",
    "S-015": "08-Competitors/Philips/FY2025_philips_annual-report.pdf",
    "S-016": "08-Competitors/Philips/FY2023_philips_annual-report.pdf",
}
REQUIRED_KEYS = {"frozen", "company", "ticker", "aliases", "depth", "as_of", "fiscal_note", "needs", "sources",
                 "extract_pages"}
OPTIONAL_KEYS = {"need_pages"}

# The highest tier a planner may give each kind (CONTRACT.md section 5: lower is allowed, never higher).
KIND_TOP_TIER = {
    "sec_filing": "priority", "xbrl_facts": "priority", "annual_report": "priority", "company_ir": "priority",
    "gov": "priority", "news": "tier_2", "market_report": "tier_2", "company_site": "tier_2",
    "product_page": "tier_2", "data_api": "tier_2", "forum": "tier_3", "code_repo": "tier_3", "other": "tier_3",
}

# One phrase per extract page, checked against the real PDF by --pages. Page keys are 1-based indices in the
# file. A page in extract_pages without an anchor here (or the other way round) is an error.
PAGE_ANCHORS: dict[str, dict[int, str]] = {
    "S-001": {
        7: "we reported R&D expenses of €1,958 million",
        8: "We operate in growth markets",
        13: "Our addressable markets generally exhibited slight growth worldwide",
        15: "A.3.2.1 Revenue by segment and region",
        22: "A.3.3.2 Cash flows",
        25: "A.4.1 Expected market development",
        138: "B.1 Consolidated statements of income",
        140: "B.3 Consolidated statements of financial position",
        141: "B.4 Consolidated statements of cash flows",
        152: "combined German tax rate of 29.5%",
        156: "Note 12 Other intangible assets and property, plant and equipment",
        159: "Note 15 Financial debt",
        160: "multicurrency revolving credit facility of up to €2.5 billion",
        162: "Net debt (including pensions)/EBITDA",
        169: "propose to distribute a dividend of €1.00 per share",
        177: "Maturity analysis as of Sept 30, 2025",
        182: "Note 29 Segment information",
        184: "Amortization, depreciation and other effects from IFRS 3 purchase price allocation adjustments",
        189: "Note 34 Subsequent events",
    },
    "S-002": {
        1: "Siemens Healthineers ends 2026 fiscal year with strong fourth quarter",
        6: "Business Development Fiscal Year 2026",
        7: "The income tax rate was 22.4%",
        8: "For fiscal year 2027, we expect comparable revenue growth between 4.0% and 5.0%",
        13: "Consolidated statements of income",
        15: "Consolidated statements of financial position",
        17: "Consolidated statements of cash flows Fiscal year",
        19: "EBITDA reconciliation",
    },
    "S-003": {
        53: "B.1 Consolidated statements of income",
        55: "B.3 Consolidated statements of financial position",
        99: "Amortization, depreciation and other effects from IFRS 3 purchase price allocation adjustments",
    },
    "S-004": {
        44: "B.1 Consolidated statements of income",
        46: "B.3 Consolidated statements of financial position",
        85: "Amortization, depreciation and other effects from IFRS 3 purchase price allocation",
    },
    "S-005": {
        62: "B.1 Consolidated statements of income",
        64: "B.3 Consolidated statements of financial position",
        95: "Amortization of intangible assets acquired in business combinations",
    },
    # Archive sources, checked against the local archive copies (--archive).
    "S-011": {
        52: "B.4 Consolidated statements of cash flows",
        64: "Effective tax rate 22.4% 20.9%",
        73: "multicurrency revolving credit facility of up to €2.5 billion",
        75: "Net debt (including pensions)/EBITDA 3.3 3.7",
        95: "Note 29 Segment information",
    },
    "S-012": {
        53: "B.4 Consolidated statements of cash flows",
        64: "Effective tax rate 26.6% 27.4%",
        72: "Note 15 Financial debt",
        74: "Net debt (including pensions)/EBITDA 3.0 3.5",
        93: "Note 29 Segment information",
    },
    "S-013": {
        59: "B.4 Consolidated statements of cash flows",
        69: "Effective tax rate 27.2 % 27.7 %",
        74: "Note 16 Financial debt",
        76: "Net debt (including pensions) / EBITDA 0.9 1.4",
        92: "Note 29 Segment information",
    },
    "S-014": {
        60: "B.1 Consolidated statements of income",
        62: "B.3 Consolidated statements of financial position",
        63: "B.4 Consolidated statements of cash flows",
        73: "Effective tax rate 28.6 % 29.4 %",
        74: "combined statutory German tax rate of 29.6 %",
        80: "Note 16 Debt",
        81: "Net debt ( including pensions ) / EBITDA 1.5 3.0",
        96: "Note 29 Segment information",
        97: "Amortization of intangible assets acquired in business combinations",
    },
    "S-015": {
        92: "Consolidated statements of income Philips Group",
        93: "Consolidated balance sheets Philips Group",
        99: "Reconciliation from net income to Adjusted EBITA in millions of EUR 2025",
        100: "Reconciliation from net income to Adjusted EBITA in millions of EUR 2023",
        111: "Effective income tax rate 23.6 389.5 17.0",
    },
    "S-016": {
        143: "Total liabilities and group equity 30,688 29,406",
    },
}


@dataclass
class Issue:
    level: str      # "error" or "warn"
    where: str
    message: str


@dataclass
class Probe:
    ok: bool
    status: int | None
    method: str
    detail: str


def err(where: str, message: str) -> Issue:
    return Issue("error", where, message)


def warn(where: str, message: str) -> Issue:
    return Issue("warn", where, message)


# ---------------------------------------------------------------------------
# Loading and small helpers
# ---------------------------------------------------------------------------


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def is_pdf(source: m.Source) -> bool:
    """PDF sources get extract_pages. Everything else is read whole (HTML pseudo-pages) or by code (JSON)."""
    return urlsplit(source.url).path.lower().endswith(".pdf")


def expand(ranges: list[list[int]]) -> list[int]:
    return [p for a, b in ranges for p in range(a, b + 1)]


def collect_order(sources: list[m.Source]) -> list[m.Source]:
    """Same order as stages/collect.py: priority, then tier_2, then tier_3, each by rank."""
    return sorted(sources, key=lambda s: (TIER_ORDER.get(s.tier, 9), s.rank, s.source_id))


def is_archive(source: m.Source | dict[str, Any]) -> bool:
    """An archive source says so in its rationale: collected before the event, served from the document cache."""
    rationale = source["rationale"] if isinstance(source, dict) else source.rationale
    return ARCHIVE_NOTE in rationale


def archive_ids(plan: dict[str, Any]) -> set[str]:
    return {s["source_id"] for s in plan.get("sources", []) if is_archive(s)}


def page_totals(plan: dict[str, Any]) -> tuple[int, int]:
    """(extract pages of the other sources, extract pages of archive sources)."""
    archived = archive_ids(plan)
    extract = plan.get("extract_pages") or {}
    demo = sum(len(expand(r)) for sid, r in extract.items() if sid not in archived)
    return demo, sum(len(expand(r)) for sid, r in extract.items() if sid in archived)


def archive_s3_url(source_id: str, bucket: str | None) -> str | None:
    """Where the archive importer stores this file: s3://<VAULT_BUCKET>/archive/shl/<relative path>.
    The bucket comes from settings at run time, never from this file."""
    rel = ARCHIVE_FILES.get(source_id)
    if rel is None:
        return None
    return f"s3://{bucket or '<VAULT_BUCKET unset>'}/{ARCHIVE_PREFIX}{rel}"


def norm_text(s: str) -> str:
    """NFKC, control characters and soft hyphens dropped, whitespace runs collapsed (older SHL PDFs carry all three)."""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub("[\u0000-\u0008\u000b-\u001f­]", "", s)
    return " ".join(s.split())


def in_scope(depth: str) -> list[str]:
    scope = RUBRIC["depths"][depth]
    return list(RUBRIC["criteria"]) if scope == "all" else list(scope)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def check_structure(plan: dict[str, Any]) -> list[Issue]:
    """Every offline check of plan.json. Model validation errors stop the checks that need the models."""
    issues = check_header(plan)
    needs, sources, model_issues = validate_models(plan)
    issues += model_issues
    if model_issues:
        return issues
    issues += check_ids(needs, sources)
    issues += check_source_fields(needs, sources)
    issues += check_tiers_and_ranks(sources)
    issues += check_demo_order(sources)
    issues += check_archive_sources(sources)
    issues += check_coverage(plan.get("depth", "screen"), needs, sources)
    issues += check_pages(plan, needs, sources)
    issues += check_test_page_source(sources)
    return issues


def check_header(plan: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    missing = REQUIRED_KEYS - plan.keys()
    if missing:
        issues.append(err("plan", f"missing keys: {sorted(missing)}"))
    extra = plan.keys() - REQUIRED_KEYS - OPTIONAL_KEYS
    if extra:
        issues.append(warn("plan", f"unknown keys: {sorted(extra)}"))
    if plan.get("frozen") is not True:
        issues.append(err("plan.frozen", "must be true"))
    if plan.get("depth") not in RUBRIC["depths"]:
        issues.append(err("plan.depth", f"{plan.get('depth')!r} is not a rubric depth"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(plan.get("as_of", ""))):
        issues.append(err("plan.as_of", "must be YYYY-MM-DD"))
    note = plan.get("fiscal_note")
    if not isinstance(note, str) or not note.strip() or "\n" in note:
        issues.append(err("plan.fiscal_note", "must be one non-empty line"))
    issues += check_aliases(plan)
    return issues


def check_aliases(plan: dict[str, Any]) -> list[Issue]:
    aliases = plan.get("aliases")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) for a in aliases):
        return [err("plan.aliases", "must be a non-empty list of strings")]
    issues = [err("plan.aliases", f"{a!r} must be lowercase") for a in aliases if a != a.lower()]
    company = str(plan.get("company", "")).lower()
    if not any(a in company for a in aliases):
        issues.append(err("plan.aliases", "no alias matches the company name"))
    return issues


def validate_models(plan: dict[str, Any]) -> tuple[list[m.DataNeed], list[m.Source], list[Issue]]:
    """Validate with the contract models themselves, so a typo fails here and not mid-run."""
    needs: list[m.DataNeed] = []
    sources: list[m.Source] = []
    issues: list[Issue] = []
    for i, raw in enumerate(plan.get("needs") or []):
        try:
            needs.append(m.DataNeed.model_validate(raw))
        except ValidationError as exc:
            issues.append(err(f"needs[{i}]", str(exc)))
    for i, raw in enumerate(plan.get("sources") or []):
        try:
            sources.append(m.Source.model_validate(raw))
        except ValidationError as exc:
            issues.append(err(f"sources[{i}]", str(exc)))
    if not needs or not sources:
        issues.append(err("plan", "needs and sources must both be non-empty"))
    return needs, sources, issues


def check_ids(needs: list[m.DataNeed], sources: list[m.Source]) -> list[Issue]:
    """Ids are code-style and contiguous: N-01, N-02 ... and S-001, S-002 ... in file order."""
    issues: list[Issue] = []
    for i, need in enumerate(needs, start=1):
        if need.need_id != f"N-{i:02d}":
            issues.append(err(need.need_id, f"expected N-{i:02d} at position {i}"))
    for i, source in enumerate(sources, start=1):
        if source.source_id != f"S-{i:03d}":
            issues.append(err(source.source_id, f"expected S-{i:03d} at position {i}"))
    return issues


def check_source_fields(needs: list[m.DataNeed], sources: list[m.Source]) -> list[Issue]:
    need_criteria = {n.need_id: set(n.criteria) for n in needs}
    issues: list[Issue] = []
    for s in sources:
        issues += check_one_source(s, need_criteria)
    return issues


def check_one_source(s: m.Source, need_criteria: dict[str, set[str]]) -> list[Issue]:
    where = s.source_id
    parts = urlsplit(s.url)
    issues: list[Issue] = []
    if parts.scheme == "s3" and not is_archive(s):
        issues.append(err(where, "only archive sources may have an s3:// url (cache-only archive copies)"))
    elif parts.scheme not in ("https", "s3"):
        issues.append(err(where, "url must be https"))
    if parts.hostname != s.domain:
        issues.append(err(where, f"domain {s.domain!r} does not match the url host {parts.hostname!r}"))
    fresh = (s.proposed_by == "planner" and s.approval == "pending" and s.status == "proposed"
             and not s.doc_ids and s.stats == m.SourceStats() and s.error is None)
    if not fresh:
        issues.append(err(where, "a frozen plan source must be planner-proposed, pending, proposed, with no docs"))
    unknown = [n for n in s.need_ids if n not in need_criteria]
    if unknown or not s.need_ids:
        issues.append(err(where, f"need_ids must be known and non-empty: {unknown or 'empty'}"))
    served = set().union(*(need_criteria.get(n, set()) for n in s.need_ids)) if s.need_ids else set()
    stray = sorted(set(s.criteria) - served)
    if stray or not s.criteria:
        issues.append(err(where, f"criteria must come from its needs: {stray or 'empty'}"))
    if not s.rationale.strip():
        issues.append(err(where, "rationale is empty"))
    return issues


def check_tiers_and_ranks(sources: list[m.Source]) -> list[Issue]:
    issues: list[Issue] = []
    for s in sources:
        top = KIND_TOP_TIER[s.kind]
        if TIER_ORDER[s.tier] < TIER_ORDER[top]:
            issues.append(err(s.source_id, f"kind {s.kind} may be at most {top}, not {s.tier}"))
    for tier in TIER_ORDER:
        ranks = sorted(s.rank for s in sources if s.tier == tier)
        if ranks != list(range(1, len(ranks) + 1)):
            issues.append(err(f"tier {tier}", f"ranks must be 1..{len(ranks)} with no gaps or ties, got {ranks}"))
    for tier in ("tier_2", "tier_3"):
        if not any(s.tier == tier for s in sources):
            issues.append(err(f"tier {tier}", "has no source"))
    return issues + check_revoke_target(sources)


def check_revoke_target(sources: list[m.Source]) -> list[Issue]:
    """S-006 is the tier_2 article the demo revokes, so it must be fetched late, after every priority source."""
    by_id = {s.source_id: s for s in sources}
    target = by_id.get(REVOKE_TARGET)
    if target is None:
        return [err(REVOKE_TARGET, "missing: the demo revokes it")]
    issues: list[Issue] = []
    if target.tier != "tier_2" or target.kind != "news":
        issues.append(err(REVOKE_TARGET, f"must be a tier_2 news article, is {target.tier} {target.kind}"))
    order = [s.source_id for s in collect_order(sources)]
    pos = order.index(REVOKE_TARGET)
    if any(by_id[sid].tier == "priority" for sid in order[pos + 1:]):
        issues.append(err(REVOKE_TARGET, "a priority source is fetched after it"))
    if pos < len(order) - 3:
        issues.append(warn(REVOKE_TARGET, f"fetched at position {pos + 1} of {len(order)}, not near the end"))
    return issues


def check_demo_order(sources: list[m.Source]) -> list[Issue]:
    """Sources added after S-010 must not reorder the demo: S-001 to S-010 keep their fetch order among
    themselves, and S-006, S-009 and S-010 stay the last three fetched."""
    order = [s.source_id for s in collect_order(sources)]
    demo = [sid for sid in order if sid in DEMO_SOURCES]
    issues: list[Issue] = []
    if demo != list(DEMO_FETCH_ORDER):
        issues.append(err("fetch order", f"S-001 to S-010 must be fetched as {list(DEMO_FETCH_ORDER)}, got {demo}"))
    if order[-len(DEMO_LAST):] != list(DEMO_LAST):
        issues.append(err("fetch order", f"{', '.join(DEMO_LAST)} must be fetched last, got {order[-3:]}"))
    return issues


def check_archive_sources(sources: list[m.Source]) -> list[Issue]:
    """Archive sources come after the demo sources, say when they were collected, carry the reliability of their
    kind and have a known archive file (also the s3:// fallback key)."""
    issues: list[Issue] = []
    for s in sources:
        if not is_archive(s):
            continue
        where = s.source_id
        num = int(s.source_id.split("-")[1])
        if num < FIRST_ARCHIVE_ID:
            issues.append(err(where, f"archive sources start at S-{FIRST_ARCHIVE_ID:03d}: S-001 to S-010 stay as they are"))
        want = ARCHIVE_RELIABILITY.get(s.kind)
        if want is None:
            issues.append(err(where, f"kind {s.kind} is not a primary document kind for the archive"))
        elif s.scores.reliability != want:
            issues.append(err(where, f"reliability must be {want} for an archived {s.kind}, not {s.scores.reliability}"))
        rel = ARCHIVE_FILES.get(s.source_id)
        if rel is None:
            issues.append(err(where, "no ARCHIVE_FILES entry: the archive file this source was collected as"))
        elif urlsplit(s.url).scheme == "s3" and urlsplit(s.url).path != f"/{ARCHIVE_PREFIX}{rel}":
            issues.append(err(where, f"s3 url must point at /{ARCHIVE_PREFIX}{rel}"))
    return issues


def check_coverage(depth: str, needs: list[m.DataNeed], sources: list[m.Source]) -> list[Issue]:
    """Every in-scope criterion has a need and a source, and every need has at least one source."""
    scope = in_scope(depth)
    issues: list[Issue] = []
    for criterion in scope:
        if not any(criterion in n.criteria for n in needs):
            issues.append(err(criterion, "no need serves this in-scope criterion"))
        if not any(criterion in s.criteria for s in sources):
            issues.append(err(criterion, "no source serves this in-scope criterion"))
    for n in needs:
        if not any(n.need_id in s.need_ids for s in sources):
            issues.append(err(n.need_id, "no source serves this need"))
        out = sorted(set(n.criteria) - set(scope))
        if out:
            issues.append(warn(n.need_id, f"criteria outside the {depth} depth: {out}"))
    return issues


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def check_ranges(where: str, ranges: Any) -> list[Issue]:
    """[[a, b], ...] with 1 <= a <= b, sorted and not overlapping."""
    if not isinstance(ranges, list) or not ranges:
        return [err(where, "must be a non-empty list of [first, last] pairs")]
    last = 0
    for r in ranges:
        ok = (isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in r)
              and 1 <= r[0] <= r[1])
        if not ok:
            return [err(where, f"bad range {r!r}")]
        if r[0] <= last:
            return [err(where, f"range {r} overlaps or is out of order")]
        last = r[1]
    return []


def check_pages(plan: dict[str, Any], needs: list[m.DataNeed], sources: list[m.Source]) -> list[Issue]:
    extract = plan.get("extract_pages") or {}
    by_id = {s.source_id: s for s in sources}
    issues: list[Issue] = []
    for sid, ranges in extract.items():
        source = by_id.get(sid)
        if source is None or not is_pdf(source):
            issues.append(err(f"extract_pages.{sid}", "only PDF sources in the plan get extract pages"))
        issues += check_ranges(f"extract_pages.{sid}", ranges)
    for s in sources:
        if is_pdf(s) and s.source_id not in extract:
            issues.append(err(f"extract_pages.{s.source_id}", "PDF source without extract pages"))
    if issues:
        return issues
    demo, archived = page_totals(plan)
    low, high = PAGE_BUDGET
    if not low <= demo <= high:
        issues.append(err("extract_pages", f"{demo} pages outside the archive sources, the budget is {low} to {high}"))
    if archived > ARCHIVE_PAGE_MAX:
        issues.append(err("extract_pages", f"{archived} pages from archive sources, at most {ARCHIVE_PAGE_MAX}"))
    issues += check_need_pages(plan.get("need_pages"), extract, needs, by_id)
    issues += check_anchor_table(extract)
    return issues


def check_need_pages(need_pages: Any, extract: dict[str, list[list[int]]], needs: list[m.DataNeed],
                     by_id: dict[str, m.Source]) -> list[Issue]:
    """need_pages is optional. When present, every range sits inside extract_pages and every page is used."""
    if need_pages is None:
        return []
    need_ids = {n.need_id for n in needs}
    issues: list[Issue] = []
    used: dict[str, set[int]] = {sid: set() for sid in extract}
    for nid, per_source in need_pages.items():
        if nid not in need_ids:
            issues.append(err(f"need_pages.{nid}", "unknown need"))
            continue
        for sid, ranges in per_source.items():
            where = f"need_pages.{nid}.{sid}"
            if sid not in extract:
                issues.append(err(where, "source has no extract pages"))
                continue
            if nid not in by_id[sid].need_ids:
                issues.append(err(where, f"{sid} does not list {nid} in need_ids"))
            bad = check_ranges(where, ranges)
            if bad:
                issues += bad
                continue
            outside = sorted(set(expand(ranges)) - set(expand(extract[sid])))
            if outside:
                issues.append(err(where, f"pages {outside} are not in extract_pages"))
            used[sid].update(expand(ranges))
    for sid, pages in used.items():
        idle = sorted(set(expand(extract[sid])) - pages)
        if idle:
            issues.append(warn(f"extract_pages.{sid}", f"pages {idle} serve no need in need_pages"))
    return issues


def check_anchor_table(extract: dict[str, list[list[int]]]) -> list[Issue]:
    """PAGE_ANCHORS must name exactly the extract pages, so --pages verifies every one of them."""
    issues: list[Issue] = []
    for sid in sorted(set(extract) | set(PAGE_ANCHORS)):
        want = set(expand(extract.get(sid, [])))
        have = set(PAGE_ANCHORS.get(sid, {}))
        if want != have:
            issues.append(err(f"PAGE_ANCHORS.{sid}", f"missing {sorted(want - have)}, extra {sorted(have - want)}"))
    return issues


# ---------------------------------------------------------------------------
# The planted test page
# ---------------------------------------------------------------------------


def check_test_page_source(sources: list[m.Source]) -> list[Issue]:
    page = next((s for s in sources if s.url == TEST_PAGE_URL), None)
    if page is None:
        return [err("test page", f"no source with url {TEST_PAGE_URL}")]
    issues: list[Issue] = []
    if page.tier != "tier_3" or page.scores.reliability > 0.1:
        issues.append(err(page.source_id, "the test page must be tier_3 with reliability 0.1 or less"))
    if "(simulated)" not in page.title or "SIMULATED" not in page.rationale:
        issues.append(err(page.source_id, "the test page must be labeled simulated in title and rationale"))
    return issues


def check_test_page_file(path: Path = TEST_PAGE_PATH) -> list[Issue]:
    """Title, a visible and a hidden injection, the simulated label and one link to example.org."""
    if not path.exists():
        return [err("test page file", f"{path} is missing")]
    html = path.read_text(encoding="utf-8")
    where = "test page file"
    issues: list[Issue] = []
    if not html.lower().startswith("<!doctype html"):
        issues.append(err(where, "must start with <!doctype html> (raw GitHub serves it as text/plain)"))
    if f"<title>{TEST_PAGE_TITLE}</title>" not in html:
        issues.append(err(where, f"title must be {TEST_PAGE_TITLE!r}"))
    if "SIMULATED" not in html:
        issues.append(err(where, "needs a visible SIMULATED label"))
    if html.count("Ignore all previous instructions") < 2:
        issues.append(err(where, "needs one visible and one hidden injection"))
    hidden = re.search(r'<span style="[^"]*(?:font-size:\s*1px|display:\s*none)[^"]*"[^>]*>\s*Ignore all previous', html)
    if not hidden:
        issues.append(err(where, "the hidden injection is not in a hidden span"))
    if re.search(r'class="[^"]*comment|aria-hidden', html):
        issues.append(err(where, 'trafilatura drops class="comment" blocks and aria-hidden elements, '
                                 "so the scan would never see the injection"))
    hrefs = re.findall(r'href="([^"]+)"', html)
    if [urlsplit(h).hostname for h in hrefs] != ["example.org"]:
        issues.append(err(where, f"must have exactly one link, to example.org, got {hrefs}"))
    return issues


def check_test_page_override(url: str) -> list[Issue]:
    """TEST_PAGE_URL must be Axelrod's own route for this page: http(s), a host, the path /test-pages/injection.html."""
    parts = urlsplit(url)
    where = "TEST_PAGE_URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return [err(where, f"must be an http(s) URL with a host, got {url!r}")]
    issues: list[Issue] = []
    if not parts.path.endswith(SERVED_PATH):
        issues.append(err(where, f"path must end with {SERVED_PATH} (the API route that serves the page)"))
    if parts.query or parts.fragment:
        issues.append(err(where, "must have no query or fragment: the gateway rule's path prefix is the URL path"))
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        issues.append(warn(where, "plain http on a non-local host: use the https API Gateway invoke URL"))
    return issues


def check_served_page(url: str, user_agent: str, timeout: float = 20.0, opener: Opener | None = None,
                      path: Path = TEST_PAGE_PATH) -> tuple[list[Issue], str]:
    """GET the page from Axelrod's API: 200, HTML, the simulated header and title, ideally the same bytes."""
    opener = opener or urllib.request.urlopen
    where = "TEST_PAGE_URL"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with opener(req, timeout=timeout) as resp:
            status, headers, body = int(resp.status), resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return [err(where, f"HTTP {exc.code} from {url}. Is the Axelrod API running there?")], f"  FAIL served page {url}"
    except Exception as exc:  # noqa: BLE001  any network error is a failed check
        return [err(where, f"{type(exc).__name__}: {exc}. Is the Axelrod API running there?")], f"  FAIL served page {url}"
    issues: list[Issue] = []
    if status != 200:
        issues.append(err(where, f"HTTP {status}, expected 200"))
    if not (headers.get("Content-Type") or "").lower().startswith("text/html"):
        issues.append(err(where, f"Content-Type {headers.get('Content-Type')!r} is not text/html"))
    if (headers.get(SIMULATED_HEADER) or "").lower() != "true":
        issues.append(err(where, f"{SIMULATED_HEADER}: true header is missing"))
    if f"<title>{TEST_PAGE_TITLE}</title>".encode("utf-8") not in body:
        issues.append(err(where, f"the page title is not {TEST_PAGE_TITLE!r}"))
    if not issues and path.exists() and body != path.read_bytes():
        issues.append(warn(where, "served bytes differ from the local file: the server runs another commit"))
    errors = sum(1 for i in issues if i.level == "error")
    return issues, f"  {'FAIL' if errors else 'ok  '} served page {status} {len(body)} bytes {url}"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

Opener = Callable[..., Any]


def probe_url(url: str, user_agent: str, timeout: float = 20.0, opener: Opener = urllib.request.urlopen,
              retries: int = 1) -> Probe:
    """HEAD first. When HEAD fails, a ranged GET of the first 1 KB (some servers refuse HEAD or flake on it)."""
    head = request_once(url, "HEAD", user_agent, timeout, opener)
    if head.ok:
        return head
    last = head
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(1.0)
        last = request_once(url, "GET", user_agent, timeout, opener, headers={"Range": "bytes=0-1023"})
        if last.ok:
            return last
    return Probe(False, last.status, last.method, f"HEAD {head.status or head.detail}, GET {last.status or last.detail}")


def request_once(url: str, method: str, user_agent: str, timeout: float, opener: Opener,
                 headers: dict[str, str] | None = None) -> Probe:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": user_agent, **(headers or {})})
    try:
        with opener(req, timeout=timeout) as resp:
            if method == "GET":
                resp.read(1024)
            status = int(resp.status)
            ctype = resp.headers.get("Content-Type", "")
            return Probe(200 <= status < 300, status, method, ctype)
    except urllib.error.HTTPError as exc:
        return Probe(False, exc.code, method, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001  network errors of every kind are a failed probe
        return Probe(False, None, method, f"{type(exc).__name__}: {exc}")


def check_urls(sources: list[m.Source], probe: Callable[[str], Probe],
               test_page_url: str | None = None, vault_bucket: str | None = None) -> tuple[list[Issue], list[str]]:
    """Every source URL must answer. The GitHub test page only warns, since it answers only after the push.

    With test_page_url (TEST_PAGE_URL) set, the test page is probed there instead, and a failure is an error.
    An archive source whose URL does not answer live (or is an s3:// URL, never probed) only warns, because the
    run serves its archived copy from the document cache.
    """
    issues: list[Issue] = []
    lines: list[str] = []
    for s in sources:
        if urlsplit(s.url).scheme == "s3":
            lines.append(f"  skip {s.source_id} s3 url, answers only from the document cache {s.url}")
            issues.append(warn(s.source_id, "s3:// archive copy: answers only from the document cache "
                                            "(CACHE_DOCUMENTS=on)"))
            continue
        served = bool(test_page_url) and s.url == TEST_PAGE_URL
        url = test_page_url if served else s.url
        result = probe(url)
        lines.append(f"  {'ok  ' if result.ok else 'FAIL'} {s.source_id} {result.method} {result.status} "
                     f"{result.detail} {url}")
        if result.ok:
            continue
        if served:
            issues.append(err(s.source_id, f"test page at TEST_PAGE_URL does not answer ({result.detail}). "
                                           "Is the Axelrod API running there?"))
        elif s.url == TEST_PAGE_URL:
            issues.append(warn(s.source_id, f"test page does not answer yet ({result.detail}). "
                                            "Expected until the backend branch is pushed"))
        elif is_archive(s):
            issues.append(warn(s.source_id, f"url does not answer live ({result.detail}). Archive source: the run "
                                            "serves the copy collected before the event from the document cache "
                                            f"(CACHE_DOCUMENTS=on), stored as {archive_s3_url(s.source_id, vault_bucket)}"))
        else:
            issues.append(err(s.source_id, f"url does not answer: {result.detail}"))
    return issues, lines


# ---------------------------------------------------------------------------
# Pages against the real PDFs (--pages)
# ---------------------------------------------------------------------------


def download(url: str, dest: Path, user_agent: str, timeout: float = 120.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


def verify_pages(plan: dict[str, Any], pdf_dir: Path | None, user_agent: str,
                 fetch: Callable[[str, Path, str], None] = download,
                 archive_dir: Path | None = None) -> tuple[list[Issue], list[str]]:
    """Open each PDF and find every page's anchor.

    A source with a local archive copy (ARCHIVE_FILES under archive_dir) is opened in place, read only, never
    copied. Any other PDF is downloaded into pdf_dir as <source_id>.pdf when missing. With neither, the source
    is skipped with a warning.
    """
    for flag, folder in (("--pages", pdf_dir), ("--archive", archive_dir)):
        if folder is not None and folder.resolve().is_relative_to(REPO):
            return [err(flag, f"{folder.resolve()} is inside the repo. PDFs must never land in the repo")], []
    if pdf_dir is not None:
        pdf_dir = pdf_dir.resolve()
        pdf_dir.mkdir(parents=True, exist_ok=True)
    urls = {s["source_id"]: s["url"] for s in plan["sources"]}
    issues: list[Issue] = []
    lines: list[str] = []
    for sid, ranges in plan["extract_pages"].items():
        path = local_copy(sid, archive_dir)
        if path is None and pdf_dir is not None:
            path = pdf_dir / f"{sid}.pdf"
            if not path.exists():
                fetch(urls[sid], path, user_agent)
        if path is None:
            issues.append(warn(f"{sid} pages", "not checked: no local archive copy, and no --pages folder to "
                                               "download into"))
            lines.append(f"  skip {sid}: no local copy")
            continue
        found, line = check_pdf_pages(sid, path, expand(ranges))
        issues += found
        lines.append(line)
    return issues, lines


def local_copy(source_id: str, archive_dir: Path | None) -> Path | None:
    """The archive's own file for this source, when the archive folder is given and holds it."""
    rel = ARCHIVE_FILES.get(source_id)
    if archive_dir is None or rel is None:
        return None
    path = archive_dir / rel
    return path if path.is_file() else None


def check_pdf_pages(sid: str, path: Path, pages: list[int]) -> tuple[list[Issue], str]:
    import pymupdf

    issues: list[Issue] = []
    with pymupdf.open(path) as doc:
        count = doc.page_count
        for page in pages:
            anchor = PAGE_ANCHORS.get(sid, {}).get(page)
            if page > count:
                issues.append(err(f"{sid} page {page}", f"the PDF has only {count} pages"))
            elif anchor is None or norm_text(anchor) not in norm_text(doc[page - 1].get_text()):
                issues.append(err(f"{sid} page {page}", f"anchor not found: {anchor!r}"))
    bad = len(issues)
    return issues, f"  {'ok  ' if not bad else 'FAIL'} {sid}: {len(pages) - bad}/{len(pages)} pages hold their anchor ({count} pages in {path.name})"


def fetch_order_line(plan: dict[str, Any]) -> str:
    """The order collect will fetch in, archive sources marked, so the demo order is visible at a glance."""
    sources = [m.Source.model_validate(s) for s in plan["sources"]]
    order = [f"{s.source_id}{'*' if is_archive(s) else ''}" for s in collect_order(sources)]
    return f"fetch order (* archive, collected before the event): {' '.join(order)}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        print(f"  {issue.level.upper():5} {issue.where}: {issue.message}")


def summary(plan: dict[str, Any]) -> str:
    demo, archived = page_totals(plan)
    tiers = {t: sum(1 for s in plan.get("sources", []) if s.get("tier") == t) for t in TIER_ORDER}
    return (f"{len(plan.get('needs', []))} needs, {len(plan.get('sources', []))} sources {tiers} "
            f"({len(archive_ids(plan))} from the archive), {demo + archived} extract pages "
            f"({demo} + {archived} from the archive)")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the frozen SHL plan.")
    parser.add_argument("--plan", type=Path, default=PLAN_PATH, help="plan.json to check")
    parser.add_argument("--offline", action="store_true", help="skip every network request")
    parser.add_argument("--pages", type=Path, metavar="DIR",
                        help="folder outside the repo for the PDFs, to check every extract page against its anchor")
    parser.add_argument("--archive", type=Path, metavar="DIR", default=os.environ.get(ARCHIVE_ENV) or None,
                        help=f"the local summer 2026 archive (read only), to check pages of the PDFs it holds "
                             f"without a download (default: ${ARCHIVE_ENV})")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per URL request")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    plan = load_plan(args.plan)
    print(f"check_plan: {args.plan.name}: {summary(plan)}")
    try:
        print(fetch_order_line(plan))
    except ValidationError:
        pass  # check_structure reports the invalid source
    from backend.axelrod.config import get_settings

    settings = get_settings()
    served = (settings.test_page_url or "").strip() or None
    issues = check_structure(plan) + check_test_page_file()
    if served:
        print(f"test page: TEST_PAGE_URL is set, runs fetch S-010 from {served}")
        issues += check_test_page_override(served)
    print_issues(issues)
    user_agent = settings.http_user_agent
    if not args.offline:
        sources = [m.Source.model_validate(s) for s in plan["sources"]]
        print("urls:")
        url_issues, lines = check_urls(sources, lambda u: probe_url(u, user_agent, args.timeout), served,
                                       settings.vault_bucket)
        if served:
            page_issues, line = check_served_page(served, user_agent, args.timeout)
            url_issues += page_issues
            lines.append(line)
        print("\n".join(lines))
        print_issues(url_issues)
        issues += url_issues
    if args.pages or args.archive:
        where = " and ".join(str(p) for p in (args.archive, args.pages) if p)
        print(f"pages against the PDFs in {where}:")
        page_issues, lines = verify_pages(plan, args.pages, user_agent, archive_dir=args.archive)
        print("\n".join(lines))
        print_issues(page_issues)
        issues += page_issues
    errors = sum(1 for i in issues if i.level == "error")
    warnings = len(issues) - errors
    print(f"check_plan: {'FAIL' if errors else 'PASS'} ({errors} errors, {warnings} warnings)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
