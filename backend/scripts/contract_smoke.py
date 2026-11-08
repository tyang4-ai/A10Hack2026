"""Contract smoke test: drive the API (real or mock) and validate every response against the contract models.

Usage (from the repo root): PY backend/scripts/contract_smoke.py --base <url> [--run <id>] [--key <shared key>]

Without --run: create an SHL run, poll until awaiting_approval, check the approval rules (422s and 409s),
approve every priority source, check the policy, read every panel, then stop the run. If the run already
finished by then (a later stage failed, or the mock's replay ran out), a second run is stopped while it
waits for approval, so the stop route is still checked.
With --run: read an existing run through every GET route.
Uses only the standard library for HTTP. The key comes from --key or AXELROD_API_KEY and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

from backend.axelrod.contract import m  # noqa: E402

FINISHED = ("done", "stopped", "failed")


class Smoke:
    def __init__(self, base: str, key: str | None, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.root = self.base[: -len("/api/v1")] if self.base.endswith("/api/v1") else self.base
        self.key = key
        self.timeout = timeout
        self.passed = 0
        self.failures: list[str] = []
        self.notes: list[str] = []

    # -- http -------------------------------------------------------------------------------

    def call(self, method: str, path: str, body: Any = None, params: dict | None = None,
             root: bool = False) -> tuple[int, Any]:
        url = (self.root if root else self.base) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.key:
            headers["X-Axelrod-Key"] = self.key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    # -- checks -----------------------------------------------------------------------------

    def ok(self, label: str) -> None:
        self.passed += 1
        print(f"  ok    {label}")

    def fail(self, label: str, why: str) -> None:
        self.failures.append(f"{label}: {why}")
        print(f"  FAIL  {label}: {why}")

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  note  {text}")

    def check(self, label: str, method: str, path: str, model: Any, *, body: Any = None, params: dict | None = None,
              status: int = 200, root: bool = False, quiet: bool = False) -> Any:
        """Call, expect `status`, validate the body against `model`. Returns the parsed model or None."""
        code, data = self.call(method, path, body, params, root)
        if code != status:
            self.fail(label, f"HTTP {code}, expected {status}: {str(data)[:300]}")
            return None
        try:
            parsed = model.model_validate(data) if hasattr(model, "model_validate") else model.validate_python(data)
        except Exception as e:
            self.fail(label, f"does not match {getattr(model, '__name__', model)}: {str(e)[:500]}")
            return None
        if not quiet:
            self.ok(f"{label} -> {getattr(model, '__name__', 'model')}")
        return parsed

    def expect_error(self, label: str, method: str, path: str, status: int, code: str, *, body: Any = None,
                     params: dict | None = None) -> m.ErrorBody | None:
        got, data = self.call(method, path, body, params)
        try:
            err = m.ErrorResponse.model_validate(data).error
        except Exception as e:
            self.fail(label, f"HTTP {got}, not the error envelope: {str(data)[:200]} ({str(e)[:200]})")
            return None
        if got != status or err.code != code:
            self.fail(label, f"HTTP {got} {err.code}, expected {status} {code}: {err.message}")
            return None
        self.ok(f"{label} -> {status} {code}")
        return err

    def expect(self, label: str, cond: bool, why: str) -> None:
        if cond:
            self.ok(label)
        else:
            self.fail(label, why)

    # -- building blocks --------------------------------------------------------------------

    def wait_status(self, run_id: str, wanted: tuple[str, ...], timeout: float) -> m.Run | None:
        deadline = time.monotonic() + timeout
        run = None
        while time.monotonic() < deadline:
            run = self.check("poll run", "GET", f"/runs/{run_id}", m.Run, quiet=True)
            if run is None or run.status in wanted:
                return run
            time.sleep(0.25)
        self.fail(f"wait for {'/'.join(wanted)}", f"timed out after {timeout:.0f}s (last {run and run.status})")
        return None

    def all_events(self, run_id: str, page_size: int = 500) -> list:
        events, after = [], 0
        while True:
            page = self.check(f"events after={after}", "GET", f"/runs/{run_id}/events", m.EventsPage,
                              params={"after": after, "limit": page_size}, quiet=True)
            if page is None:
                return events
            events += page.events
            if len(page.events) < page_size:
                break
            after = page.last_seq
        seqs = [e.seq for e in events]
        self.expect(f"events gapless and ordered ({len(events)} events)", seqs == list(range(1, len(seqs) + 1)),
                    f"seqs are not 1..n: {seqs[:10]}...")
        return events

    def read_everything(self, run_id: str) -> None:
        """Every GET route for one run."""
        r = f"/runs/{run_id}"
        self.check("GET run", "GET", r, m.Run)
        self.check("GET plan", "GET", f"{r}/plan", m.Plan)
        self.all_events(run_id)
        page = self.check("GET events limit=1", "GET", f"{r}/events", m.EventsPage, params={"after": 0, "limit": 1})
        if page is not None:
            self.expect("events limit=1 gives one event", len(page.events) <= 1, f"got {len(page.events)}")
        self.expect_error("GET events limit=2001", "GET", f"{r}/events", 422, "validation_error",
                          params={"limit": 2001})
        self.check("GET policy", "GET", f"{r}/policy", m.Policy)
        self.check("GET report", "GET", f"{r}/report", m.AgentReport)
        self.check("GET summary", "GET", f"{r}/summary", m.HumanReport)
        self.check("GET agents", "GET", f"{r}/agents", m.AgentList)
        self.check("GET comments", "GET", f"{r}/comments", m.CommentList)
        docs = self.check("GET documents", "GET", f"{r}/documents", m.DocumentList)
        for doc in (docs.documents[:1] if docs else []):
            self.check(f"GET document {doc.doc_id}", "GET", f"{r}/documents/{doc.doc_id}", m.Document)
            code, data = self.call("GET", f"{r}/documents/{doc.doc_id}/pages/1")
            if code == 200:
                self.check(f"GET {doc.doc_id} page 1", "GET", f"{r}/documents/{doc.doc_id}/pages/1", m.DocumentPage)
            else:
                self.expect_error(f"GET {doc.doc_id} page 1 (not built)", "GET",
                                  f"{r}/documents/{doc.doc_id}/pages/1", 409, "invalid_state")
                self.note(f"page viewer answered 409: {data['error']['message'] if isinstance(data, dict) else data}")
        self.expect_error("GET unknown document", "GET", f"{r}/documents/DOC-999", 404, "not_found")
        tree = self.check("GET vault tree", "GET", f"{r}/vault/tree", m.VaultNode)
        path = first_file(tree) if tree else None
        if path:
            self.check("GET vault file", "GET", f"{r}/vault/file", m.VaultFile, params={"path": path})
        else:
            self.note("vault has no files yet, vault/file not checked")
        self.expect_error("GET vault file outside the run", "GET", f"{r}/vault/file", 404, "not_found",
                          params={"path": "../../etc/passwd"})
        runs = self.check("GET runs", "GET", "/runs", m.RunList)
        if runs is not None:
            self.expect("run is listed", any(x.run_id == run_id for x in runs.runs), f"{run_id} missing from /runs")

    # -- flows ------------------------------------------------------------------------------

    def create_flow(self, company: str, ticker: str | None, wait_s: float) -> None:
        self.check("GET /healthz", "GET", "/healthz", m.Health, root=True)
        self.expect_error("GET unknown run", "GET", "/runs/run_20000101_nope00", 404, "not_found")
        self.expect_error("POST runs without company", "POST", "/runs", 422, "validation_error", body={})
        run = self.check("POST runs", "POST", "/runs", m.Run, status=201,
                         body={"company": company, "ticker": ticker, "created_via": "api", "notes": "contract smoke"})
        if run is None:
            return
        rid, r = run.run_id, f"/runs/{run.run_id}"
        print(f"  run   {rid} (mode {run.mode})")
        run = self.wait_status(rid, ("awaiting_approval",) + FINISHED, wait_s)
        if run is None or run.status != "awaiting_approval":
            self.fail("reach awaiting_approval", f"run is {run and run.status}: {run and run.error}")
            return
        self.ok("run reached awaiting_approval")
        plan = self.check("GET plan", "GET", f"{r}/plan", m.Plan)
        if plan is None:
            return
        self.expect("plan is ready", plan.ready, "plan.ready is false while awaiting approval")
        priority = [s.source_id for s in plan.sources if s.tier == "priority" and s.proposed_by == "planner"]
        self.expect_error("revoke before approval", "POST", f"{r}/sources/{plan.sources[0].source_id}/revoke", 409,
                          "invalid_state", body={})
        if priority:
            err = self.expect_error("approval missing priority decisions", "POST", f"{r}/approval", 422,
                                    "validation_error", body={"decisions": []})
            if err is not None:
                self.expect("422 lists details.undecided", sorted(err.details.get("undecided", [])) == sorted(priority),
                            f"details: {err.details}")
        err = self.expect_error("approval with an unknown source", "POST", f"{r}/approval", 422, "validation_error",
                                body={"decisions": [{"source_id": "S-999", "approval": "approved"}]})
        if err is not None:
            self.expect("422 lists details.unknown", err.details.get("unknown") == ["S-999"], f"details: {err.details}")
        decisions = [{"source_id": sid, "approval": "approved"} for sid in priority]
        resp = self.check("POST approval", "POST", f"{r}/approval", m.ApprovalResponse, body={"decisions": decisions})
        if resp is None:
            return
        rule_ids = {x.rule_id for x in resp.policy.rules}
        missing = [sid for sid in priority if f"R-allow-{sid}" not in rule_ids]
        self.expect("policy has an allow rule per approved source", not missing, f"no R-allow rule for {missing}")
        self.expect("policy is default deny", resp.policy.default_effect == "deny", resp.policy.default_effect)
        # the mock flips the status a moment after it answers, so wait for that before the second try
        self.wait_status(rid, ("running",) + FINISHED, 10.0)
        self.expect_error("approval twice", "POST", f"{r}/approval", 409, "invalid_state", body={"decisions": []})
        pol = self.check("GET policy", "GET", f"{r}/policy", m.Policy)
        if pol is not None:
            self.expect("policy version is at least 1", pol.version >= 1, f"version {pol.version}")
        self.check("POST comment", "POST", f"{r}/comments", m.Comment, status=201,
                   body={"target_type": "run", "text": "contract smoke comment"})
        self.read_everything(rid)
        self.stop_flow(rid, company, ticker, wait_s)

    def stop_flow(self, rid: str, company: str, ticker: str | None, wait_s: float) -> None:
        r = f"/runs/{rid}"
        code, data = self.call("POST", f"{r}/stop", {"reason": "contract smoke"})
        if code == 409:
            self.expect_error("stop a finished run", "POST", f"{r}/stop", 409, "invalid_state", body={})
            run = self.check("GET run", "GET", r, m.Run, quiet=True)
            self.note(f"{rid} had already finished ({run and run.status}: {run and run.error}), "
                      "so stop is checked on a second run")
            second = self.check("POST runs (for stop)", "POST", "/runs", m.Run, status=201,
                                body={"company": company, "ticker": ticker, "created_via": "api",
                                      "notes": "contract smoke, stop check"})
            if second is None:
                return
            if self.wait_status(second.run_id, ("awaiting_approval",) + FINISHED, wait_s) is None:
                return
            rid, r = second.run_id, f"/runs/{second.run_id}"
        stopped = self.check("POST stop", "POST", f"{r}/stop", m.Run, body={"reason": "contract smoke"}) \
            if code == 409 else self._validate_stop(code, data)
        if stopped is None:
            return
        self.expect("run is stopped", stopped.status == "stopped", f"status {stopped.status}")
        pol = self.check("GET policy after stop", "GET", f"{r}/policy", m.Policy)
        if pol is not None:
            self.expect("kill switch is on", pol.kill_switch, "policy.kill_switch is false after stop")
        self.expect_error("stop twice", "POST", f"{r}/stop", 409, "invalid_state", body={})
        c = self.check("POST comment on a stopped run", "POST", f"{r}/comments", m.Comment, status=201,
                       body={"target_type": "run", "text": "after stop"})
        if c is not None:
            self.expect("comment on a stopped run is rejected", self.comment_rejected(rid, c), f"status {c.status}")
        time.sleep(0.5)
        run = self.check("GET run after stop", "GET", r, m.Run)
        if run is not None:
            self.expect("run stays stopped", run.status == "stopped", f"status {run.status}")
        self.all_events(rid)

    def comment_rejected(self, rid: str, comment: m.Comment, timeout: float = 3.0) -> bool:
        """Rejected in the response, or (the mock resolves it a moment later) in GET /comments soon after."""
        deadline = time.monotonic() + timeout
        while comment.status != "rejected" and time.monotonic() < deadline:
            time.sleep(0.2)
            listing = self.check("GET comments", "GET", f"/runs/{rid}/comments", m.CommentList, quiet=True)
            found = [x for x in (listing.comments if listing else []) if x.comment_id == comment.comment_id]
            if found:
                comment = found[0]
        return comment.status == "rejected"

    def _validate_stop(self, code: int, data: Any) -> m.Run | None:
        if code != 200:
            self.fail("POST stop", f"HTTP {code}: {str(data)[:300]}")
            return None
        try:
            run = m.Run.model_validate(data)
        except Exception as e:
            self.fail("POST stop", f"does not match Run: {str(e)[:300]}")
            return None
        self.ok("POST stop -> Run")
        return run


def first_file(node: m.VaultNode) -> str | None:
    if node.type == "file":
        return node.path
    for child in node.children or []:
        found = first_file(child)
        if found:
            return found
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://localhost:8000/api/v1", help="API base URL, ending in /api/v1")
    ap.add_argument("--run", help="read this existing run instead of creating one")
    ap.add_argument("--key", default=os.environ.get("AXELROD_API_KEY"), help="X-Axelrod-Key (default: env)")
    ap.add_argument("--company", default="Siemens Healthineers AG")
    ap.add_argument("--ticker", default="SHL")
    ap.add_argument("--wait", type=float, default=90.0, help="seconds to wait for awaiting_approval")
    args = ap.parse_args(argv)

    smoke = Smoke(args.base, args.key, args.wait)
    print(f"contract smoke against {smoke.base}")
    if args.run:
        smoke.check("GET /healthz", "GET", "/healthz", m.Health, root=True)
        smoke.read_everything(args.run)
    else:
        smoke.create_flow(args.company, args.ticker, args.wait)
    print(f"\n{smoke.passed} checks passed, {len(smoke.failures)} failed")
    for n in smoke.notes:
        print(f"note: {n}")
    for f in smoke.failures:
        print(f"FAIL: {f}")
    return 1 if smoke.failures else 0


if __name__ == "__main__":
    sys.exit(main())
