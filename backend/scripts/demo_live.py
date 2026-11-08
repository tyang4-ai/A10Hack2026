"""Rehearse the A10 live segment over HTTP like a presenter, and check every beat from the run's events.

Usage (from the repo root):
  PY backend/scripts/demo_live.py --api http://127.0.0.1:8010 [--pacing-check] [--budget 0.5] [--timeout 900]
  PY backend/scripts/demo_live.py --api <url> --check <run_id>     # score a run the presenter drove with the CLI

What it does, in PLAN.md's order (A10's four steps):
  1. Launch: create a run (created_via "cli") for Siemens Healthineers and approve it per plan.json (every
     priority source approved, the rest left to auto-approval).
  2. Traffic: fetches and model calls go through the gateway, with tokens and cost.
  3. Block: revoke S-006 as soon as collect starts (with --pacing-check: only once the fetcher logs
     "next: S-006", the way a presenter reacts). The S-009 link-follow must be blocked by default deny, the
     next S-006 request by R-revoke-S-006, and S-010 (our planted test page, simulated) flagged and quarantined.
  4. Stop: STOP once extract has made its first allowed model call. The next model call must be blocked and
     nothing may be allowed afterwards.
Then it prints PASS or FAIL per beat and the exact CLI commands for each beat (for backend/DEMO.md).

The API it talks to decides the rest: run it with live Bedrock, GUARDRAIL_ID set, DEMO_PACING (2 to 3 s) and
TEST_PAGE_URL pointing at that same API's /test-pages/injection.html. A STOP early in extract keeps the spend to
a few cents. Like the CLI, it only talks to Axelrod's own API (stdlib urllib through cli.Client).
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

from backend.axelrod.cli import Client, CliError, api_base, api_key, decisions_for  # noqa: E402

TERMINAL = ("done", "stopped", "failed")
COMPANY = "Siemens Healthineers"
TICKER = "SHL"
REVOKE_ID = "S-006"
LINK_FROM = "S-009"
PLANTED_ID = "S-010"
REVOKE_REASON = "Demo: the analyst revoked this source mid-run"
STOP_REASON = "Demo: the presenter pressed STOP"
CLI = "python -m backend.axelrod.cli"
PAGE = 2000
Say = Callable[[str], None]


@dataclass
class Options:
    company: str = COMPANY
    ticker: str = TICKER
    revoke_id: str = REVOKE_ID
    link_from: str = LINK_FROM
    planted_id: str = PLANTED_ID
    pacing_check: bool = False
    min_gap_s: float = 1.0          # --pacing-check: seconds the presenter needs between "next: S-006" and its request
    budget_usd: float | None = 0.5
    timeout_s: float = 900.0        # the whole rehearsal
    settle_s: float = 180.0         # after STOP: in-flight model calls land and are recorded as blocked
    poll_s: float = 0.25
    approve_wait_s: float = 120.0


@dataclass
class Actions:
    """What the rehearsal did, with the last event seq it had seen when it did it."""
    run_id: str = ""
    revoke_after_seq: int | None = None
    revoke_error: str | None = None
    stop_after_seq: int | None = None
    stop_error: str | None = None
    timed_out: bool = False


@dataclass
class Beat:
    key: str
    title: str
    ok: bool
    detail: str


@dataclass
class Rehearsal:
    actions: Actions
    run: dict
    events: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.beats) and all(b.ok for b in self.beats)


# ---------------------------------------------------------------------------
# Driving the run
# ---------------------------------------------------------------------------


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def create_run(client: Client, opts: Options) -> dict:
    body = {"company": opts.company, "ticker": opts.ticker, "depth": "screen", "created_via": "cli",
            "notes": "Rehearsal of the A10 live segment (backend/scripts/demo_live.py)",
            "budget_usd": opts.budget_usd}
    return client.post("/runs", body)


def approve(client: Client, run_id: str, opts: Options) -> dict:
    """Wait for awaiting_approval, then approve every priority source (plan.json's proposal) and leave the rest
    to auto-approval, the same decisions as `cli approve` without --all."""
    deadline = time.monotonic() + opts.approve_wait_s
    while True:
        run = client.get(f"/runs/{_q(run_id)}")
        if run["status"] == "awaiting_approval":
            break
        if run["status"] in TERMINAL or time.monotonic() > deadline:
            raise CliError(409, "invalid_state", f"Run is {run['status']} at {run['stage']}, not awaiting approval")
        time.sleep(opts.poll_s)
    sources = client.get(f"/runs/{_q(run_id)}/plan")["sources"]
    decisions = decisions_for(sources, approve_all=False, reject=[])
    return client.post(f"/runs/{_q(run_id)}/approval", {"decisions": decisions,
                                                        "note": "Approved per plan.json (rehearsal)"})


def revoke_due(e: dict, opts: Options) -> bool:
    """Default: as soon as collect starts. --pacing-check: when the fetcher announces the revoke target."""
    if opts.pacing_check:
        return e["type"] == "agent.log" and e["data"]["text"].startswith(f"next: {opts.revoke_id} ")
    return e["type"] == "run.status" and e["data"]["stage"] == "collect" and e["data"]["status"] == "running"


def stop_due(e: dict) -> bool:
    """The first model call extract got through the gateway."""
    d = e["data"]
    return (e["type"] == "gateway.request" and e["stage"] == "extract" and d["action"] == "model.invoke"
            and d["decision"] == "allowed")


def _act(client: Client, path: str, body: dict) -> str | None:
    try:
        client.post(path, body)
    except CliError as err:
        return err.describe()
    return None


def drive(client: Client, run_id: str, opts: Options, say: Say) -> tuple[Actions, list[dict]]:
    """Poll the events, revoke and STOP when their moment comes, then wait for the run to settle."""
    acts = Actions(run_id=run_id)
    events: list[dict] = []
    seq = 0
    deadline = time.monotonic() + opts.timeout_s
    status = "running"
    while time.monotonic() < deadline:
        page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE})
        for e in page["events"]:
            events.append(e)
            if acts.revoke_after_seq is None and revoke_due(e, opts):
                acts.revoke_after_seq = e["seq"]
                acts.revoke_error = _act(client, f"/runs/{_q(run_id)}/sources/{_q(opts.revoke_id)}/revoke",
                                         {"reason": REVOKE_REASON})
                say(f"  revoke {opts.revoke_id} sent after seq {e['seq']}" +
                    (f": {acts.revoke_error}" if acts.revoke_error else ""))
            if acts.stop_after_seq is None and stop_due(e):
                acts.stop_after_seq = e["seq"]
                acts.stop_error = _act(client, f"/runs/{_q(run_id)}/stop", {"reason": STOP_REASON})
                say(f"  STOP sent after seq {e['seq']} (first allowed extract model call)" +
                    (f": {acts.stop_error}" if acts.stop_error else ""))
        seq = page["last_seq"]
        status = page["run_status"]
        if len(page["events"]) >= PAGE:
            continue
        if status in TERMINAL:
            break
        time.sleep(opts.poll_s)
    else:
        acts.timed_out = True
    events += settle(client, run_id, seq, opts)
    return acts, events


def settle(client: Client, run_id: str, seq: int, opts: Options) -> list[dict]:
    """After the run ends: keep reading until no agent is queued or working (in-flight calls have landed)."""
    out: list[dict] = []
    deadline = time.monotonic() + opts.settle_s
    while True:
        page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE})
        out += page["events"]
        seq = page["last_seq"]
        if len(page["events"]) >= PAGE:
            continue
        agents = client.get(f"/runs/{_q(run_id)}/agents")["agents"]
        busy = [a for a in agents if a["status"] in ("queued", "working")]
        if not busy or time.monotonic() > deadline:
            return out
        time.sleep(max(opts.poll_s, 0.5))


def check_run(client: Client, run_id: str, opts: Options) -> Rehearsal:
    """Score a run someone else drove (the presenter, with the CLI) from its events. Nothing is sent to it."""
    events: list[dict] = []
    seq = 0
    while True:
        page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE})
        events += page["events"]
        seq = page["last_seq"]
        if len(page["events"]) < PAGE:
            break
    run = client.get(f"/runs/{_q(run_id)}")
    docs = client.get(f"/runs/{_q(run_id)}/documents")["documents"]
    stopped = next((e["seq"] for e in events if e["type"] == "run.stopped"), None)
    acts = Actions(run_id=run_id, stop_after_seq=stopped)
    reh = Rehearsal(actions=acts, run=run, events=events, documents=docs)
    reh.beats = check_beats(events, docs, acts, opts)
    return reh


def rehearse(client: Client, opts: Options, say: Say = print) -> Rehearsal:
    run = create_run(client, opts)
    run_id = run["run_id"]
    say(f"Run {run_id} created via {run['created_via']} for {run['company']}")
    approve(client, run_id, opts)
    say("  approved per plan.json (priority sources approved, the rest auto-approved)")
    acts, events = drive(client, run_id, opts, say)
    run = client.get(f"/runs/{_q(run_id)}")
    docs = client.get(f"/runs/{_q(run_id)}/documents")["documents"]
    reh = Rehearsal(actions=acts, run=run, events=events, documents=docs)
    reh.beats = check_beats(events, docs, acts, opts)
    return reh


# ---------------------------------------------------------------------------
# Checking the beats (pure: events and documents in, verdicts out)
# ---------------------------------------------------------------------------


def _requests(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "gateway.request"]


def _ts(e: dict) -> float:
    return datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()


def beat_launch(events: list[dict]) -> Beat:
    created = next((e for e in events if e["type"] == "run.created"), None)
    ready = next((e for e in events if e["type"] == "plan.ready"), None)
    committed = next((e for e in events if e["type"] == "approval.committed"), None)
    via = created["data"]["run"]["created_via"] if created else None
    ok = via == "cli" and ready is not None and committed is not None
    if not ok:
        missing = [n for n, e in (("run.created", created), ("plan.ready", ready), ("approval.committed", committed))
                   if e is None]
        return Beat("launch", "Launch from the CLI, plan streams, approval", False,
                    f"created_via={via}; missing: {', '.join(missing) or 'none'}")
    d = committed["data"]
    return Beat("launch", "Launch from the CLI, plan streams, approval", True,
                f"created via cli, plan ready ({ready['data']['needs']} needs, {ready['data']['sources']} sources), "
                f"approved {len(d['approved'])}, auto-approved {len(d['auto_approved'])}, "
                f"rejected {len(d['rejected'])}")


def beat_traffic(events: list[dict]) -> Beat:
    reqs = _requests(events)
    fetches = [e["data"] for e in reqs if e["data"]["action"] == "fetch" and e["data"]["decision"] == "allowed"
               and 200 <= (e["data"]["http_status"] or 0) < 300]
    calls = [e["data"] for e in reqs if e["data"]["action"] == "model.invoke" and e["data"]["decision"] == "allowed"]
    metered = [c for c in calls if (c["tokens_in"] or 0) > 0 and (c["tokens_out"] or 0) > 0
               and (c["cost_usd"] or 0) > 0]
    ok = bool(fetches) and bool(metered)
    mb = sum(f["bytes"] or 0 for f in fetches) / 1_000_000
    tin = sum(c["tokens_in"] or 0 for c in calls)
    tout = sum(c["tokens_out"] or 0 for c in calls)
    cost = sum(c["cost_usd"] or 0 for c in calls)
    return Beat("traffic", "Every fetch and model call, with tokens and cost", ok,
                f"{len(fetches)} fetch(es) allowed ({mb:.1f} MB), {len(calls)} model call(s) allowed "
                f"({tin:,} in / {tout:,} out, ${cost:.4f}); metered calls: {len(metered)}")


def beat_link_blocked(events: list[dict], link_from: str) -> Beat:
    """The first request after the fetcher announced a link from link_from: blocked by default deny."""
    title = f"A link found in {link_from} is blocked by default deny"
    announced = [e for e in events if e["type"] == "agent.log"
                 and e["data"]["text"].startswith(f"next: link found in {link_from},")]
    for log in announced:
        req = next((e for e in _requests(events) if e["seq"] > log["seq"] and e["data"]["action"] == "fetch"
                    and e["data"]["source_id"] is None), None)
        if req is None:
            continue
        d = req["data"]
        if d["decision"] == "blocked" and d["rule_id"] is None and "Default deny" in d["reason"]:
            return Beat("link_blocked", title, True, f"{d['target']}: {d['reason']}")
    links = [e["data"] for e in _requests(events) if e["data"]["reason"].startswith("link found in page")]
    if not announced:
        why = f"{link_from} requested no links (was it fetched and stored?)"
    else:
        why = "its link requests were not blocked by default deny: " + "; ".join(
            f"{x['target']} {x['decision']} ({x['rule_id']})" for x in links[:3])
    return Beat("link_blocked", title, False, why)


def beat_revoke(events: list[dict], sid: str, acts: Actions) -> Beat:
    rule = f"R-revoke-{sid}"
    title = f"Revoke {sid} mid-run: the next request to it is blocked"
    if acts.revoke_error:
        return Beat("revoke", title, False, f"the revoke call failed: {acts.revoke_error}")
    pol = next((e for e in events if e["type"] == "policy.updated" and rule in e["data"]["rule_ids"]), None)
    if pol is None:
        return Beat("revoke", title, False, f"no policy.updated with {rule}")
    reqs = [e for e in _requests(events) if e["data"]["source_id"] == sid]
    before = [e for e in reqs if e["seq"] < pol["seq"] and e["data"]["decision"] == "allowed"]
    after = [e for e in reqs if e["seq"] > pol["seq"]]
    if not after:
        why = "it was fetched before the revoke landed" if before else "collect never requested it after the revoke"
        return Beat("revoke", title, False, f"no {sid} request after policy v{pol['data']['version']}: {why}")
    first = after[0]["data"]
    allowed_after = [e for e in after if e["data"]["decision"] == "allowed"]
    ok = first["decision"] == "blocked" and first["rule_id"] == rule and not allowed_after
    return Beat("revoke", title, ok,
                f"policy v{pol['data']['version']} at seq {pol['seq']}, then {first['target']} "
                f"{first['decision']} by {first['rule_id']}: {first['reason']}"
                + (f"; {len(allowed_after)} allowed afterwards" if allowed_after else ""))


def beat_flagged(events: list[dict], documents: list[dict], sid: str) -> Beat:
    title = f"{sid} (our planted test page, simulated) flagged and quarantined"
    flagged = next((e["data"] for e in _requests(events)
                    if e["data"]["decision"] == "flagged" and e["data"]["source_id"] == sid), None)
    stored = next((e["data"] for e in events if e["type"] == "doc.stored" and e["data"]["source_id"] == sid), None)
    doc = next((d for d in documents if d["source_id"] == sid), None)
    ok = (flagged is not None and stored is not None and stored["scan"]["verdict"] == "flagged"
          and doc is not None and doc["quarantined"] is True)
    if ok:
        return Beat("flagged", title, True, f"{flagged['target']} flagged ({flagged['reason']}); "
                                            f"{doc['doc_id']} quarantined, scan {doc['scan']['verdict']}")
    parts = [f"flagged request: {'yes' if flagged else 'no'}",
             f"doc.stored scan: {stored['scan']['verdict'] if stored else 'none'}",
             f"document quarantined: {doc['quarantined'] if doc else 'no document'}"]
    return Beat("flagged", title, False, "; ".join(parts))


def beat_stop(events: list[dict], acts: Actions) -> Beat:
    title = "STOP: the kill switch blocks the next model call, nothing is allowed afterwards"
    if acts.stop_error:
        return Beat("stop", title, False, f"the stop call failed: {acts.stop_error}")
    stopped = next((e for e in events if e["type"] == "run.stopped"), None)
    if stopped is None:
        why = "STOP was never sent: extract made no allowed model call" if acts.stop_after_seq is None else \
            "no run.stopped event"
        return Beat("stop", title, False, why)
    after = [e["data"] for e in _requests(events) if e["seq"] > stopped["seq"]]
    blocked_calls = [d for d in after if d["action"] == "model.invoke" and d["decision"] == "blocked"]
    allowed = [d for d in after if d["decision"] == "allowed"]
    ok = bool(blocked_calls) and not allowed
    if not blocked_calls:
        return Beat("stop", title, False, "no model call reached the gateway after STOP (nothing was in flight)"
                    + (f"; {len(allowed)} allowed afterwards" if allowed else ""))
    first = blocked_calls[0]
    return Beat("stop", title, ok, f"{len(blocked_calls)} model call(s) blocked after STOP, first: {first['reason']}"
                + (f"; {len(allowed)} request(s) allowed afterwards" if allowed else "; 0 allowed afterwards"))


def beat_pacing(events: list[dict], sid: str, min_gap_s: float) -> Beat:
    """--pacing-check: the presenter saw "next: S-006", revoked, and the very request it announced was blocked."""
    title = f"Pacing: time to revoke between 'next: {sid}' and its request (at least {min_gap_s:g} s)"
    log = next((e for e in events if e["type"] == "agent.log" and e["data"]["text"].startswith(f"next: {sid} ")),
               None)
    if log is None:
        return Beat("pacing", title, False, f"the fetcher never announced {sid}")
    req = next((e for e in _requests(events) if e["seq"] > log["seq"] and e["data"]["source_id"] == sid), None)
    if req is None:
        return Beat("pacing", title, False, f"no {sid} request after its announcement")
    gap = _ts(req) - _ts(log)
    revoked_between = any(e["type"] == "policy.updated" and f"R-revoke-{sid}" in e["data"]["rule_ids"]
                          and log["seq"] < e["seq"] < req["seq"] for e in events)
    start = next((e for e in events if e["type"] == "run.status" and e["data"]["stage"] == "collect"), None)
    window = f", {(_ts(log) - _ts(start)):.1f} s into collect" if start else ""
    ok = gap >= min_gap_s and revoked_between
    return Beat("pacing", title, ok, f"announced{window}, requested {gap:.1f} s later; revoke landed in between: "
                                     f"{'yes' if revoked_between else 'no'}")


def check_beats(events: list[dict], documents: list[dict], acts: Actions, opts: Options) -> list[Beat]:
    beats = [beat_launch(events), beat_traffic(events), beat_link_blocked(events, opts.link_from),
             beat_revoke(events, opts.revoke_id, acts), beat_flagged(events, documents, opts.planted_id),
             beat_stop(events, acts)]
    if opts.pacing_check:
        beats.append(beat_pacing(events, opts.revoke_id, opts.min_gap_s))
    return beats


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def presenter_commands(api: str, run_id: str = "<run_id>", revoke_id: str = REVOKE_ID) -> list[str]:
    """The exact commands for each beat, as they go in backend/DEMO.md."""
    return [
        f"# once per terminal (PowerShell: $env:AXELROD_API = \"{api}\")",
        f"export AXELROD_API={api}",
        "# 1 Launch: the run shows up in the UI and the plan streams in",
        f"{CLI} run \"{COMPANY}\" --ticker {TICKER}",
        f"{CLI} tail {run_id}            # second terminal; add --all to also see the fetcher's 'next: S-00x' lines",
        "#   approve in the UI (priority sources, drag one up, reject one), or from the CLI:",
        f"{CLI} approve {run_id} --all",
        "# 2 Traffic: nothing to type, the tail shows every fetch and model call with tokens and cost",
        "# 3 Block: S-009's links are blocked by default deny on their own (red rows), then, while collect runs:",
        f"{CLI} revoke {run_id} {revoke_id} --reason \"Analyst revoked mid-run\"",
        "#   S-010 (our planted test page, simulated) comes back FLAGGED and its document is quarantined",
        "# 4 Stop: once extract shows its first model calls",
        f"{CLI} stop {run_id} --reason \"Presenter pressed STOP\"",
        f"{CLI} status {run_id}",
    ]


def report(reh: Rehearsal, api: str, say: Say = print) -> None:
    run = reh.run
    say("")
    for b in reh.beats:
        say(f"{'PASS' if b.ok else 'FAIL'}  {b.key:<13} {b.title}")
        say(f"      {b.detail}")
    u = run["usage"]
    say("")
    say(f"Run {run['run_id']}: {run['status']} at {run['stage']}, {u['model_calls']} model calls, "
        f"${u['cost_usd']:.4f} spent" + (f" of ${u['budget_usd']:.2f}" if u.get("budget_usd") is not None else ""))
    if reh.actions.timed_out:
        say("The rehearsal timed out before the run ended.")
    say("")
    say("Presenter commands (for backend/DEMO.md):")
    for line in presenter_commands(api, run["run_id"]):
        say("  " + line)
    say("")
    say("RESULT " + ("PASS" if reh.passed else "FAIL"))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--api", default=None, help="API base (root or /api/v1). Default: AXELROD_API, then localhost:8000")
    p.add_argument("--pacing-check", action="store_true",
                   help="Revoke only when the fetcher announces S-006, and check the presenter had time")
    p.add_argument("--min-gap", type=float, default=1.0, help="--pacing-check: seconds needed (default 1.0)")
    p.add_argument("--budget", type=float, default=0.5, help="Run budget in USD (default 0.5)")
    p.add_argument("--timeout", type=float, default=900.0, help="Seconds for the whole rehearsal (default 900)")
    p.add_argument("--revoke", default=REVOKE_ID, help=f"Source to revoke (default {REVOKE_ID})")
    p.add_argument("--check", metavar="RUN_ID", default=None,
                   help="Only score this existing run (driven by hand with the CLI). Sends nothing to it")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base = api_base(args.api)
    opts = Options(pacing_check=args.pacing_check, min_gap_s=args.min_gap, budget_usd=args.budget,
                   timeout_s=args.timeout, revoke_id=args.revoke.strip().upper())
    client = Client(base, api_key(), timeout=60.0)
    what = f"Scoring {args.check}" if args.check else "Rehearsing the A10 live segment"
    print(f"{what} against {base}")
    try:
        reh = check_run(client, args.check, opts) if args.check else rehearse(client, opts)
    except CliError as e:
        print(f"FAIL  {e.describe()}")
        return 1
    report(reh, base)
    return 0 if reh.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
