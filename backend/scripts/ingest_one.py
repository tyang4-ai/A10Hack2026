"""Fetch and ingest one source from data/shl/plan.json into a throwaway local run and print each page's first 400 characters.

Usage (from the repo root): PY backend/scripts/ingest_one.py <source_id> --pages a-b [--find TEXT] [--keep]

The fetch goes through the real Gateway with a policy that allows only this source, so it is the same path
the collect stage takes. The run lives in a temp folder outside the repo and is deleted afterwards unless
--keep is given (never commit a PDF). No AWS: the store is local and the Guardrails scan is skipped.
If plan.json is missing, S-001 falls back to the FY2025 annual report URL so the PDF path can still be checked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

PLAN_PATH = Path(__file__).resolve().parents[1] / "data" / "shl" / "plan.json"
FALLBACK_S001 = ("https://corporate.webassets.siemens-healthineers.com/a364b935102e7df5/d7fdacda458e/"
                 "siemens-healthineers-ir-annual-report-2025.pdf")
PREVIEW_CHARS = 400


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        plan, source = load_source(args.source_id, Path(args.plan))
        pages = parse_range(args.pages) if args.pages else None
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="axelrod-ingest-"))
    try:
        return asyncio.run(_run(plan, source, tmp, pages, args.find))
    finally:
        if args.keep:
            print(f"\nkept the throwaway run under {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("source_id", help="for example S-001")
    p.add_argument("--pages", help="1-based page range to print, a-b or n (default: every page)")
    p.add_argument("--find", help="also list the pages whose text contains this string")
    p.add_argument("--plan", default=str(PLAN_PATH), help="plan.json path (default: backend/data/shl/plan.json)")
    p.add_argument("--keep", action="store_true", help="keep the temp run folder and print its path")
    return p


def parse_range(text: str) -> tuple[int, int]:
    """'150-156' gives (150, 156), '12' gives (12, 12). 1-based, inclusive."""
    parts = text.split("-")
    if len(parts) not in (1, 2) or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"--pages wants a-b or n, got {text!r}")
    lo, hi = int(parts[0]), int(parts[-1])
    if lo < 1 or hi < lo:
        raise ValueError(f"--pages range {text!r} is empty or below 1")
    return lo, hi


def load_source(source_id: str, plan_path: Path):
    """(plan dict or None, approved m.Source) from plan.json, or the S-001 fallback when the file is missing."""
    from backend.axelrod.contract import m

    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for raw in plan.get("sources", []):
            if raw.get("source_id") == source_id:
                source = m.Source.model_validate(raw)
                return plan, source.model_copy(update={"approval": "approved", "status": "queued"})
        raise KeyError(f"{source_id} is not in {plan_path}")
    if source_id != "S-001":
        raise KeyError(f"{plan_path} is missing, and only S-001 has a fallback")
    print(f"note: {plan_path} is missing, using the FY2025 annual report URL as S-001", file=sys.stderr)
    return None, m.Source(
        source_id="S-001", title="Siemens Healthineers Annual Report FY2025", url=FALLBACK_S001,
        domain=urlsplit(FALLBACK_S001).hostname or "", kind="annual_report", data_types=["financial_filing"],
        access=m.SourceAccess(), tier="priority", rank=1, scores=m.SourceScores(reliability=0.95, relevance=1.0),
        rationale="Fallback used by ingest_one.py when data/shl/plan.json is missing", need_ids=[], criteria=[],
        vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved", status="queued",
    )


def build_context(plan: dict | None, source, data_dir: Path):
    """A throwaway local run whose policy allows only this source."""
    from backend.axelrod.config import load_settings
    from backend.axelrod.contract import m
    from backend.axelrod.runtime import RunContext
    from backend.axelrod.store import Store

    settings = load_settings().model_copy(update={
        "store": "local", "data_dir": data_dir, "fake_models": True, "guardrail_id": None,
        "guardrail_version": None, "vault_bucket": None, "runs_table": None, "events_table": None,
        "stage_pacing": 0.0,
    })
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(
        company=(plan or {}).get("company", "Siemens Healthineers AG"), ticker=(plan or {}).get("ticker", "SHL"),
        notes="Throwaway run from ingest_one.py", created_via="cli"))
    state.run.stage = "collect"
    state.sources = [source]
    extract = (plan or {}).get("extract_pages", {})
    if source.source_id in extract:
        state.extract_pages = {source.source_id: extract[source.source_id]}
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()  # allows only this source, default deny for the rest
    return ctx


async def _run(plan: dict | None, source, data_dir: Path, pages: tuple[int, int] | None, find: str | None) -> int:
    from backend.axelrod.stages.collect import collect_source, finish_fetcher, spawn_fetcher

    ctx = build_context(plan, source, data_dir)
    agent = spawn_fetcher(ctx, [source.source_id])
    status = "failed"
    try:
        doc = await collect_source(ctx, agent, source)
        status = "done"
    finally:
        finish_fetcher(ctx, agent, status)
    for event in ctx.state.events:
        if event.type in ("gateway.request", "source.status", "doc.stored"):
            print(f"[{event.type}] {event.message}")
    if doc is None:
        held = ctx.state.sources[0]
        print(f"\n{held.source_id} not stored: {held.status} ({held.error})")
        return 1
    texts = ctx.state.pages[doc.doc_id]
    print(f"\n{doc.doc_id} {doc.content_type} pages={doc.pages} bytes={doc.bytes} sha256={doc.sha256[:16]}... "
          f"quarantined={doc.quarantined} scan={doc.scan.verdict}")
    print(f"raw={doc.vault_paths.raw}\ntext={doc.vault_paths.text}\nmeta={doc.vault_paths.meta}")
    if source.source_id in ctx.state.extract_pages:
        print(f"extract_pages={ctx.state.extract_pages[source.source_id]}")
    if find:
        hits = [n for n, t in enumerate(texts, start=1) if find in t]
        print(f"pages containing {find!r}: {hits if hits else 'none'}")
    lo, hi = pages or (1, len(texts))
    for n in range(lo, min(hi, len(texts)) + 1):
        print(f"\n----- page {n} of {len(texts)} -----")
        print(texts[n - 1][:PREVIEW_CHARS])
    return 0


if __name__ == "__main__":
    sys.exit(main())
