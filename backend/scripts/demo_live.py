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

--webapp (G6b, PLAN.md "Demo script: 3 minutes, webapp first", 0:25 to 1:20) makes the webapp's calls in the
webapp's order instead, and checks the new beats too:
  PY backend/scripts/demo_live.py --webapp --api <url> [--pacing-check] [--pacing 2] [--approve-delay 5]
  PY backend/scripts/demo_live.py --webapp --api <url> --check <run_id>   # score a run driven in the webapp
  1. Launch: create the run (created_via "ui"), t0 at the create call. Bobby spawns first, the plan streams.
  2. Approval like the presenter: every priority source approved with its new rank (S-002 dragged one place up,
     above S-001), S-016 rejected. S-006, S-009 and S-010 are never touched (the script refuses).
  3. Traffic and cache: every fetch is a gateway.request, cached copies say cache "hit" and cached_at.
  4. Block: as above (S-009's links, revoke S-006, S-010 flagged).
  5. Stop one goon once extract made its first allowed model call: its next call is blocked by R-stop-<id>
     while the other goons keep getting allowed calls ("not observed" when it made no call before STOP).
  6. STOP --stop-gap seconds later (default 3). t0 to STOP must stay within --max-seconds (default 55).
Then the beat times against the plan, a suggested DEMO_PACING for STOP at about 50 s, and the webapp steps.
Extract does not overlap collect: it is the next pipeline stage and starts once every source is collected, so
the goon stop always comes after S-010.

The API it talks to decides the rest: run it with live Bedrock, GUARDRAIL_ID set, DEMO_PACING (2 to 3 s) and
TEST_PAGE_URL pointing at that same API's /test-pages/injection.html (with --webapp also CACHE_DOCUMENTS=on).
A STOP early in extract keeps the spend to a few cents. Like the CLI, it only talks to Axelrod's own API
(stdlib urllib through cli.Client).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

from backend.axelrod.cli import Client, CliError, api_base, api_key, decisions_for  # noqa: E402
from backend.axelrod.verify.numbers import half_up  # noqa: E402

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

# --webapp ------------------------------------------------------------------------------------------
WEBAPP_VIA = "ui"            # CreateRunRequest.created_via of the webapp (contract/models.py)
BOBBY = "bobby"
MOVE_UP_ID = "S-002"         # dragged one place up, above S-001: only the first two fetches swap
# Rejected on stage: S-016 (Philips Annual Report 2023, peer) is one extract page, Philips's balance sheets at
# Dec 31, 2023 and 2022 for its average TICAP. The finished run on plan v2 (run_20261108_5zqdvb) got 0 data
# items from it, and S-008 (Philips's SEC XBRL facts, FY2020 to FY2025) carries those balances anyway, so the
# finished report loses nothing. It is the last priority source, so the rest of the fetch order is unchanged.
REJECT_ID = "S-016"
PROTECTED = (REVOKE_ID, LINK_FROM, PLANTED_ID)   # the block beat needs these three exactly as planned
GOON_STOP_REASON = "Demo: the presenter stopped one goon"
APPROVAL_NOTE = "Approved like the presenter: priority sources, one moved up, one rejected (rehearsal)"
TARGET_STOP_S = 50.0
SAFETY_STOP_S = 30.0         # STOP anyway this long past --max-seconds when extract never made a call
PLAN_FILE = Path(__file__).resolve().parents[1] / "data" / "shl" / "plan.json"
# (key, label, planned seconds from t0, the create call). Tuned so STOP lands at about 50 s, inside the 55 s
# PLAN.md gives the live run (0:25 to 1:20). {r} {l} {p} are the revoke, link and planted source ids.
PLANNED: list[tuple[str, str, float]] = [
    ("launch", "run created, Bobby spawns", 0), ("plan", "plan streamed (plan.ready)", 5),
    ("bobby", "Bobby's plan-gate call", 9), ("approve", "approval committed", 15),
    ("first_fetch", "first fetch", 16), ("revoke", "{r} revoked", 20), ("s006_blocked", "{r}'s request blocked", 31),
    ("links_blocked", "{l}'s links blocked", 34), ("flagged", "{p} flagged and quarantined", 37),
    ("extract", "first extractor call allowed", 44), ("goon", "one goon stopped", 46),
    ("goon_blocked", "the goon's next call blocked", 47), ("stop", "STOP", 50),
]


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
    # --webapp only
    webapp: bool = False
    created_via: str = WEBAPP_VIA
    move_up: str = MOVE_UP_ID
    reject: str = REJECT_ID
    approve_delay_s: float = 0.0    # the presenter's clicks between "waiting for approval" and Approve
    stop_gap_s: float = 3.0         # between the goon stop and STOP
    max_seconds: float = 55.0       # t0 (the create call) to STOP
    target_stop_s: float = TARGET_STOP_S
    pacing_hint: float | None = None
    stop_agent: str | None = None   # stop this goon instead of the script's pick


@dataclass
class Actions:
    """What the rehearsal did, with the last event seq it had seen when it did it."""
    run_id: str = ""
    revoke_after_seq: int | None = None
    revoke_error: str | None = None
    stop_after_seq: int | None = None
    stop_error: str | None = None
    timed_out: bool = False
    # --webapp only
    t0: float | None = None                                  # monotonic clock at the create call
    marks: dict[str, float] = field(default_factory=dict)    # what the script did, seconds from t0
    approval_error: str | None = None
    order: tuple[str, str] | None = None                     # (moved source, the source it passed)
    goon_after_seq: int | None = None
    goon_id: str | None = None
    goon_was: str | None = None                              # its status when picked: queued or working
    goon_error: str | None = None
    stop_by_timer: bool = False


@dataclass
class Beat:
    key: str
    title: str
    ok: bool
    detail: str
    observed: bool = True       # False: nothing went wrong, but the beat never showed (it doesn't fail the run)

    @property
    def verdict(self) -> str:
        return "FAIL" if not self.ok else "PASS" if self.observed else "N/OBS"


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
    what = "the 3-minute webapp demo" if opts.webapp else "the A10 live segment"
    body = {"company": opts.company, "ticker": opts.ticker, "depth": "screen",
            "created_via": opts.created_via if opts.webapp else "cli",
            "notes": f"Rehearsal of {what} (backend/scripts/demo_live.py)", "budget_usd": opts.budget_usd}
    return client.post("/runs", body)


def wait_for_approval(client: Client, run_id: str, opts: Options) -> None:
    deadline = time.monotonic() + opts.approve_wait_s
    while True:
        run = client.get(f"/runs/{_q(run_id)}")
        if run["status"] == "awaiting_approval":
            return
        if run["status"] in TERMINAL or time.monotonic() > deadline:
            raise CliError(409, "invalid_state", f"Run is {run['status']} at {run['stage']}, not awaiting approval")
        time.sleep(opts.poll_s)


def approve(client: Client, run_id: str, opts: Options) -> dict:
    """Wait for awaiting_approval, then approve every priority source (plan.json's proposal) and leave the rest
    to auto-approval, the same decisions as `cli approve` without --all."""
    wait_for_approval(client, run_id, opts)
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


def read_events(client: Client, run_id: str) -> list[dict]:
    events: list[dict] = []
    seq = 0
    while True:
        page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE})
        events += page["events"]
        seq = page["last_seq"]
        if len(page["events"]) < PAGE:
            return events


def check_run(client: Client, run_id: str, opts: Options) -> Rehearsal:
    """Score a run someone else drove (the presenter, with the CLI or the webapp) from its events. Nothing is
    sent to it."""
    if opts.webapp:
        return check_run_webapp(client, run_id, opts)
    events = read_events(client, run_id)
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


# ---------------------------------------------------------------------------
# --webapp: the webapp's calls in the webapp's order (G6b)
# ---------------------------------------------------------------------------


def _since(t0: float | None) -> float:
    return time.monotonic() - t0 if t0 is not None else 0.0


def protected(opts: Options) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*PROTECTED, opts.revoke_id, opts.link_from, opts.planted_id)))


def validate_webapp(opts: Options) -> None:
    """Refuse choices that would touch the block beat's sources or Bobby, before anything is sent."""
    keep = protected(opts)
    for what, sid in (("reject", opts.reject), ("move up", opts.move_up)):
        if sid in keep:
            raise ValueError(f"Refusing to {what} {sid}: {', '.join(keep)} must stay as planned for the block beat")
    if opts.reject == opts.move_up:
        raise ValueError(f"Refusing to move up and reject the same source ({opts.reject})")
    if opts.stop_agent == BOBBY:
        raise ValueError("Refusing to stop Bobby: this beat stops one of his goons")


def webapp_decisions(sources: list[dict], opts: Options) -> tuple[list[dict], tuple[str, str]]:
    """The approval a presenter sends: every priority source approved with its new rank (one moved up one place;
    a drag sends the whole order) and one source rejected. Returns (decisions, (moved, passed)). Raises
    ValueError before anything is sent when the plan doesn't allow it."""
    validate_webapp(opts)
    ids = {s["source_id"] for s in sources}
    if opts.reject not in ids:
        raise ValueError(f"{opts.reject} is not in the plan, so it can't be rejected")
    pri = [s["source_id"] for s in sorted((s for s in sources if s["tier"] == "priority"),
                                          key=lambda s: (s["rank"], s["source_id"]))]
    if opts.move_up not in pri:
        raise ValueError(f"{opts.move_up} is not a priority source, so it can't be moved up")
    i = pri.index(opts.move_up)
    if i == 0:
        raise ValueError(f"{opts.move_up} is already first among the priority sources")
    passed = pri[i - 1]
    if passed == opts.reject:
        raise ValueError(f"{opts.move_up} would pass {passed}, which is rejected: the order check needs both fetched")
    pri[i - 1], pri[i] = pri[i], pri[i - 1]
    decisions = [{"source_id": sid, "approval": "rejected" if sid == opts.reject else "approved", "rank": n}
                 for n, sid in enumerate(pri, start=1)]
    if opts.reject not in pri:
        decisions.append({"source_id": opts.reject, "approval": "rejected"})
    return decisions, (opts.move_up, passed)


def plan_passed(move_up: str, plan_file: Path = PLAN_FILE) -> str | None:
    """--check: the source ranked just above move_up in plan.json (the one a one-place drag passes)."""
    try:
        sources = json.loads(plan_file.read_text(encoding="utf-8"))["sources"]
    except (OSError, ValueError, KeyError):
        return None
    pri = [s["source_id"] for s in sorted((s for s in sources if s["tier"] == "priority"),
                                          key=lambda s: (s["rank"], s["source_id"]))]
    i = pri.index(move_up) if move_up in pri else 0
    return pri[i - 1] if i > 0 else None


def approve_webapp(client: Client, run_id: str, opts: Options, acts: Actions, say: Say) -> None:
    wait_for_approval(client, run_id, opts)
    say(f"  {_since(acts.t0):5.1f} s  waiting for approval")
    if opts.approve_delay_s > 0:
        time.sleep(opts.approve_delay_s)        # the presenter's clicks
    sources = client.get(f"/runs/{_q(run_id)}/plan")["sources"]
    decisions, acts.order = webapp_decisions(sources, opts)
    try:
        client.post(f"/runs/{_q(run_id)}/approval", {"decisions": decisions, "note": APPROVAL_NOTE})
    except CliError as err:
        acts.approval_error = err.describe()
        raise
    acts.marks["approve"] = _since(acts.t0)
    moved, passed = acts.order
    say(f"  {acts.marks['approve']:5.1f} s  approval sent: {len(decisions) - 1} priority approved, {moved} moved above "
        f"{passed}, {opts.reject} rejected")


def goon_due(e: dict) -> bool:
    """Extraction has started: the first model call an extractor got through the gateway."""
    return stop_due(e) and e["data"]["role"] == "extractor"


def pick_goon(agents: list[dict], exclude: set[str]) -> list[dict]:
    """Extractors that can still call a model, best first. Queued ones in spawn order come first: they wait for
    a model slot in that order, so the first of them calls within seconds and the block shows before STOP. Then
    working ones by start time (the oldest call in flight lands first, and is discarded by R-stop)."""
    live = [a for a in agents if a["role"] == "extractor" and a.get("model_id") and a["agent_id"] not in exclude
            and a["status"] in ("queued", "working")]
    queued = [a for a in live if a["status"] == "queued"]
    working = sorted((a for a in live if a["status"] == "working"), key=lambda a: a.get("started_at") or "")
    return queued + working


def stop_goon(client: Client, run_id: str, trigger: dict, opts: Options, acts: Actions, say: Say) -> None:
    """Stop one extractor that is still working (the Agents window's Stop). A 409 means it finished between the
    list and the stop, so the next pick is tried (three at most)."""
    acts.goon_after_seq = trigger["seq"]
    if opts.stop_agent:
        cands = [{"agent_id": opts.stop_agent, "status": "chosen with --stop-agent"}]
    else:
        cands = pick_goon(client.get(f"/runs/{_q(run_id)}/agents")["agents"], {trigger.get("agent_id") or ""})
    if not cands:
        acts.goon_error = "no extractor was still queued or working"
    for a in cands[:3]:
        try:
            client.post(f"/runs/{_q(run_id)}/agents/{_q(a['agent_id'])}/stop", {"reason": GOON_STOP_REASON})
        except CliError as err:
            acts.goon_error = f"{a['agent_id']}: {err.describe()}"
            if err.status == 409:
                continue
            break
        acts.goon_id, acts.goon_was, acts.goon_error = a["agent_id"], a["status"], None
        break
    acts.marks["goon"] = _since(acts.t0)
    say(f"  {acts.marks['goon']:5.1f} s  " + (f"goon {acts.goon_id} ({acts.goon_was}) stopped" if acts.goon_id
                                             else f"goon stop failed: {acts.goon_error}"))


def send_stop(client: Client, run_id: str, seq: int, acts: Actions, say: Say, why: str) -> None:
    acts.stop_after_seq = seq
    acts.stop_error = _act(client, f"/runs/{_q(run_id)}/stop", {"reason": STOP_REASON})
    acts.marks["stop"] = _since(acts.t0)
    say(f"  {acts.marks['stop']:5.1f} s  STOP sent ({why})" + (f": {acts.stop_error}" if acts.stop_error else ""))


def drive_webapp(client: Client, run_id: str, opts: Options, acts: Actions, say: Say) -> list[dict]:
    """Poll the events like the webapp. Revoke S-006, stop one goon at the first allowed extractor call, STOP
    stop_gap_s later, printing each beat's time from t0 as it shows. Then wait for the run to settle."""
    events: list[dict] = []
    seq, shown = 0, set()
    deadline = time.monotonic() + opts.timeout_s
    goon_at: float | None = None
    while time.monotonic() < deadline:
        page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE})
        for e in page["events"]:
            events.append(e)
            if acts.revoke_after_seq is None and revoke_due(e, opts):
                acts.revoke_after_seq = e["seq"]
                acts.revoke_error = _act(client, f"/runs/{_q(run_id)}/sources/{_q(opts.revoke_id)}/revoke",
                                         {"reason": REVOKE_REASON})
                acts.marks["revoke"] = _since(acts.t0)
                say(f"  {acts.marks['revoke']:5.1f} s  revoke {opts.revoke_id} sent after seq {e['seq']}"
                    + (f": {acts.revoke_error}" if acts.revoke_error else ""))
            if goon_at is None and acts.stop_after_seq is None and goon_due(e):
                stop_goon(client, run_id, e, opts, acts, say)
                goon_at = time.monotonic()
        seq, status = page["last_seq"], page["run_status"]
        say_new_marks(events, opts, acts, shown, say)
        if acts.stop_after_seq is None and status not in TERMINAL:
            if goon_at is not None and time.monotonic() - goon_at >= opts.stop_gap_s:
                send_stop(client, run_id, seq, acts, say, f"{opts.stop_gap_s:g} s after the goon stop")
            elif goon_at is None and _since(acts.t0) > opts.max_seconds + SAFETY_STOP_S:
                acts.stop_by_timer = True
                send_stop(client, run_id, seq, acts, say, "safety timer: extract made no allowed call in time")
        if len(page["events"]) >= PAGE:
            continue
        if status in TERMINAL:
            break
        time.sleep(opts.poll_s)
    else:
        acts.timed_out = True
    events += settle(client, run_id, seq, opts)
    return events


def say_new_marks(events: list[dict], opts: Options, acts: Actions, shown: set[str], say: Say) -> None:
    """Print each beat once, the first time the events show it (server clock, seconds from run.created)."""
    tl = timeline(events, opts, acts.goon_id)
    for key, label, _ in PLANNED:
        if key in tl and key not in shown:
            shown.add(key)
            say(f"  {tl[key]:5.1f} s  [event] {_label(label, opts)}")


def rehearse_webapp(client: Client, opts: Options, say: Say = print) -> Rehearsal:
    validate_webapp(opts)
    acts = Actions(t0=time.monotonic())
    run = create_run(client, opts)
    acts.run_id = run_id = run["run_id"]
    acts.marks["launch"] = _since(acts.t0)
    say(f"Run {run_id} created via {run['created_via']} for {run['company']} (cache_mode {run.get('cache_mode')})")
    approve_webapp(client, run_id, opts, acts, say)
    events = drive_webapp(client, run_id, opts, acts, say)
    return _scored(client, run_id, events, acts, opts)


def _scored(client: Client, run_id: str, events: list[dict], acts: Actions, opts: Options) -> Rehearsal:
    run = client.get(f"/runs/{_q(run_id)}")
    docs = client.get(f"/runs/{_q(run_id)}/documents")["documents"]
    sources = client.get(f"/runs/{_q(run_id)}/plan")["sources"]
    reh = Rehearsal(actions=acts, run=run, events=events, documents=docs)
    reh.beats = check_webapp_beats(events, docs, sources, acts, opts)
    return reh


def check_run_webapp(client: Client, run_id: str, opts: Options) -> Rehearsal:
    """--webapp --check: score a run the presenter drove in the webapp. The goon is the one its R-stop rule
    names, the times come from the server's clock."""
    events = read_events(client, run_id)
    acts = Actions(run_id=run_id)
    acts.stop_after_seq = next((e["seq"] for e in events if e["type"] == "run.stopped"), None)
    acts.goon_id = stopped_goon(events)
    passed = plan_passed(opts.move_up)
    acts.order = (opts.move_up, passed) if passed else None
    return _scored(client, run_id, events, acts, opts)


def stopped_goon(events: list[dict]) -> str | None:
    """The first goon the analyst stopped: its R-stop-<agent_id> rule in a policy.updated."""
    for e in events:
        if e["type"] == "policy.updated":
            for rid in e["data"]["rule_ids"]:
                if rid.startswith("R-stop-") and rid != f"R-stop-{BOBBY}":
                    return rid[len("R-stop-"):]
    return None


# -- webapp beats (pure) ------------------------------------------------------------------


def _first(events: list[dict], pred: Callable[[dict], bool]) -> dict | None:
    return next((e for e in events if pred(e)), None)


def _label(label: str, opts: Options) -> str:
    return label.format(r=opts.revoke_id, l=opts.link_from, p=opts.planted_id)


def _link_block(events: list[dict], link_from: str) -> dict | None:
    """The first link request after link_from's first link announcement, when it was blocked."""
    log = _first(events, lambda e: e["type"] == "agent.log"
                 and e["data"]["text"].startswith(f"next: link found in {link_from},"))
    if log is None:
        return None
    req = _first(_requests(events), lambda e: e["seq"] > log["seq"] and e["data"]["action"] == "fetch"
                 and e["data"]["source_id"] is None)
    return req if req is not None and req["data"]["decision"] == "blocked" else None


def timeline(events: list[dict], opts: Options, goon_id: str | None = None) -> dict[str, float]:
    """Each beat's time on the server clock, in seconds from run.created (t0 within the create call)."""
    created = _first(events, lambda e: e["type"] == "run.created")
    if created is None:
        return {}
    reqs = _requests(events)
    revoke_rule = f"R-revoke-{opts.revoke_id}"
    goon_id = goon_id or stopped_goon(events)
    stop_rule = f"R-stop-{goon_id}" if goon_id else None
    found = {
        "launch": created,
        "plan": _first(events, lambda e: e["type"] == "plan.ready"),
        "bobby": _first(events, lambda e: e["type"] == "agent.decision" and e["data"]["agent_id"] == BOBBY),
        "approve": _first(events, lambda e: e["type"] == "approval.committed"),
        "first_fetch": _first(reqs, lambda e: e["data"]["action"] == "fetch"),
        "revoke": _first(events, lambda e: e["type"] == "policy.updated" and revoke_rule in e["data"]["rule_ids"]),
        "s006_blocked": _first(reqs, lambda e: e["data"]["source_id"] == opts.revoke_id
                               and e["data"]["rule_id"] == revoke_rule),
        "links_blocked": _link_block(events, opts.link_from),
        "flagged": _first(reqs, lambda e: e["data"]["decision"] == "flagged"
                          and e["data"]["source_id"] == opts.planted_id),
        "extract": _first(events, goon_due),
        "goon": stop_rule and _first(events, lambda e: e["type"] == "policy.updated"
                                     and stop_rule in e["data"]["rule_ids"]),
        "goon_blocked": stop_rule and _first(reqs, lambda e: e["data"]["rule_id"] == stop_rule),
        "stop": _first(events, lambda e: e["type"] == "run.stopped"),
    }
    t0 = _ts(created)
    return {k: _ts(e) - t0 for k, e in found.items() if e}


def beat_launch_webapp(events: list[dict], opts: Options) -> Beat:
    title = "Launch from the webapp: Bobby spawns first, the plan streams, Bobby's first call"
    created = _first(events, lambda e: e["type"] == "run.created")
    ready = _first(events, lambda e: e["type"] == "plan.ready")
    spawned = _first(events, lambda e: e["type"] == "agent.spawned")
    call = _first(events, lambda e: e["type"] == "agent.decision" and e["data"]["agent_id"] == BOBBY)
    via = created["data"]["run"]["created_via"] if created else None
    first_agent = spawned["data"]["agent"]["agent_id"] if spawned else None
    ok = via == opts.created_via and ready is not None and first_agent == BOBBY
    plan = f"plan ready ({ready['data']['needs']} needs, {ready['data']['sources']} sources)" if ready else \
        "no plan.ready"
    gate = (f"Bobby's {call['data']['stage']} gate: {call['data']['decision']}" if call
            else "no call from Bobby logged yet")
    return Beat("launch", title, ok, f"created via {via} (want {opts.created_via}), first agent {first_agent}, "
                                     f"{plan}, {gate}")


def _fetch_order(events: list[dict]) -> list[str]:
    """Source ids in the order collect first requested them."""
    order: list[str] = []
    for e in _requests(events):
        sid = e["data"]["source_id"]
        if e["data"]["action"] == "fetch" and sid and sid not in order:
            order.append(sid)
    return order


def beat_approval(events: list[dict], sources: list[dict], acts: Actions, opts: Options) -> Beat:
    """The approval landed as sent: priority approved, the rejection in the policy and never requested, and the
    moved source fetched before the one it passed."""
    title = f"Approval like the presenter: priority approved, {opts.move_up} moved up, {opts.reject} rejected"
    if acts.approval_error:
        return Beat("approval", title, False, f"the approval call failed: {acts.approval_error}")
    committed = _first(events, lambda e: e["type"] == "approval.committed")
    if committed is None:
        return Beat("approval", title, False, "no approval.committed")
    d = committed["data"]
    pol = _first(events, lambda e: e["type"] == "policy.updated" and e["seq"] > committed["seq"])
    problems: list[str] = []
    if opts.reject not in d["rejected"]:
        problems.append(f"{opts.reject} not rejected")
    if [x for x in d["rejected"] if x != opts.reject]:
        problems.append(f"also rejected: {', '.join(x for x in d['rejected'] if x != opts.reject)}")
    if any(x in d["rejected"] for x in protected(opts)):
        problems.append("a protected source was rejected")
    priority = [s["source_id"] for s in sources if s["tier"] == "priority" and s["source_id"] != opts.reject]
    missing = [sid for sid in priority if sid not in d["approved"]]
    if missing:
        problems.append(f"priority not approved: {', '.join(missing)}")
    rule = f"R-reject-{opts.reject}"
    if pol is None or rule not in pol["data"]["rule_ids"]:
        problems.append(f"no {rule} in the approval's policy")
    asked = [e for e in _requests(events) if e["data"]["source_id"] == opts.reject]
    if asked:
        problems.append(f"{opts.reject} was requested {len(asked)} time(s)")
    order = _fetch_order(events)
    moved, passed = acts.order or (opts.move_up, None)
    if passed is None:
        problems.append(f"don't know which source {moved} passed")
    elif moved not in order or passed not in order:
        problems.append(f"{moved} and {passed} were not both fetched")
    elif order.index(moved) > order.index(passed):
        problems.append(f"{moved} was fetched after {passed}: the new order did not land")
    detail = (f"approved {len(d['approved'])}, rejected {', '.join(d['rejected']) or 'none'}, auto-approved "
              f"{len(d['auto_approved'])}; {rule} in policy v{pol['data']['version'] if pol else '?'}; fetch order "
              f"starts {', '.join(order[:4]) or 'nothing'}")
    return Beat("approval", title, not problems, detail + ("; " + "; ".join(problems) if problems else ""))


def beat_cache(events: list[dict], documents: list[dict]) -> Beat:
    title = "Traffic from the document cache: every fetch is a gateway.request, cached copies say when"
    created = _first(events, lambda e: e["type"] == "run.created")
    mode = created["data"]["run"].get("cache_mode") if created else None
    fetches = [e for e in _requests(events) if e["data"]["action"] == "fetch" and e["data"]["decision"] == "allowed"]
    hits = [e["data"] for e in fetches if e["data"].get("cache") == "hit"]
    misses = [e["data"] for e in fetches if e["data"].get("cache") == "miss"]
    undated = [h["target"] for h in hits if not h.get("cached_at")]
    unlogged = [s["data"]["source_id"] for s in events if s["type"] == "doc.stored"
                and not any(f["seq"] < s["seq"] and f["data"]["source_id"] == s["data"]["source_id"] for f in fetches)]
    origins = Counter(d.get("origin") or "unknown" for d in documents)
    dated = sorted({h["cached_at"][:10] for h in hits if h.get("cached_at")})
    ok = bool(hits) and not undated and not unlogged
    detail = (f"cache_mode {mode}; {len(fetches)} allowed fetch(es): {len(hits)} hit, {len(misses)} miss, "
              f"{len(fetches) - len(hits) - len(misses)} with the cache off; copies collected "
              f"{', '.join(dated) or 'never'}; documents by origin: "
              + (", ".join(f"{k} {v}" for k, v in sorted(origins.items())) or "none"))
    if not hits:
        detail += "; no fetch was a cache hit (CACHE_DOCUMENTS off, or the cache is empty)"
    if undated:
        detail += f"; {len(undated)} hit(s) without cached_at"
    if unlogged:
        detail += f"; stored without an allowed fetch request: {', '.join(unlogged)}"
    return Beat("cache", title, ok, detail)


def beat_goon(events: list[dict], acts: Actions) -> Beat:
    """The stopped goon's next request is blocked by R-stop-<id>, nothing of its is allowed afterwards, and the
    other goons keep getting allowed calls until STOP. No call from it before STOP is "not observed"."""
    title = "Stop one goon: its next model call is blocked, the other goons keep going"
    gid = acts.goon_id
    if gid is None:
        why = acts.goon_error or ("extract made no allowed model call" if acts.goon_after_seq is None
                                  else "no goon was stopped")
        return Beat("goon", title, False, f"no goon stopped: {why}")
    rule = f"R-stop-{gid}"
    pol = _first(events, lambda e: e["type"] == "policy.updated" and rule in e["data"]["rule_ids"])
    if pol is None:
        return Beat("goon", title, False, f"no policy.updated with {rule}")
    status = _first(events, lambda e: e["type"] == "agent.status" and e["data"]["agent_id"] == gid
                    and e["data"]["status"] == "stopped")
    stop = _first(events, lambda e: e["type"] == "run.stopped")
    stop_seq = stop["seq"] if stop else float("inf")
    reqs = [e for e in _requests(events) if e["seq"] > pol["seq"]]
    mine = [e for e in reqs if e.get("agent_id") == gid]
    allowed_mine = [e for e in mine if e["data"]["decision"] == "allowed"]
    others = [e for e in reqs if e["seq"] < stop_seq and e.get("agent_id") not in (gid, BOBBY)
              and e["data"]["action"] == "model.invoke" and e["data"]["decision"] == "allowed"]
    was = f" ({acts.goon_was} when stopped)" if acts.goon_was else ""
    head = (f"{gid}{was} stopped at policy v{pol['data']['version']}"
            + (f", reason: {status['data'].get('reason')}" if status else ", no agent.status stopped"))
    if allowed_mine:
        return Beat("goon", title, False, f"{head}; {len(allowed_mine)} request(s) of it allowed afterwards")
    if not others:
        return Beat("goon", title, False, f"{head}; no other goon got an allowed model call before STOP")
    first = mine[0] if mine else None
    tail = f"; {len(others)} allowed call(s) from other goons before STOP"
    if first is None or first["seq"] > stop_seq:
        why = "it made no further call" if first is None else "its next call came after STOP (kill switch)"
        return Beat("goon", title, True, f"{head}; not observed: {why}{tail}", observed=False)
    d = first["data"]
    ok = d["decision"] == "blocked" and d["rule_id"] == rule
    return Beat("goon", title, ok, f"{head}; its next request {d['decision']} by {d['rule_id']}: {d['reason']}{tail}")


def beat_timing(events: list[dict], acts: Actions, opts: Options) -> Beat:
    title = f"Timing: t0 (the create call) to STOP within {opts.max_seconds:g} s"
    server = timeline(events, opts, acts.goon_id).get("stop")
    local = acts.marks.get("stop")
    used = local if local is not None else server
    if used is None:
        return Beat("timing", title, False, "STOP never landed")
    clock = "script clock" if local is not None else "server clock"
    detail = f"STOP at {used:.1f} s ({clock})" + (f", {server:.1f} s on the server clock" if
                                                   local is not None and server is not None else "")
    if acts.stop_by_timer:
        detail += "; sent by the safety timer, not after a goon stop"
    ok = used <= opts.max_seconds and not acts.stop_by_timer
    return Beat("timing", title, ok, detail + ("" if ok else f"; over the {opts.max_seconds:g} s limit"
                                                if used > opts.max_seconds else ""))


def check_webapp_beats(events: list[dict], documents: list[dict], sources: list[dict], acts: Actions,
                       opts: Options) -> list[Beat]:
    beats = [beat_launch_webapp(events, opts), beat_approval(events, sources, acts, opts), beat_traffic(events),
             beat_cache(events, documents), beat_link_blocked(events, opts.link_from),
             beat_revoke(events, opts.revoke_id, acts), beat_flagged(events, documents, opts.planted_id),
             beat_goon(events, acts), beat_stop(events, acts), beat_timing(events, acts, opts)]
    if opts.pacing_check:
        beats.append(beat_pacing(events, opts.revoke_id, opts.min_gap_s))
    return beats


# -- pacing advice --------------------------------------------------------------------------


@dataclass
class PacingAdvice:
    server: str                 # what the server reports about DEMO_PACING
    used: float | None          # the pacing the advice starts from: measured, else the --pacing hint
    basis: str
    fetches: int                # paced fetches in collect (each Gateway.fetch call sleeps DEMO_PACING once)
    per_fetch_s: float | None   # announcement to announcement
    revoke_pos: int | None      # the revoke target's place among them (1-based)
    stop_s: float | None
    suggested: float | None
    note: str = ""


def fetch_calls(events: list[dict]) -> list[dict]:
    """One gateway.request per Gateway.fetch call in collect. Redirect hops are not paced again, and a flagged
    request is the Guardrails scan of a page already fetched."""
    return [e for e in _requests(events) if e["data"]["action"] == "fetch" and e["stage"] == "collect"
            and e["data"]["decision"] != "flagged" and not e["data"]["reason"].startswith("redirect ")]


def _next_logs(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "agent.log" and e["data"]["text"].startswith("next: ")]


def measured_pacing(events: list[dict]) -> float | None:
    """DEMO_PACING as the events show it: each "next:" line to its request, minus that request's own latency
    (the gateway sleeps between the two, and latency_ms starts after the sleep). The median, so one slow
    ingest doesn't count."""
    calls = fetch_calls(events)
    gaps = []
    for log in _next_logs(events):
        req = _first(calls, lambda r: r["seq"] > log["seq"] and r.get("agent_id") == log.get("agent_id"))
        if req is not None:
            gaps.append(_ts(req) - _ts(log) - (req["data"]["latency_ms"] or 0) / 1000)
    return max(0.0, statistics.median(gaps)) if gaps else None


def advise_pacing(events: list[dict], acts: Actions, opts: Options) -> PacingAdvice:
    """A DEMO_PACING that lands STOP at about target_stop_s: every paced fetch moves STOP by the change."""
    calls = fetch_calls(events)
    measured = measured_pacing(events)
    used = measured if measured is not None else opts.pacing_hint
    basis = "measured from the events" if measured is not None else "the --pacing hint" if used is not None \
        else "nothing (no fetch was announced, and no --pacing hint)"
    order = [c["data"]["source_id"] for c in calls]
    pos = order.index(opts.revoke_id) + 1 if opts.revoke_id in order else None
    logs = _next_logs(events)
    per_fetch = (_ts(logs[-1]) - _ts(logs[0])) / (len(logs) - 1) if len(logs) > 1 else None
    stop_s = acts.marks.get("stop", timeline(events, opts, acts.goon_id).get("stop"))
    advice = PacingAdvice("unknown (no settings endpoint, and no event carries DEMO_PACING)", used, basis,
                          len(calls), per_fetch, pos, stop_s, None)
    if used is None or stop_s is None or not calls:
        advice.note = "no suggestion: " + ("STOP never landed" if stop_s is None else "nothing to measure")
        return advice
    raw = used + (opts.target_stop_s - stop_s) / len(calls)
    floor = opts.min_gap_s if opts.pacing_check else 0.0
    advice.suggested = max(raw, floor)
    if raw < floor:
        advice.note = (f"STOP can't land at {opts.target_stop_s:g} s and still leave {floor:g} s to react to "
                       f"'next: {opts.revoke_id}': cut the approval clicks or the STOP gap instead")
    req = _first(calls, lambda c: c["data"]["source_id"] == opts.revoke_id)
    if req is not None and pos is not None and not opts.pacing_check:
        window = _ts(req) - _ts(calls[0]) + (pos - 1) * (advice.suggested - used)
        advice.note = (advice.note + "; " if advice.note else "") + (
            f"the presenter then has about {half_up(max(window, 0.0), 0)} s from the first fetch to "
            f"{opts.revoke_id}'s request to click Revoke")
    return advice


# -- webapp output --------------------------------------------------------------------------


def _at(tl: dict[str, float], key: str) -> str:
    planned = next(p for k, _, p in PLANNED if k == key)
    return f"{half_up(tl[key], 0)} s" if key in tl else f"{planned:g} s (planned)"


def webapp_steps(tl: dict[str, float], opts: Options, acts: Actions, advice: PacingAdvice) -> list[str]:
    """What to click, when, and what should appear (for backend/DEMO.md). Times are this run's, else planned."""
    moved, passed = acts.order or (opts.move_up, plan_passed(opts.move_up) or "the source above it")
    pace = f"{half_up(advice.suggested, 1)}" if advice.suggested is not None else "2"
    r, l, p = opts.revoke_id, opts.link_from, opts.planted_id
    if opts.pacing_check:
        revoke = (f"When the log says 'next: {r}' (at {_at(tl, 'revoke')}), click Revoke on {r} at once: you have "
                  f"DEMO_PACING (about {pace} s). Its request turns red (R-revoke-{r}) at {_at(tl, 's006_blocked')}.")
    else:
        revoke = (f"Right after the first fetches (at {_at(tl, 'revoke')}), click Revoke on {r} in Sources. When "
                  f"collect reaches it (at {_at(tl, 's006_blocked')}) its request turns red (R-revoke-{r}).")
    goon = acts.goon_id or "a queued extractor"
    return [
        f"0 s: launch. `{CLI} run \"{COMPANY}\" --ticker {TICKER}` (or New run in the webapp; a CLI launch is "
        f"created_via cli, so score it with --created-via cli). Switch to the webapp: the run is there and Bobby "
        f"is the first agent.",
        f"{_at(tl, 'plan')}: the plan streams into Sources (frozen for the demo, say so). At {_at(tl, 'bobby')} "
        f"Bobby's plan-gate call shows in the log.",
        f"When the run says waiting for approval: approve every priority source, drag {moved} above {passed}, "
        f"reject {opts.reject}. Never touch {r}, {l} or {p}. Click Approve (committed at {_at(tl, 'approve')}).",
        f"{_at(tl, 'first_fetch')}: Traffic fills in, {moved} first. Each cached fetch says cache hit and when the "
        f"copy was collected (archive copies were collected before the event, say so).",
        revoke,
        f"{_at(tl, 'links_blocked')}: {l}'s off-site links turn red on their own (default deny, no rule).",
        f"{_at(tl, 'flagged')}: {p}, our planted test page (simulated), comes back flagged by Bedrock Guardrails "
        f"and its document is quarantined.",
        f"{_at(tl, 'extract')}: extract starts once collect is done and the first extractor calls go green. In the "
        f"Agents window, stop one goon that still shows queued near the top of the extractors (this run: {goon}). "
        f"At {_at(tl, 'goon_blocked')} its next model call turns red (R-stop-<agent id>), the other goons stay "
        f"green.",
        f"{opts.stop_gap_s:g} s later (at {_at(tl, 'stop')}): press STOP. The next model call is blocked and "
        f"nothing is allowed after it. The run shows stopped.",
    ]


def report_webapp(reh: Rehearsal, api: str, opts: Options, say: Say = print) -> None:
    run, acts = reh.run, reh.actions
    tl = timeline(reh.events, opts, acts.goon_id)
    advice = advise_pacing(reh.events, acts, opts)
    say("")
    for b in reh.beats:
        say(f"{b.verdict:<5} {b.key:<13} {b.title}")
        say(f"      {b.detail}")
    say("")
    say("Timing, seconds from t0 (the create call), server clock. Read it against the stopwatch:")
    say(f"  {'beat':<34} {'planned':>7} {'actual':>7}")
    for key, label, planned in PLANNED:
        actual = f"{tl[key]:.1f}" if key in tl else "-"
        say(f"  {_label(label, opts):<34} {planned:>7g} {actual:>7}")
    if "stop" in acts.marks:
        say(f"  {'STOP sent (script clock)':<34} {'':>7} {acts.marks['stop']:>7.1f}")
    say("")
    hint = f"{opts.pacing_hint:g} s" if opts.pacing_hint is not None else "none"
    used = f"{half_up(advice.used, 2)} s" if advice.used is not None else "unknown"
    say(f"DEMO_PACING on the server: {advice.server}; --pacing hint: {hint}; used for the advice: {used} "
        f"({advice.basis})")
    per = f", {half_up(advice.per_fetch_s, 1)} s per fetch" if advice.per_fetch_s is not None else ""
    pos = f", {opts.revoke_id} is fetch {advice.revoke_pos}" if advice.revoke_pos else f", {opts.revoke_id} not fetched"
    say(f"Fetch order: {advice.fetches} paced fetch(es) in collect{pos}{per}")
    if advice.suggested is not None:
        say(f"Suggested DEMO_PACING for STOP at about {opts.target_stop_s:g} s: {half_up(advice.suggested, 1)}"
            + (f" ({advice.note})" if advice.note else ""))
    else:
        say(f"Suggested DEMO_PACING: {advice.note}")
    u = run["usage"]
    say("")
    say(f"Run {run['run_id']}: {run['status']} at {run['stage']}, {u['model_calls']} model calls, "
        f"${u['cost_usd']:.4f} spent" + (f" of ${u['budget_usd']:.2f}" if u.get("budget_usd") is not None else ""))
    if acts.timed_out:
        say("The rehearsal timed out before the run ended.")
    say("")
    say("Webapp steps (for backend/DEMO.md):")
    for n, line in enumerate(webapp_steps(tl, opts, acts, advice), start=1):
        say(f"  {n}. {line}")
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
                   help="Only score this existing run (driven by hand with the CLI or the webapp). Sends nothing to it")
    w = p.add_argument_group("--webapp (the 3-minute webapp demo, G6b)")
    w.add_argument("--webapp", action="store_true", help="Make the webapp's calls in the webapp's order")
    w.add_argument("--created-via", choices=("ui", "cli"), default=WEBAPP_VIA,
                   help="created_via of the run (default ui, the webapp; cli when the presenter launches from it)")
    w.add_argument("--move-up", default=MOVE_UP_ID, help=f"Priority source dragged one place up (default {MOVE_UP_ID})")
    w.add_argument("--reject", default=REJECT_ID, help=f"Source rejected at approval (default {REJECT_ID})")
    w.add_argument("--approve-delay", type=float, default=5.0,
                   help="Seconds of presenter clicks before Approve (default 5)")
    w.add_argument("--stop-gap", type=float, default=3.0, help="Seconds between the goon stop and STOP (default 3)")
    w.add_argument("--max-seconds", type=float, default=55.0, help="Limit for t0 to STOP (default 55)")
    w.add_argument("--target-stop", type=float, default=TARGET_STOP_S,
                   help=f"Where the suggested DEMO_PACING lands STOP (default {TARGET_STOP_S:g} s)")
    w.add_argument("--pacing", type=float, default=None, help="DEMO_PACING the server runs with, if you know it")
    w.add_argument("--stop-agent", default=None, help="Stop this goon instead of the script's pick")
    return p


def _sid(s: str) -> str:
    return s.strip().upper()


def _options(args: argparse.Namespace) -> Options:
    up = _sid
    return Options(pacing_check=args.pacing_check, min_gap_s=args.min_gap, budget_usd=args.budget,
                   timeout_s=args.timeout, revoke_id=up(args.revoke), webapp=args.webapp,
                   created_via=args.created_via, move_up=up(args.move_up), reject=up(args.reject),
                   approve_delay_s=args.approve_delay, stop_gap_s=args.stop_gap, max_seconds=args.max_seconds,
                   target_stop_s=args.target_stop, pacing_hint=args.pacing,
                   stop_agent=(args.stop_agent or "").strip() or None)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base = api_base(args.api)
    opts = _options(args)
    if opts.webapp:
        try:
            validate_webapp(opts)
        except ValueError as e:
            print(f"REFUSED  {e}")
            return 2
    client = Client(base, api_key(), timeout=60.0)
    what = f"Scoring {args.check}" if args.check else \
        "Rehearsing the 3-minute webapp demo" if opts.webapp else "Rehearsing the A10 live segment"
    print(f"{what} against {base}")
    try:
        if args.check:
            reh = check_run(client, args.check, opts)
        else:
            reh = rehearse_webapp(client, opts) if opts.webapp else rehearse(client, opts)
    except CliError as e:
        print(f"FAIL  {e.describe()}")
        return 1
    except ValueError as e:        # the plan doesn't allow the presenter's approval
        print(f"REFUSED  {e}")
        return 2
    if opts.webapp:
        report_webapp(reh, base, opts)
    else:
        report(reh, base)
    return 0 if reh.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
