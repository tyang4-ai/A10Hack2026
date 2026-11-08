"""Pre-smoke: a local run with one source and a few pages, auto-approved, every built stage, then a summary.

Usage (from the repo root): PY backend/scripts/presmoke.py --source S-001 --pages a,b,c,d,e

The plan comes from data/shl/plan.json, cut down to that one source and those pages. Approval is automatic
(auto_approved) and the policy is built by the real Gateway, so every fetch and model call goes through it.
Stages run in contract order. A stage that is still a stub is reported, not crashed on. The run lives in a
temp folder outside the repo and is deleted afterwards unless --keep is given (never commit a PDF).
STORE is always local. Models are live unless FAKE_MODELS=1 or --fake (the fakes are simulated).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

PLAN_PATH = Path(__file__).resolve().parents[1] / "data" / "shl" / "plan.json"
AFTER_APPROVE = ("collect", "extract", "draft", "verify", "repair", "summarize", "verify_summary", "complete")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        pages = parse_pages(args.pages) if args.pages else None
        if args.until and args.until not in AFTER_APPROVE:
            raise ValueError(f"--until must be one of {', '.join(AFTER_APPROVE)}")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    tmp = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="axelrod-presmoke-"))
    try:
        return asyncio.run(_run(args, plan, pages, tmp))
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.keep or args.data_dir:
            print(f"\nkept the run under {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default="S-001", help="source id from plan.json (default S-001)")
    p.add_argument("--pages", help="1-based pages: a,b,c or a-b, mixed is fine (default: the plan's extract_pages)")
    p.add_argument("--until", help="last stage to run (default: every stage through complete)")
    p.add_argument("--budget", type=float, default=1.0, help="run budget in USD, enforced by the gateway (default 1.0)")
    p.add_argument("--fake", action="store_true", help="use the simulated fake models (no Bedrock)")
    p.add_argument("--plan", default=str(PLAN_PATH), help="plan.json path")
    p.add_argument("--data-dir", help="keep the run in this folder instead of a temp one")
    p.add_argument("--keep", action="store_true", help="keep the temp run folder and print its path")
    return p


def parse_pages(text: str) -> list[int]:
    """'138,140-141' gives [138, 140, 141]. 1-based, sorted, no duplicates."""
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if not lo.strip().isdigit() or (hi and not hi.strip().isdigit()):
            raise ValueError(f"--pages wants numbers like 1,2,5-7, got {part!r}")
        a, b = int(lo), int(hi or lo)
        if a < 1 or b < a:
            raise ValueError(f"--pages range {part!r} is empty or below 1")
        out.update(range(a, b + 1))
    if not out:
        raise ValueError("--pages is empty")
    return sorted(out)


def cut_plan(plan: dict, source_id: str, pages: list[int] | None):
    """(needs, source, extract_pages, need_pages) for one source, auto-approved."""
    from backend.axelrod.contract import m

    raw = next((s for s in plan.get("sources", []) if s.get("source_id") == source_id), None)
    if raw is None:
        raise KeyError(f"{source_id} is not in the plan")
    source = m.Source.model_validate(raw).model_copy(update={
        "approval": "auto_approved", "status": "queued", "doc_ids": [], "stats": m.SourceStats(), "error": None})
    needs = [m.DataNeed.model_validate(n) for n in plan.get("needs", []) if n.get("need_id") in source.need_ids]
    if pages is not None:
        extract = {source_id: [[p, p] for p in pages]}
    else:
        extract = {k: v for k, v in (plan.get("extract_pages") or {}).items() if k == source_id}
    need_pages = {nid: {source_id: per[source_id]} for nid, per in (plan.get("need_pages") or {}).items()
                  if source_id in per}
    return needs, source, extract, need_pages


def build_context(args, plan: dict, pages: list[int] | None, data_dir: Path):
    from backend.axelrod.config import load_settings
    from backend.axelrod.contract import m
    from backend.axelrod.runtime import RunContext
    from backend.axelrod.store import Store

    base = load_settings()
    settings = base.model_copy(update={
        "store": "local", "data_dir": data_dir, "vault_bucket": None, "runs_table": None, "events_table": None,
        "demo_pacing": 0.0, "fake_models": bool(args.fake or base.fake_models)})
    needs, source, extract, need_pages = cut_plan(plan, args.source, pages)
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(
        company=plan.get("company", "Siemens Healthineers AG"), ticker=plan.get("ticker"),
        notes=f"Pre-smoke (presmoke.py): {args.source} only, auto-approved", budget_usd=args.budget,
        created_via="cli"))
    state.needs, state.sources = needs, [source]
    state.extract_pages, state.need_pages = extract, need_pages
    state.as_of, state.fiscal_note, state.plan_ready = plan.get("as_of"), plan.get("fiscal_note"), True
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message=f"Pre-smoke run for {source.source_id}", stage="plan")
    ctx.state.run.stage = "approve"
    ctx.gateway.build_policy()  # allows only this source, default deny for the rest
    return ctx


async def run_stages(ctx, until: str | None) -> list[tuple[str, str, float]]:
    """(stage, outcome, seconds) for each stage after approve. Stubs and failures are reported, not raised."""
    from backend.axelrod.orchestrator import STAGES
    from backend.axelrod.runtime import StopRequested

    modules = dict(STAGES)
    outcomes: list[tuple[str, str, float]] = []
    for stage in AFTER_APPROVE:
        ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
        t0 = time.perf_counter()
        try:
            mod = importlib.import_module(f"backend.axelrod.stages.{modules[stage]}")
            await mod.run(ctx)
            outcome = "ok"
        except NotImplementedError as exc:
            outcome = f"not built yet ({exc or 'stub'})"
        except StopRequested as exc:
            outcomes.append((stage, f"stopped: {exc}", time.perf_counter() - t0))
            break
        except Exception as exc:  # noqa: BLE001  report it and go on, later stages may still be stubs
            outcome = f"failed: {type(exc).__name__}: {exc}"
        outcomes.append((stage, outcome, time.perf_counter() - t0))
        if stage == until:
            break
    return outcomes


async def _run(args, plan: dict, pages: list[int] | None, data_dir: Path) -> int:
    ctx = build_context(args, plan, pages, data_dir)
    mode = "FAKE models (simulated)" if ctx.settings.fake_models else "live Bedrock through the gateway"
    print(f"run {ctx.run_id}: {args.source} pages {pages or ctx.state.extract_pages.get(args.source)} ({mode}), "
          f"budget ${args.budget:.2f}")
    t0 = time.perf_counter()
    outcomes = await run_stages(ctx, args.until)
    ctx.store.save(ctx.state)
    print_summary(ctx, outcomes, time.perf_counter() - t0)
    extract = next((o for s, o, _ in outcomes if s == "extract"), None)
    return 0 if extract == "ok" else 1


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(ctx, outcomes: list[tuple[str, str, float]], seconds: float) -> None:
    state = ctx.state
    print("\nStages")
    for stage, outcome, secs in outcomes:
        print(f"  {stage:<15} {outcome}  ({secs:.1f}s)")
    for s in state.sources:
        print(f"\nSource {s.source_id}: {s.status}, {s.stats.pages} page(s), {s.stats.data_items} item(s), "
              f"{s.stats.data_items_verified} verified" + (f", error: {s.error}" if s.error else ""))
    data = state.data_items()
    counts: dict[str, int] = {}
    for it in state.items.values():
        key = f"{it.kind}:{it.verification.status}"
        counts[key] = counts.get(key, 0) + 1
    ok = sum(i.verification.status in ("pass", "fixed") for i in data)
    pct = f"{100 * ok / len(data):.0f}%" if data else "n/a"
    print(f"\nItems: {len(state.items)} ({', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'none'})")
    print(f"Data items pass or fixed: {ok} of {len(data)} ({pct})")
    for it in data:
        if it.verification.status == "fixed":
            print(f"  fixed {it.id}: {_attempt_note(it)}")
    bad = [i for i in state.items.values() if i.verification.status not in ("pass", "fixed")]
    if bad:
        print("Not passing:")
        for it in bad:
            print(f"  {it.id} {it.verification.status}: {_label(it)}")
            for c in it.verification.checks:
                if c.result != "pass":
                    print(f"      {c.check} {c.result}: {c.detail}")
            diff = _first_diff(it)
            print(f"      diff: {diff}" if diff else "      diff: none (no quote mismatch)")
    print(f"\nHoles: {len(state.holes)}")
    for h in state.holes:
        print(f"  {h.hole_id}: {h.reason}")
    agents = state.agents.values()
    by_status: dict[str, int] = {}
    for a in agents:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    print(f"\nAgents: {len(state.agents)} ({', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))})")
    u, c = state.run.usage, state.run.counts
    print(f"Gateway: {c.gateway_allowed} allowed, {c.gateway_blocked} blocked, {c.gateway_flagged} flagged")
    sim = " (simulated: fake models, fake token counts)" if ctx.settings.fake_models else ""
    print(f"Cost{sim}: ${u.cost_usd:.4f} for {u.model_calls} model call(s), {u.tokens_in} tokens in, "
          f"{u.tokens_out} out (budget ${u.budget_usd if u.budget_usd is not None else ctx.settings.budget_usd:.2f})")
    print(f"Elapsed: {seconds:.1f}s")


def _label(it) -> str:
    s = getattr(it, "statement", None) or getattr(it, "text", None) or getattr(it, "title", "")
    return s if len(s) <= 110 else s[:107] + "..."


def _attempt_note(it) -> str:
    notes = [a.note for a in it.verification.attempts if a.note]
    return notes[0] if notes else _label(it)


def _first_diff(it) -> str | None:
    """The first quote diff in the checks or the attempt history, as [-agent-]{+source+} marks."""
    pools = [it.verification.checks] + [a.checks for a in it.verification.attempts]
    for pool in pools:
        for c in pool:
            if c.diff:
                return render_diff(c.diff)
    return None


def render_diff(diff) -> str:
    out = []
    for d in diff:
        if d.op == "equal":
            out.append(d.agent if len(d.agent) <= 24 else d.agent[:10] + "..." + d.agent[-10:])
        else:
            out.append((f"[-{d.agent}-]" if d.agent else "") + (f"{{+{d.source}+}}" if d.source else ""))
    return "".join(out).replace("\n", " ")


if __name__ == "__main__":
    sys.exit(main())
