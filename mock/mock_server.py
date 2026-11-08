"""
Axelrod mock API: the full contract, served from fixtures, with a live replay.

    pip install -r mock/requirements.txt
    uvicorn mock.mock_server:app --reload --port 8000      (from the repo root)
    open http://localhost:8000/docs                          (Swagger UI)

What it does
- GET /api/v1/runs lists a finished fixture run (run_20261108_fx0001) so every
  panel has data immediately.
- POST /api/v1/runs starts a replay of the same story in real time: planning
  (about 8 s), then it waits at approval until you POST /approval, then about
  30 s of collection, extraction, verification, repair and the brief.
- Stop, revoke, comments, user edits and demo error injection all work and
  emit the same events the real backend will.
- Every response is validated against contract/models.py, so if the mock
  answers, the shape is right.

MOCK_SPEED=0.5 replays at half speed, MOCK_SPEED=3 at triple speed.

Limits: the replay is a fixed story. Approving or rejecting changes the plan,
the policy and the traffic (rejected or revoked sources show as blocked), but
conclusions are not recomputed. The fixture company and every number in it
are fictional.
"""
from __future__ import annotations

import copy
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "contract"))
sys.path.insert(0, str(HERE))
import build_fixtures as bf  # noqa: E402
import models as m  # noqa: E402

CONTRACT_VERSION = (HERE.parent / "contract" / "VERSION").read_text().strip() if (HERE.parent / "contract" / "VERSION").exists() else "0.1.0"
SPEED = float(os.environ.get("MOCK_SPEED", "1.0"))
PREFIX = "/api/v1"
FX = bf.build()

app = FastAPI(title="Axelrod mock API", version=CONTRACT_VERSION,
              description="Mock of the Axelrod contract. Fixture data, fictional company.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Errors in the contract's envelope
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse(status_code=e.status, content={"error": {"code": e.code, "message": e.message, "details": e.details}})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Request body or query is invalid",
                                                            "details": {"errors": e.errors()}}})


def now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

FOLDER_BY_KIND = {
    "sec_filing": "01-Annual-Reports", "xbrl_facts": "01-Annual-Reports", "annual_report": "01-Annual-Reports",
    "company_ir": "03-Other-Company-Reports", "company_site": "11-Technical-Explainers", "product_page": "11-Technical-Explainers",
    "news": "04-News-Coverage", "gov": "05-Regulatory-and-Legal", "market_report": "10-Demand-Evidence",
    "forum": "10-Demand-Evidence", "code_repo": "11-Technical-Explainers", "data_api": "00-Meta", "other": "00-Meta",
}


class RunState:
    def __init__(self, run_id: str, company: str, ticker: Optional[str], depth: str, created_via: str,
                 notes: Optional[str], budget_usd: Optional[float], created_at: str, mode: str):
        self.run_id, self.company, self.ticker, self.depth = run_id, company, ticker, depth
        self.created_via, self.notes, self.mode = created_via, notes, mode
        self.budget_usd = budget_usd if budget_usd is not None else 5.0
        self.created_at = created_at
        self.created_mono = time.monotonic()
        self.status, self.stage = "running", "plan"
        self.scheduled: list[tuple[float, int, dict]] = []  # (release_mono, order, event)
        self.order = 0
        self.released: list[dict] = []
        self.stopped_mono: Optional[float] = None
        self.stop_reason: Optional[str] = None
        self.error: Optional[str] = None
        self.policy_version = 0
        self.decisions: Optional[list[dict]] = None
        self.revoked: dict[str, str] = {}
        self.rejected: set[str] = set()
        self.user_sources: list[dict] = []
        # folded view
        self.needs: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.docs: set[str] = set()
        self.agents: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.item_order: list[str] = []
        self.sentences: dict[str, dict] = {}
        self.sentence_section: dict[str, str] = {}
        self.report_ready = False
        self.summary_ready = False
        self.usage = {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "budget_usd": self.budget_usd}
        self.gw = {"allowed": 0, "blocked": 0, "flagged": 0}
        self.comments: dict[str, dict] = {}
        self.updated_at = created_at

    # -- scheduling ----------------------------------------------------------
    def schedule(self, delay_s: float, event: dict, base: Optional[float] = None):
        self.order += 1
        self.scheduled.append(((base if base is not None else time.monotonic()) + delay_s, self.order, event))

    def schedule_timeline(self, events: list[dict], base: float):
        for e in events:
            self.schedule(e["t"] / 1000 / SPEED, {k: v for k, v in e.items() if k != "t"}, base=base)

    def tick(self):
        now = time.monotonic()
        due = sorted([s for s in self.scheduled if s[0] <= now], key=lambda s: (s[0], s[1]))
        self.scheduled = [s for s in self.scheduled if s[0] > now]
        for release, _, e in due:
            if self.stopped_mono is not None and release > self.stopped_mono and not e.get("_after_stop"):
                continue
            e = self.transform(e)
            if e is None:
                continue
            e = {k: v for k, v in e.items() if not k.startswith("_")}
            stamped = {"seq": len(self.released) + 1, "ts": now_iso(release - now), "run_id": self.run_id, **e}
            self.released.append(stamped)
            self.apply(stamped)
            self.updated_at = stamped["ts"]

    # -- rejected/revoked sources show up as blocked traffic ---------------------
    def transform(self, e: dict) -> Optional[dict]:
        sid = (e.get("data") or {}).get("source_id")
        if not sid:
            if e["type"] in ("item.created", "item.updated"):
                sid = e["data"]["item"].get("source_id")
            elif e["type"] == "item.verified":
                it = self.items.get(e["data"]["item_id"])
                sid = it.get("source_id") if it else None
        blocked_by = None
        if sid in self.rejected:
            blocked_by = (f"R-reject-{sid}", "Rejected at approval")
        elif sid in self.revoked:
            blocked_by = (f"R-revoke-{sid}", f"Revoked by user: {self.revoked[sid] or 'no reason given'}")
        if not blocked_by or e.get("_user"):
            return e
        if e["type"] == "gateway.request" and e["data"]["action"] == "fetch":
            e = copy.deepcopy(e)
            e["data"].update({"decision": "blocked", "rule_id": blocked_by[0], "reason": blocked_by[1], "http_status": None,
                              "bytes": None, "latency_ms": 35})
            e["message"] = f"{e['agent_id']} fetch {e['data']['target']} blocked ({blocked_by[0]})"
            e["level"] = "warn"
            return e
        if e["type"] in ("doc.stored", "source.status", "item.created", "item.updated", "item.verified"):
            return None
        return e

    # -- fold events into snapshots ---------------------------------------------
    def apply(self, e: dict):
        t, d = e["type"], e["data"]
        if t == "run.status":
            self.status, self.stage = d["status"], d["stage"]
        elif t == "run.stopped":
            self.status = "stopped"
            for a in self.agents.values():
                if a["status"] in ("working", "queued"):
                    a["status"], a["finished_at"] = "stopped", e["ts"]
        elif t == "run.completed":
            self.status, self.stage = "done", "complete"
        elif t == "plan.need":
            self.needs[d["need"]["need_id"]] = copy.deepcopy(d["need"])
        elif t == "plan.source":
            s = copy.deepcopy(d["source"])
            if self.decisions is not None and s["source_id"] in self.sources:
                return
            self.sources[s["source_id"]] = s
        elif t == "source.status":
            s = self.sources.get(d["source_id"])
            if s:
                s["status"], s["stats"], s["error"] = d["status"], d["stats"], d["error"]
        elif t == "doc.stored":
            self.docs.add(d["doc_id"])
            s = self.sources.get(d["source_id"])
            if s and d["doc_id"] not in s["doc_ids"]:
                s["doc_ids"].append(d["doc_id"])
        elif t == "agent.spawned":
            a = copy.deepcopy(d["agent"])
            a["started_at"] = a["started_at"] or e["ts"]
            self.agents[a["agent_id"]] = a
        elif t == "agent.status":
            a = self.agents.get(d["agent_id"])
            if a:
                a["status"], a["task"], a["usage"] = d["status"], d["task"], d["usage"]
                if d["status"] in ("done", "failed", "stopped"):
                    a["finished_at"] = e["ts"]
        elif t == "gateway.request":
            self.gw[d["decision"]] += 1
        elif t in ("item.created", "item.updated"):
            it = copy.deepcopy(d["item"])
            if it["id"] not in self.items:
                self.item_order.append(it["id"])
            self.items[it["id"]] = it
        elif t == "item.verified":
            it = self.items.get(d["item_id"])
            if it:
                it["verification"] = copy.deepcopy(d["verification"])
                it["certainty"] = copy.deepcopy(d.get("certainty"))
        elif t == "report.ready":
            self.report_ready = True
        elif t == "summary.sentence":
            s = copy.deepcopy(d["sentence"])
            self.sentences[s["id"]] = s
            self.sentence_section[s["id"]] = d["section_heading"]
        elif t == "summary.verified":
            s = self.sentences.get(d["sentence_id"])
            if s:
                s["verification"] = copy.deepcopy(d["verification"])
                s["certainty"] = copy.deepcopy(d.get("certainty"))
        elif t == "summary.ready":
            self.summary_ready = True
        elif t == "usage.updated":
            self.usage = {**d["usage"], "budget_usd": self.budget_usd}
        elif t in ("comment.created", "comment.resolved"):
            self.comments[d["comment"]["comment_id"]] = copy.deepcopy(d["comment"])

    # -- snapshots ------------------------------------------------------------------
    @property
    def last_seq(self) -> int:
        return len(self.released)

    def run(self) -> dict:
        items = list(self.items.values())
        st = [i["verification"]["status"] for i in items]
        return {
            "run_id": self.run_id, "company": self.company, "ticker": self.ticker, "depth": self.depth,
            "status": self.status, "stage": self.stage, "created_at": self.created_at, "updated_at": self.updated_at,
            "created_via": self.created_via, "notes": self.notes, "policy_version": self.policy_version,
            "counts": {
                "sources_total": len(self.sources),
                "sources_approved": sum(s["approval"] in ("approved", "auto_approved") for s in self.sources.values()),
                "sources_fetched": sum(s["status"] in ("fetched", "extracting", "extracted", "quarantined") for s in self.sources.values()),
                "docs": len(self.docs), "agents_total": len(self.agents),
                "agents_active": sum(a["status"] == "working" for a in self.agents.values()),
                "items_total": len(items), "items_pass": st.count("pass"), "items_fixed": st.count("fixed"),
                "items_fail": st.count("fail"), "items_pending": st.count("pending"),
                "gateway_allowed": self.gw["allowed"], "gateway_blocked": self.gw["blocked"], "gateway_flagged": self.gw["flagged"],
            },
            "usage": self.usage, "last_seq": self.last_seq, "error": self.error, "stop_reason": self.stop_reason,
            "mode": self.mode,
        }

    def sorted_sources(self) -> list[dict]:
        tier_order = {"priority": 0, "tier_2": 1, "tier_3": 2}
        return sorted(self.sources.values(), key=lambda s: (tier_order[s["tier"]], s["rank"], s["source_id"]))

    def policy(self) -> dict:
        return bf.build_policy(self.run_id, self.sorted_sources() if self.decisions is not None else [], self.policy_version,
                               revoked=self.revoked, kill_switch=self.status == "stopped", budget_usd=self.budget_usd,
                               as_of_seq=self.last_seq, at=self.updated_at)


RUNS: dict[str, RunState] = {}


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(3)}"


def start_replay(r: RunState, base: float):
    r.schedule(0, {"type": "run.created", "stage": "plan", "agent_id": None, "level": "info",
                   "message": f"Run created for {r.company}", "data": {"run": r.run()}}, base=base)
    r.schedule_timeline(FX["timeline_pre"], base=base + 0.05)


def approval_events(r: RunState, approved, rejected, auto) -> list[tuple[float, dict]]:
    return [
        (0.0, {"type": "approval.committed", "stage": "approve", "agent_id": None, "level": "info",
               "message": f"Approved {len(approved)}, rejected {len(rejected)}, auto-approved {len(auto)}",
               "data": {"approved": approved, "rejected": rejected, "auto_approved": auto}}),
        (0.01, {"type": "policy.updated", "stage": "approve", "agent_id": None, "level": "info",
                "message": f"Gateway policy v{r.policy_version}: allowlist built from approved sources, default deny",
                "data": {"version": r.policy_version, "change": "Approval committed",
                         "rule_ids": [x["rule_id"] for x in r.policy()["rules"]]}}),
    ]


def commit_approval(r: RunState, decisions: list[dict], base: float):
    srcs = list(r.sources.values())
    approved, rejected, auto = bf.apply_decisions(srcs, decisions)
    for u in r.user_sources:
        if u["approval"] == "approved" and u["source_id"] not in approved:
            approved.append(u["source_id"])
            r.sources[u["source_id"]]["status"] = "queued"
    r.decisions, r.rejected = decisions, set(rejected)
    r.policy_version += 1
    for delay, ev in approval_events(r, approved, rejected, auto):
        r.schedule(delay, ev, base=base)
    r.schedule_timeline(FX["timeline_post"], base=base + 0.1)
    for i, s in enumerate(u for u in r.user_sources if u["approval"] == "approved"):
        sid = s["source_id"]
        r.schedule(3.0 / SPEED + i * 0.3, {"type": "gateway.request", "stage": "collect", "agent_id": "fetcher-01", "level": "info",
                                           "message": f"fetcher-01 fetch {s['url']} allowed", "_user": True,
                                           "data": {"request_id": f"REQ-U{i + 1:03d}", "role": "fetcher", "action": "fetch",
                                                    "target": s["url"], "decision": "allowed", "rule_id": f"R-allow-{sid}",
                                                    "reason": "Added and approved by the analyst", "source_id": sid,
                                                    "latency_ms": 380, "http_status": 200, "bytes": 18_204, "tokens_in": None,
                                                    "tokens_out": None, "cost_usd": None}}, base=base)
        r.schedule(3.2 / SPEED + i * 0.3, {"type": "source.status", "stage": "collect", "agent_id": "fetcher-01", "level": "info",
                                           "message": f"{sid} fetched (mock: no items extracted)", "_user": True,
                                           "data": {"source_id": sid, "status": "fetched",
                                                    "stats": {"bytes": 18_204, "pages": 1, "data_items": 0, "data_items_verified": 0},
                                                    "error": None}}, base=base)


def make_fixture_run() -> RunState:
    r = RunState(bf.RUN_ID, bf.COMPANY, bf.TICKER, "screen", "ui", "Finished fixture run for building the panels", 5.0,
                 bf.ts(0), "fixture")
    base = time.monotonic() - 10_000
    start_replay(r, base)
    r.tick()
    commit_approval(r, copy.deepcopy(FX["fixture_decisions"]), base=base + 1000)
    r.tick()
    return r


RUNS[bf.RUN_ID] = make_fixture_run()


def get_run(run_id: str) -> RunState:
    r = RUNS.get(run_id)
    if not r:
        raise ApiError(404, "not_found", f"No run {run_id}")
    r.tick()
    return r


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.get("/healthz", response_model=m.Health)
def healthz():
    return {"ok": True, "version": CONTRACT_VERSION, "mode": "mock"}


@app.post(f"{PREFIX}/runs", response_model=m.Run, status_code=201)
def create_run(req: m.CreateRunRequest):
    r = RunState(new_run_id(), req.company, req.ticker, req.depth, req.created_via, req.notes, req.budget_usd, now_iso(), "fixture")
    RUNS[r.run_id] = r
    start_replay(r, time.monotonic())
    r.tick()
    return r.run()


@app.get(f"{PREFIX}/runs", response_model=m.RunList)
def list_runs():
    out = []
    for r in RUNS.values():
        r.tick()
        out.append(r.run())
    return {"runs": sorted(out, key=lambda x: x["created_at"], reverse=True)}


@app.get(f"{PREFIX}/runs/{{run_id}}", response_model=m.Run)
def read_run(run_id: str):
    return get_run(run_id).run()


@app.get(f"{PREFIX}/runs/{{run_id}}/events", response_model=m.EventsPage)
def read_events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)):
    r = get_run(run_id)
    evs = r.released[after: after + limit]
    return {"run_id": run_id, "events": evs, "last_seq": evs[-1]["seq"] if evs else after,
            "run_status": r.status, "stage": r.stage}


@app.post(f"{PREFIX}/runs/{{run_id}}/stop", response_model=m.Run)
def stop_run(run_id: str, req: m.StopRequest):
    r = get_run(run_id)
    if r.status in ("done", "stopped", "failed"):
        raise ApiError(409, "invalid_state", f"Run is already {r.status}")
    now = time.monotonic()
    r.stopped_mono, r.stop_reason = now, req.reason or "Stopped by user"
    r.policy_version += 1
    r.schedule(0, {"type": "run.status", "stage": r.stage, "agent_id": None, "level": "warn", "message": "Kill switch on",
                   "data": {"status": "stopped", "stage": r.stage}, "_after_stop": True}, base=now)
    r.schedule(0.001, {"type": "run.stopped", "stage": r.stage, "agent_id": None, "level": "warn",
                       "message": f"Run stopped: {r.stop_reason}", "data": {"reason": r.stop_reason}, "_after_stop": True}, base=now)
    r.schedule(0.002, {"type": "policy.updated", "stage": r.stage, "agent_id": None, "level": "warn",
                       "message": f"Gateway policy v{r.policy_version}: kill switch, every request denied",
                       "data": {"version": r.policy_version, "change": "Kill switch on", "rule_ids": []}, "_after_stop": True}, base=now)
    for cm in r.comments.values():
        if cm["status"] in ("queued", "working"):
            r.schedule(0.003, {"type": "comment.resolved", "stage": r.stage, "agent_id": None, "level": "warn", "_after_stop": True,
                               "message": f"Comment {cm['comment_id']} rejected: run stopped",
                               "data": {"comment": {**cm, "status": "rejected", "resolved_at": now_iso(),
                                                    "response": "Run stopped before this comment was applied."}}}, base=now)
    working = [a for a in r.agents.values() if a["status"] == "working"]
    who = working[0] if working else {"agent_id": "verifier-01", "role": "verifier"}
    r.schedule(0.8 / SPEED, {"type": "gateway.request", "stage": r.stage, "agent_id": who["agent_id"], "level": "warn",
                             "message": f"{who['agent_id']} -> model call blocked: run stopped", "_after_stop": True,
                             "data": {"request_id": f"REQ-K{len(r.released):04d}", "role": who["role"], "action": "model.invoke",
                                      "target": bf.HAIKU, "decision": "blocked", "rule_id": None,
                                      "reason": "Kill switch: run stopped. In-flight results are discarded.", "source_id": None,
                                      "latency_ms": 2, "http_status": None, "bytes": None, "tokens_in": None, "tokens_out": None,
                                      "cost_usd": None}}, base=now)
    r.tick()
    return r.run()


# ---------------------------------------------------------------------------
# Plan, approval, sources, policy
# ---------------------------------------------------------------------------


@app.get(f"{PREFIX}/runs/{{run_id}}/plan", response_model=m.Plan)
def read_plan(run_id: str):
    r = get_run(run_id)
    ready = any(e["type"] == "plan.ready" for e in r.released)
    return {"run_id": run_id, "needs": list(r.needs.values()), "sources": r.sorted_sources(), "ready": ready, "as_of_seq": r.last_seq}


@app.post(f"{PREFIX}/runs/{{run_id}}/sources", response_model=m.Source, status_code=201)
def add_source(run_id: str, req: m.AddSourceRequest):
    r = get_run(run_id)
    if r.status != "awaiting_approval":
        raise ApiError(409, "invalid_state", "Sources can only be added while the run waits for approval")
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(422, "validation_error", "url must be an absolute http(s) URL", {"url": req.url})
    sid = f"S-{len(r.sources) + 1:03d}"
    while sid in r.sources:
        sid = f"S-{int(sid[2:]) + 1:03d}"
    kind = req.kind or "other"
    rank = 1 + max([s["rank"] for s in r.sources.values() if s["tier"] == req.tier] or [0])
    s = {"source_id": sid, "title": req.title or parsed.netloc + parsed.path, "url": req.url, "domain": parsed.netloc,
         "kind": kind, "data_types": ["other"],
         "access": {"method": "api" if req.api_key else "http_get", "auth": "api_key" if req.api_key else "none",
                    "secret_ref": f"secret://{run_id}/{sid}" if req.api_key else None},
         "tier": req.tier, "rank": rank, "scores": {"reliability": 0.5, "relevance": 0.5},
         "rationale": "Added by the analyst" + (f": {req.note}" if req.note else ""), "need_ids": [], "criteria": [],
         "vault_folder": FOLDER_BY_KIND[kind], "proposed_by": "user", "approval": "approved", "status": "proposed",
         "doc_ids": [], "stats": {"bytes": 0, "pages": 0, "data_items": 0, "data_items_verified": 0}, "error": None}
    r.user_sources.append(s)
    r.schedule(0, {"type": "plan.source", "stage": "approve", "agent_id": None, "level": "info",
                   "message": f"Analyst added {sid}: {s['title']}", "data": {"source": s}})
    r.tick()
    return r.sources[sid]


@app.post(f"{PREFIX}/runs/{{run_id}}/approval", response_model=m.ApprovalResponse)
def approve(run_id: str, req: m.ApprovalRequest):
    r = get_run(run_id)
    if r.status != "awaiting_approval":
        raise ApiError(409, "invalid_state", f"Run is {r.status} at stage {r.stage}, not awaiting approval")
    decided = {d.source_id for d in req.decisions}
    unknown = sorted(decided - set(r.sources))
    if unknown:
        raise ApiError(422, "validation_error", "Unknown source ids", {"unknown": unknown})
    undecided = sorted(s["source_id"] for s in r.sources.values() if s["tier"] == "priority" and s["source_id"] not in decided
                       and not (s["proposed_by"] == "user"))
    if undecided:
        raise ApiError(422, "validation_error", "Every priority source needs a decision", {"undecided": undecided})
    commit_approval(r, [d.model_dump() for d in req.decisions], base=time.monotonic())
    r.tick()
    return {"run": r.run(), "policy": r.policy()}


@app.post(f"{PREFIX}/runs/{{run_id}}/sources/{{source_id}}/revoke", response_model=m.RevokeResponse)
def revoke(run_id: str, source_id: str, req: m.RevokeRequest):
    r = get_run(run_id)
    s = r.sources.get(source_id)
    if not s:
        raise ApiError(404, "not_found", f"No source {source_id}")
    if r.decisions is None:
        raise ApiError(409, "invalid_state", "Revoke applies after approval. Before that, reject the source in the approval request.")
    if source_id in r.revoked:
        raise ApiError(409, "conflict", f"{source_id} is already revoked")
    r.revoked[source_id] = req.reason or ""
    r.policy_version += 1
    now = time.monotonic()
    r.schedule(0, {"type": "policy.updated", "stage": r.stage, "agent_id": None, "level": "warn", "_user": True, "_after_stop": True,
                   "message": f"Gateway policy v{r.policy_version}: {source_id} revoked",
                   "data": {"version": r.policy_version, "change": f"Revoked {source_id}", "rule_ids": [f"R-revoke-{source_id}"]}}, base=now)
    r.schedule(0, {"type": "source.status", "stage": r.stage, "agent_id": None, "level": "warn", "_user": True, "_after_stop": True,
                      "message": f"{source_id} revoked", "data": {"source_id": source_id, "status": "revoked", "stats": s["stats"],
                                                                  "error": req.reason or "Revoked by user"}}, base=now)
    if r.status not in ("done", "stopped", "failed"):
        r.schedule(1.5 / SPEED, {"type": "gateway.request", "stage": r.stage, "agent_id": "fetcher-02", "level": "warn", "_user": True,
                                 "message": f"fetcher-02 fetch {s['url']} blocked (R-revoke-{source_id})",
                                 "data": {"request_id": f"REQ-R{len(r.released):04d}", "role": "fetcher", "action": "fetch",
                                          "target": s["url"], "decision": "blocked", "rule_id": f"R-revoke-{source_id}",
                                          "reason": "Source revoked mid-run. Subsequent requests are denied.", "source_id": source_id,
                                          "latency_ms": 3, "http_status": None, "bytes": None, "tokens_in": None, "tokens_out": None,
                                          "cost_usd": None}}, base=now)
    r.tick()
    return {"source": r.sources[source_id], "policy": r.policy()}


@app.get(f"{PREFIX}/runs/{{run_id}}/policy", response_model=m.Policy)
def read_policy(run_id: str):
    return get_run(run_id).policy()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

STATUS_BY_STAGE = {"plan": "not_started", "approve": "not_started", "collect": "not_started", "extract": "drafting",
                   "draft": "drafting", "verify": "verifying", "repair": "repairing"}


@app.get(f"{PREFIX}/runs/{{run_id}}/report", response_model=m.AgentReport)
def read_report(run_id: str):
    r = get_run(run_id)
    rep = FX["report"]
    ids = set(r.items)
    sections = [{**s, "item_ids": [i for i in s["item_ids"] if i in ids]} for s in rep["sections"]]
    ready = r.report_ready
    status = "final" if ready else STATUS_BY_STAGE.get(r.stage, "final" if r.stage in ("summarize", "verify_summary", "complete") else "drafting")
    return {**rep, "status": status, "sections": [s for s in sections if s["item_ids"]],
            "items": [r.items[i] for i in r.item_order], "rollup": rep["rollup"] if ready else None,
            "certainty": rep["certainty"] if ready else None, "holes": rep["holes"] if ready else [],
            "generated_at": rep["generated_at"] if ready else None, "run_id": run_id, "company": r.company,
            "ticker": r.ticker, "as_of_seq": r.last_seq}


@app.get(f"{PREFIX}/runs/{{run_id}}/summary", response_model=m.HumanReport)
def read_summary(run_id: str):
    r = get_run(run_id)
    fx = FX["summary"]
    if r.summary_ready:
        sections = copy.deepcopy(fx["sections"])
        for sec in sections:
            for b in sec["blocks"]:
                for s in b.get("sentences") or ([b["caption"]] if b.get("caption") else []):
                    live = r.sentences.get(s["id"])
                    if live:
                        s.update({"text": live["text"], "verification": live["verification"], "certainty": live["certainty"]})
        return {**fx, "sections": sections, "run_id": run_id, "company": r.company, "ticker": r.ticker, "as_of_seq": r.last_seq}
    status = "not_started" if r.stage not in ("summarize", "verify_summary") else ("drafting" if r.stage == "summarize" else "verifying")
    grouped: dict[str, list[dict]] = {}
    for sid, s in r.sentences.items():
        grouped.setdefault(r.sentence_section[sid], []).append(s)
    sents = [s for g in grouped.values() for s in g]
    words = sum(len(s["text"].split()) for s in sents)
    st = [s["verification"]["status"] for s in sents]
    return {**fx, "status": status, "run_id": run_id, "company": r.company, "ticker": r.ticker, "holes": [], "certainty": None,
            "sections": [{"heading": h, "blocks": [{"type": "paragraph", "sentences": g}]} for h, g in grouped.items()],
            "stats": {"sentences": len(sents), "passed": st.count("pass"), "fixed": st.count("fixed"), "failed": st.count("fail"),
                      "words": words, "est_pages": round(words / 450 + 1.0, 1) if sents else 0.0},
            "as_of_seq": r.last_seq}


@app.patch(f"{PREFIX}/runs/{{run_id}}/items/{{item_id}}", response_model=m.Item)
def patch_item(run_id: str, item_id: str, req: m.ItemPatch):
    r = get_run(run_id)
    if r.status == "stopped":
        raise ApiError(409, "invalid_state", "Run is stopped (kill switch)")
    it = r.items.get(item_id)
    if not it:
        raise ApiError(404, "not_found", f"No item {item_id}")
    if it["kind"] == "chart":
        raise ApiError(422, "validation_error", "Charts are built by code and cannot be edited")
    field = "statement" if it["kind"] == "data" else "text"
    before = it[field]
    edited = copy.deepcopy(it)
    edited[field] = req.text
    pending = {"status": "pending", "checks": [], "attempts": it["verification"]["attempts"], "verified_at": None}
    edited["verification"] = pending
    edited["certainty"] = None
    now = time.monotonic()
    r.schedule(0, {"type": "item.updated", "stage": r.stage, "agent_id": None, "level": "info", 
                   "message": f"{item_id} edited by the analyst. Re-verifying.", "data": {"item": edited, "reason": "user_edit"}}, base=now)
    done_v = copy.deepcopy(it["verification"])
    done_v["attempts"] = done_v["attempts"] + [{"attempt": len(done_v["attempts"]) + 1, "status": done_v["status"], "checks": [],
                                                "action": "user_edit", "before": before, "after": req.text,
                                                "note": "Analyst edit, re-verified (mock always passes)", "at": now_iso(1.5)}]
    r.schedule(1.5 / SPEED, {"type": "item.verified", "stage": r.stage, "agent_id": f"verifier-{item_id}", "level": "info",
                              "message": f"{item_id} re-verified after edit",
                             "data": {"item_id": item_id, "verification": done_v, "certainty": it.get("certainty")}}, base=now)
    r.tick()
    return r.items[item_id]


# ---------------------------------------------------------------------------
# Agents, documents, vault
# ---------------------------------------------------------------------------


@app.get(f"{PREFIX}/runs/{{run_id}}/agents", response_model=m.AgentList)
def read_agents(run_id: str):
    r = get_run(run_id)
    return {"agents": list(r.agents.values()), "as_of_seq": r.last_seq}


def _doc(doc_id: str) -> dict:
    for d in FX["docs"]:
        if d["doc_id"] == doc_id:
            return d
    raise ApiError(404, "not_found", f"No document {doc_id}")


@app.get(f"{PREFIX}/runs/{{run_id}}/documents", response_model=m.DocumentList)
def list_documents(run_id: str):
    r = get_run(run_id)
    return {"documents": [d for d in FX["docs"] if d["doc_id"] in r.docs]}


@app.get(f"{PREFIX}/runs/{{run_id}}/documents/{{doc_id}}", response_model=m.Document)
def read_document(run_id: str, doc_id: str):
    r = get_run(run_id)
    if doc_id not in r.docs:
        raise ApiError(404, "not_found", f"{doc_id} has not been fetched in this run")
    return _doc(doc_id)


@app.get(f"{PREFIX}/runs/{{run_id}}/documents/{{doc_id}}/pages/{{page}}", response_model=m.DocumentPage)
def read_page(run_id: str, doc_id: str, page: int):
    r = get_run(run_id)
    if doc_id not in r.docs:
        raise ApiError(404, "not_found", f"{doc_id} has not been fetched in this run")
    d = _doc(doc_id)
    if not 1 <= page <= d["pages"]:
        raise ApiError(404, "not_found", f"{doc_id} has pages 1 to {d['pages']}")
    highlights = []
    for it in r.items.values():
        for c in it.get("citations", []):
            loc = c["locator"]
            if c["doc_id"] == doc_id and loc["type"] == "text" and loc["page"] == page:
                highlights.append({"item_id": it["id"], "char_start": loc["char_start"], "char_end": loc["char_end"],
                                   "status": it["verification"]["status"], "role": c["role"]})
    return {"doc_id": doc_id, "page": page, "pages": d["pages"], "content_type": d["content_type"],
            "text": FX["page_text"](doc_id, page), "highlights": highlights}


def _visible_paths(r: RunState) -> list[str]:
    visible = []
    released_docs = [d for d in FX["docs"] if d["doc_id"] in r.docs]
    doc_paths = {p for d in released_docs for p in d["vault_paths"].values()}
    for p in FX["vault_files"]:
        if p in doc_paths or "/00-Meta/" in p:
            visible.append(p)
        elif "/09-Analysis/agent-report" in p or "/12-Verification/" in p:
            if r.report_ready:
                visible.append(p)
        elif "/09-Analysis/human-report" in p and r.summary_ready:
            visible.append(p)
    return visible


@app.get(f"{PREFIX}/runs/{{run_id}}/vault/tree", response_model=m.VaultNode)
def vault_tree(run_id: str):
    r = get_run(run_id)
    paths = _visible_paths(r)
    root = bf.VAULT_ROOT.replace(bf.RUN_ID, run_id)
    tree = bf.vault_tree([p.replace(bf.RUN_ID, run_id) for p in paths], root=root)

    def sizes(node):
        if node["type"] == "file":
            node["size"] = len(FX["vault_files"][node["path"].replace(run_id, bf.RUN_ID)].encode())
        for ch in node.get("children") or []:
            sizes(ch)
    sizes(tree)
    return tree


@app.get(f"{PREFIX}/runs/{{run_id}}/vault/file", response_model=m.VaultFile)
def vault_file(run_id: str, path: str = Query(...)):
    r = get_run(run_id)
    key = path.replace(run_id, bf.RUN_ID)
    if key not in _visible_paths(r):
        raise ApiError(404, "not_found", f"No vault file {path}")
    text = FX["vault_files"][key]
    ext = key.rsplit(".", 1)[-1]
    ctype = {"json": "application/json", "jsonl": "application/x-ndjson", "md": "text/markdown", "html": "text/html",
             "pdf": "text/plain"}.get(ext, "text/plain")
    limit = 1_000_000
    return {"path": path, "content_type": ctype, "size": len(text.encode()), "text": text[:limit], "truncated": len(text) > limit,
            "download_url": None}


# ---------------------------------------------------------------------------
# Comments and demo controls
# ---------------------------------------------------------------------------


@app.get(f"{PREFIX}/runs/{{run_id}}/comments", response_model=m.CommentList)
def list_comments(run_id: str):
    r = get_run(run_id)
    return {"comments": list(r.comments.values())}


@app.post(f"{PREFIX}/runs/{{run_id}}/comments", response_model=m.Comment, status_code=201)
def add_comment(run_id: str, req: m.CommentRequest):
    r = get_run(run_id)
    if req.target_type in ("item", "summary_sentence") and not req.target_id:
        raise ApiError(422, "validation_error", "target_id is required for item and summary_sentence comments")
    if req.target_type == "item" and req.target_id not in r.items:
        raise ApiError(404, "not_found", f"No item {req.target_id}")
    cid = f"CM-{len(r.comments) + 1:03d}"
    c = {"comment_id": cid, "target_type": req.target_type, "target_id": req.target_id, "text": req.text, "status": "queued",
         "response": None, "changed_item_ids": [], "created_at": now_iso(), "resolved_at": None}
    r.comments[cid] = c
    now = time.monotonic()
    if r.status == "stopped":
        rej = {**c, "status": "rejected", "response": "Run is stopped (kill switch). Start a new run to apply comments.",
               "resolved_at": now_iso()}
        r.schedule(0, {"type": "comment.created", "stage": r.stage, "agent_id": None, "level": "info", "_after_stop": True,
                       "message": f"Comment {cid} on {req.target_id or req.target_type}", "data": {"comment": c}}, base=now)
        r.schedule(0.001, {"type": "comment.resolved", "stage": r.stage, "agent_id": None, "level": "warn", "_after_stop": True,
                           "message": f"Comment {cid} rejected: run stopped", "data": {"comment": rej}}, base=now)
        r.tick()
        return r.comments[cid]
    r.schedule(0, {"type": "comment.created", "stage": r.stage, "agent_id": None, "level": "info", "_after_stop": True,
                   "message": f"Comment {cid} on {req.target_id or req.target_type}", "data": {"comment": c}}, base=now)
    resolved = {**c, "status": "applied", "resolved_at": now_iso(4 / SPEED)}
    if req.target_type == "item":
        it = r.items[req.target_id]
        agent_id = f"reviser-{cid}"
        r.schedule(0.5 / SPEED, {"type": "agent.spawned", "stage": r.stage, "agent_id": agent_id, "level": "info", 
                                 "message": f"{agent_id} spawned for {req.target_id}",
                                 "data": {"agent": bf.agent(agent_id, "reviser", bf.SONNET, f"Apply {cid} to {req.target_id}",
                                                            [req.target_id])}}, base=now)
        revised = copy.deepcopy(it)
        att = {"attempt": len(it["verification"]["attempts"]) + 1, "status": it["verification"]["status"], "checks": [],
               "action": "reword", "before": None, "after": None, "note": f"Revised per {cid}: {req.text}", "at": now_iso(3 / SPEED)}
        revised["verification"] = {**it["verification"], "attempts": it["verification"]["attempts"] + [att]}
        r.schedule(3 / SPEED, {"type": "item.updated", "stage": r.stage, "agent_id": agent_id, "level": "info", 
                               "message": f"{req.target_id} revised per {cid}", "data": {"item": revised, "reason": "comment"}}, base=now)
        r.schedule(3.5 / SPEED, {"type": "item.verified", "stage": r.stage, "agent_id": f"verifier-{req.target_id}", "level": "info",
                                  "message": f"{req.target_id} re-verified",
                                 "data": {"item_id": req.target_id, "verification": revised["verification"],
                                          "certainty": it.get("certainty")}}, base=now)
        r.schedule(3.6 / SPEED, {"type": "agent.status", "stage": r.stage, "agent_id": agent_id, "level": "info", 
                                 "message": f"{agent_id} done", "data": {"agent_id": agent_id, "status": "done", "task": "Comment applied",
                                                                         "usage": {"model_calls": 1, "tokens_in": 2100, "tokens_out": 240,
                                                                                   "cost_usd": 0.0066, "budget_usd": None}}}, base=now)
        resolved.update({"response": "Applied and re-verified. (Mock: the text is unchanged, the history records the request.)",
                         "changed_item_ids": [req.target_id]})
    else:
        resolved.update({"response": "Noted. The mock records comments on this target but changes nothing."})
    r.schedule(4 / SPEED, {"type": "comment.resolved", "stage": r.stage, "agent_id": None, "level": "info", 
                           "message": f"Comment {cid} resolved", "data": {"comment": resolved}}, base=now)
    r.tick()
    return r.comments[cid]


@app.post(f"{PREFIX}/runs/{{run_id}}/demo/inject-errors", response_model=m.InjectErrorsResponse)
def inject_errors(run_id: str, req: m.InjectErrorsRequest):
    """Demo control: corrupt a digit in N verified quotes, then show the checker catch and repair them."""
    r = get_run(run_id)
    if r.status == "stopped":
        raise ApiError(409, "invalid_state", "Run is stopped (kill switch)")
    pool = [it for it in r.items.values() if it["kind"] == "data" and it["verification"]["status"] == "pass"
            and it["citations"][0]["locator"]["type"] == "text" and re.search(r"\d", it["citations"][0]["quote"])]
    picked = pool[: req.count]
    if not picked:
        raise ApiError(409, "invalid_state", "No verified text data items yet. Wait for the extract stage.")
    now = time.monotonic()
    for k, it in enumerate(picked):
        q = it["citations"][0]["quote"]
        i = re.search(r"\d", q).start()
        bad_q = q[:i] + str((int(q[i]) + 1) % 10) + q[i + 1:]
        bad = copy.deepcopy(it)
        bad["citations"][0]["quote"] = bad_q
        bad["verification"] = {"status": "pending", "checks": [], "attempts": it["verification"]["attempts"], "verified_at": None}
        bad["certainty"] = None
        fail_checks = [bf.check("quote_exact", "code", "fail", "Quote not found at the cited span. Closest span differs by 1 character.",
                                diff=bf.diff_segments(bad_q, q), closest_locator=it["citations"][0]["locator"])]
        fixed_v = copy.deepcopy(it["verification"])
        fixed_v["status"] = "fixed"
        fixed_v["attempts"] = it["verification"]["attempts"] + [
            {"attempt": len(it["verification"]["attempts"]) + 1, "status": "fail", "checks": fail_checks, "action": "recite",
             "before": bad_q, "after": q, "note": "DEMO INJECTION. Quote replaced with the exact source span.", "at": now_iso(2)},
        ]
        base = now + k * 0.4
        r.schedule(0, {"type": "item.updated", "stage": r.stage, "agent_id": "demo", "level": "warn", 
                       "message": f"DEMO: corrupted one digit in {it['id']}", "data": {"item": bad, "reason": "demo_injection"}}, base=base)
        r.schedule(0.6 / SPEED, {"type": "item.verified", "stage": r.stage, "agent_id": None, "level": "warn", 
                                 "message": f"{it['id']} FAILED quote_exact (demo injection)",
                                 "data": {"item_id": it["id"], "verification": {"status": "fail", "checks": fail_checks, "attempts": [],
                                                                               "verified_at": None}, "certainty": None}}, base=base)
        r.schedule(2 / SPEED, {"type": "item.updated", "stage": r.stage, "agent_id": "repairer-demo", "level": "info", 
                               "message": f"{it['id']} re-cited from the source", "data": {"item": {**copy.deepcopy(it), "verification": fixed_v},
                                                                                        "reason": "repair"}}, base=base)
        r.schedule(2.2 / SPEED, {"type": "item.verified", "stage": r.stage, "agent_id": None, "level": "info", 
                                 "message": f"{it['id']} fixed", "data": {"item_id": it["id"], "verification": fixed_v,
                                                                          "certainty": it.get("certainty")}}, base=base)
    r.tick()
    return {"item_ids": [it["id"] for it in picked], "note": "Demo control. Label it as a deliberate injection in the UI."}
