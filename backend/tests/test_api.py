"""The real API against the contract (B-store-api): routes, status codes, the error envelope, approval rules,
stop and revoke, restart and replay. Offline: the gateway's policy methods are monkeypatched with a small
fake, the plan is the two-source test sample, and the pipeline holds after approve."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.axelrod import demo, orchestrator
from backend.axelrod.api import create_app
from backend.axelrod.config import load_settings
from backend.axelrod.contract import CONTRACT_VERSION, m
from backend.axelrod.fetch import ingest
from backend.axelrod.gateway import Gateway
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import Store

P = "/api/v1"
SAMPLE_PLAN = Path(__file__).parent / "data" / "store" / "plan.json"
SHL = {"company": "Siemens Healthineers AG", "ticker": "SHL", "created_via": "api"}
FAKE_KEY = "sk-test-not-a-real-key"


# ---------------------------------------------------------------------------
# fakes and fixtures
# ---------------------------------------------------------------------------


def fake_build_policy(self: Gateway, *, emit: bool = True) -> m.Policy:
    st = self.ctx.state
    now = now_iso()
    rules = [m.PolicyRule(rule_id="R-sys-models", effect="allow", action="model.invoke", origin="system",
                          created_at=now)]
    for s in st.sources:
        if s.approval in ("approved", "auto_approved"):
            rules.append(m.PolicyRule(rule_id=f"R-allow-{s.source_id}", effect="allow", action="fetch",
                                      domain=s.domain, path_prefix="/", roles=["fetcher"], source_id=s.source_id,
                                      origin="user_approval" if s.approval == "approved" else "auto_tier",
                                      created_at=now))
        elif s.approval == "rejected":
            rules.append(m.PolicyRule(rule_id=f"R-reject-{s.source_id}", effect="deny", action="fetch",
                                      domain=s.domain, source_id=s.source_id, origin="user_rejection", created_at=now))
    st.policy = m.Policy(run_id=st.run.run_id, version=(st.policy.version if st.policy else 0) + 1, rules=rules,
                         role_scopes=[m.RoleScope(role="fetcher", actions=["fetch"])],
                         budget_usd=st.run.usage.budget_usd, updated_at=now, as_of_seq=st.run.last_seq)
    if emit:
        self.ctx.emit("policy.updated", {"version": st.policy.version, "change": "x", "rule_ids": []}, message="x")
    return st.policy


def fake_revoke(self: Gateway, source_id: str, reason: str | None) -> m.Policy:
    """Same duties as the real Gateway.revoke: new policy, source marked, policy.updated and source.status."""
    st = self.ctx.state
    pol = st.policy.model_copy(deep=True)
    pol.version += 1
    pol.rules.insert(0, m.PolicyRule(rule_id=f"R-revoke-{source_id}", effect="deny", action="fetch",
                                     source_id=source_id, origin="user_revoke", note=reason, created_at=now_iso()))
    st.policy = pol
    src = st.sources_by_id()[source_id]
    src.status, src.error = "revoked", reason or "Revoked by user"
    self.ctx.emit("policy.updated", {"version": pol.version, "change": f"Revoked {source_id}",
                                     "rule_ids": [f"R-revoke-{source_id}"]}, message="revoked")
    self.ctx.emit("source.status", {"source_id": source_id, "status": "revoked", "stats": src.stats,
                                    "error": src.error}, message="revoked")
    return pol


def fake_kill_switch(self: Gateway, reason: str | None) -> None:
    pol = self.ctx.state.policy
    if pol is not None:
        pol.kill_switch = True
        pol.version += 1
        self.ctx.emit("policy.updated", {"version": pol.version, "change": "Kill switch on", "rule_ids": []},
                      message="kill switch")


async def hold(ctx: RunContext) -> None:
    """Stands in for collect: keeps the run alive until the kill switch."""
    while True:
        ctx.check_stop()
        await asyncio.sleep(0.01)


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setattr(Gateway, "build_policy", fake_build_policy)
    monkeypatch.setattr(Gateway, "revoke", fake_revoke)
    monkeypatch.setattr(Gateway, "set_kill_switch", fake_kill_switch)
    monkeypatch.setattr(orchestrator, "STAGES", [("plan", "plan"), ("approve", "approve"), ("collect", hold)])
    return monkeypatch


@pytest.fixture
def client(settings, patched):
    with TestClient(create_app(settings)) as c:
        yield c


def wait(client: TestClient, run_id: str, pred, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    run: dict = {}
    while time.monotonic() < deadline:
        run = client.get(f"{P}/runs/{run_id}").json()
        if pred(run):
            return run
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting, last run: {run}")


def new_run(client: TestClient, body: dict | None = None) -> str:
    r = client.post(f"{P}/runs", json=body or SHL)
    assert r.status_code == 201, r.text
    return m.Run.model_validate(r.json()).run_id


def awaiting(client: TestClient) -> str:
    run_id = new_run(client)
    wait(client, run_id, lambda r: r["status"] == "awaiting_approval")
    return run_id


def events(client: TestClient, run_id: str) -> list[m.Event]:
    page = m.EventsPage.model_validate(client.get(f"{P}/runs/{run_id}/events", params={"limit": 2000}).json())
    return list(page.events)


def error(r, status: int, code: str) -> m.ErrorBody:
    """The response is the contract error envelope with this status and code."""
    assert r.status_code == status, r.text
    body = m.ErrorResponse.model_validate(r.json()).error
    assert body.code == code, r.text
    return body


def approve_all(client: TestClient, run_id: str) -> m.ApprovalResponse:
    r = client.post(f"{P}/runs/{run_id}/approval", json={"decisions": [{"source_id": "S-001", "approval": "approved"}]})
    assert r.status_code == 200, r.text
    return m.ApprovalResponse.model_validate(r.json())


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_healthz_and_error_envelope(client):
    assert m.Health.model_validate(client.get("/healthz").json()) == m.Health(ok=True, version=CONTRACT_VERSION,
                                                                                mode="live")
    error(client.get(f"{P}/runs/run_20261107_nope00"), 404, "not_found")
    error(client.get(f"{P}/no/such/route"), 404, "not_found")
    error(client.post(f"{P}/runs", json={}), 422, "validation_error")
    error(client.post(f"{P}/runs", json={"company": "SHL", "typo": 1}), 422, "validation_error")
    run_id = awaiting(client)
    error(client.get(f"{P}/runs/{run_id}/events", params={"limit": 2001}), 422, "validation_error")
    error(client.get(f"{P}/runs/{run_id}/events", params={"after": -1}), 422, "validation_error")


def test_cors_any_origin(client):
    r = client.get(f"{P}/runs", headers={"Origin": "https://ui.example"})
    assert r.headers.get("access-control-allow-origin") in ("*", "https://ui.example")


def test_api_key_when_set(settings, patched):
    keyed = settings.model_copy(update={"api_key": "test-shared-key"})
    with TestClient(create_app(keyed)) as c:
        error(c.get(f"{P}/runs"), 401, "policy_denied")
        error(c.get(f"{P}/runs", headers={"X-Axelrod-Key": "wrong"}), 401, "policy_denied")
        assert c.get(f"{P}/runs", headers={"X-Axelrod-Key": "test-shared-key"}).status_code == 200
        assert c.get("/healthz").status_code == 200
        pre = c.options(f"{P}/runs", headers={"Origin": "https://ui.example", "Access-Control-Request-Method": "GET",
                                              "Access-Control-Request-Headers": "x-axelrod-key"})
        assert pre.status_code == 200


# ---------------------------------------------------------------------------
# the main flow: plan, add source, approval rules, revoke, comments, stop
# ---------------------------------------------------------------------------


def test_plan_streams_then_waits_for_approval(client):
    r = client.post(f"{P}/runs", json=SHL)
    assert r.status_code == 201
    run = m.Run.model_validate(r.json())
    assert (run.status, run.stage, run.mode, run.last_seq) == ("running", "plan", "live", 1)
    run = m.Run.model_validate(wait(client, run.run_id, lambda x: x["status"] == "awaiting_approval"))
    assert run.stage == "approve" and "Frozen demo plan" in (run.notes or "")
    plan = m.Plan.model_validate(client.get(f"{P}/runs/{run.run_id}/plan").json())
    assert plan.ready and [n.need_id for n in plan.needs] == ["N-01", "N-02"]
    assert [s.source_id for s in plan.sources] == ["S-001", "S-002"]
    evs = events(client, run.run_id)
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
    types = [e.type for e in evs]
    assert types[:2] == ["run.created", "run.status"]
    assert types.count("plan.need") == 2 and types.count("plan.source") == 2 and "plan.ready" in types
    assert evs[-1].type == "run.status" and evs[-1].data.status == "awaiting_approval"
    ready = next(e for e in evs if e.type == "plan.ready")
    assert "Frozen demo plan" in ready.message
    assert plan.as_of_seq == run.last_seq or plan.as_of_seq >= run.last_seq


def test_full_flow(client, settings):
    run_id = awaiting(client)
    error(client.post(f"{P}/runs/{run_id}/sources/S-001/revoke", json={}), 409, "invalid_state")

    # analyst adds a source with an api key: stored server-side, never echoed
    r = client.post(f"{P}/runs/{run_id}/sources", json={"url": "https://extra.data.test/v1/report", "title": "Extra",
                                                       "tier": "tier_3", "kind": "data_api", "api_key": FAKE_KEY})
    assert r.status_code == 201, r.text
    added = m.Source.model_validate(r.json())
    assert added.source_id == "S-003" and added.proposed_by == "user" and added.approval == "approved"
    assert added.access.auth == "api_key" and added.access.secret_ref and FAKE_KEY not in r.text
    assert client.app.state.rt.store.get_secret(added.access.secret_ref) == FAKE_KEY
    error(client.post(f"{P}/runs/{run_id}/sources", json={"url": "ftp://x.test/a"}), 422, "validation_error")
    error(client.post(f"{P}/runs/{run_id}/sources", json={"url": "https://u:p@x.test/a"}), 422, "validation_error")

    # approval rules
    err = error(client.post(f"{P}/runs/{run_id}/approval", json={"decisions": []}), 422, "validation_error")
    assert err.details == {"undecided": ["S-001"]}
    err = error(client.post(f"{P}/runs/{run_id}/approval",
                            json={"decisions": [{"source_id": "S-001", "approval": "approved"},
                                                {"source_id": "S-099", "approval": "approved"}]}),
                422, "validation_error")
    assert err.details == {"unknown": ["S-099"]}
    resp = approve_all(client, run_id)
    assert resp.policy.version == 1 and resp.policy.default_effect == "deny"
    assert {"R-allow-S-001", "R-allow-S-002", "R-allow-S-003"} <= {x.rule_id for x in resp.policy.rules}
    assert resp.run.status == "running"
    error(client.post(f"{P}/runs/{run_id}/approval", json={"decisions": []}), 409, "invalid_state")
    error(client.post(f"{P}/runs/{run_id}/sources", json={"url": "https://late.test/"}), 409, "invalid_state")
    wait(client, run_id, lambda x: x["stage"] == "collect")
    evs = events(client, run_id)
    committed = next(e for e in evs if e.type == "approval.committed")
    assert committed.data.approved == ["S-001", "S-003"] and committed.data.auto_approved == ["S-002"]
    assert [e.data.version for e in evs if e.type == "policy.updated"] == [1]
    plan = m.Plan.model_validate(client.get(f"{P}/runs/{run_id}/plan").json())
    assert {s.source_id: s.approval for s in plan.sources} == {"S-001": "approved", "S-002": "auto_approved",
                                                              "S-003": "approved"}
    run = m.Run.model_validate(client.get(f"{P}/runs/{run_id}").json())
    assert run.counts.sources_approved == 3 and run.policy_version == 1

    # revoke after approval
    r = client.post(f"{P}/runs/{run_id}/sources/S-002/revoke", json={"reason": "demo revoke"})
    assert r.status_code == 200, r.text
    rev = m.RevokeResponse.model_validate(r.json())
    assert rev.source.status == "revoked" and rev.source.error == "demo revoke"
    assert rev.policy.version == 2 and rev.policy.rules[0].rule_id == "R-revoke-S-002"
    error(client.post(f"{P}/runs/{run_id}/sources/S-002/revoke", json={}), 409, "conflict")
    error(client.post(f"{P}/runs/{run_id}/sources/S-404/revoke", json={}), 404, "not_found")
    m.Policy.model_validate(client.get(f"{P}/runs/{run_id}/policy").json())

    # comments
    r = client.post(f"{P}/runs/{run_id}/comments", json={"target_type": "run", "text": "Check the FY base"})
    assert r.status_code == 201
    assert m.Comment.model_validate(r.json()).comment_id == "CM-001"
    error(client.post(f"{P}/runs/{run_id}/comments", json={"target_type": "item", "text": "x"}), 422, "validation_error")
    error(client.post(f"{P}/runs/{run_id}/comments", json={"target_type": "item", "target_id": "D-404", "text": "x"}),
          404, "not_found")

    # stop
    r = client.post(f"{P}/runs/{run_id}/stop", json={"reason": "demo stop"})
    assert r.status_code == 200, r.text
    run = m.Run.model_validate(r.json())
    assert run.status == "stopped" and run.stop_reason == "demo stop"
    time.sleep(0.1)
    assert client.get(f"{P}/runs/{run_id}").json()["status"] == "stopped"
    pol = m.Policy.model_validate(client.get(f"{P}/runs/{run_id}/policy").json())
    assert pol.kill_switch and pol.version == 3
    error(client.post(f"{P}/runs/{run_id}/stop", json={}), 409, "invalid_state")
    comments = m.CommentList.model_validate(client.get(f"{P}/runs/{run_id}/comments").json()).comments
    assert comments[0].status == "rejected"
    late = m.Comment.model_validate(client.post(f"{P}/runs/{run_id}/comments",
                                                json={"target_type": "run", "text": "late"}).json())
    assert late.status == "rejected"
    evs = events(client, run_id)
    assert [e.type for e in evs].count("run.stopped") == 1
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))

    # every read route answers a contract shape
    for path, model in (("report", m.AgentReport), ("summary", m.HumanReport), ("agents", m.AgentList),
                        ("documents", m.DocumentList), ("vault/tree", m.VaultNode), ("comments", m.CommentList)):
        r = client.get(f"{P}/runs/{run_id}/{path}")
        assert r.status_code == 200, (path, r.text)
        model.model_validate(r.json())
    tree = m.VaultNode.model_validate(client.get(f"{P}/runs/{run_id}/vault/tree").json())
    plan_path = f"{tree.path}/00-Meta/_plan.json"
    vf = m.VaultFile.model_validate(client.get(f"{P}/runs/{run_id}/vault/file", params={"path": plan_path}).json())
    assert vf.content_type == "application/json" and '"frozen": true' in (vf.text or "")
    error(client.get(f"{P}/runs/{run_id}/vault/file", params={"path": "../../state.json"}), 404, "not_found")
    assert run_id in [x.run_id for x in m.RunList.model_validate(client.get(f"{P}/runs").json()).runs]

    # the api key never reached disk
    for f in Path(settings.data_dir).rglob("*"):
        if f.is_file():
            assert FAKE_KEY not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_stop_while_awaiting_approval(client):
    run_id = awaiting(client)
    run = m.Run.model_validate(client.post(f"{P}/runs/{run_id}/stop", json={}).json())
    assert run.status == "stopped" and run.stop_reason == "Stopped by user"
    time.sleep(0.1)
    assert client.get(f"{P}/runs/{run_id}").json()["status"] == "stopped"
    error(client.post(f"{P}/runs/{run_id}/approval", json={"decisions": []}), 409, "invalid_state")
    pol = m.Policy.model_validate(client.get(f"{P}/runs/{run_id}/policy").json())
    assert pol.kill_switch and pol.version == 0
    assert [e.type for e in events(client, run_id)].count("run.stopped") == 1


def test_stop_without_body(client):
    run_id = awaiting(client)
    assert client.post(f"{P}/runs/{run_id}/stop").status_code == 200


def test_other_company_fails(client):
    run_id = new_run(client, {"company": "Philips", "ticker": "PHIA"})
    run = wait(client, run_id, lambda r: r["status"] == "failed")
    assert "only the frozen Siemens Healthineers plan is available in this build" in run["error"]
    assert events(client, run_id)[-1].type == "run.failed"


def test_unbuilt_stage_fails_the_run(client, patched):
    async def stub(ctx):
        raise NotImplementedError("B-ingest")

    patched.setattr(orchestrator, "STAGES", [("plan", "plan"), ("approve", "approve"), ("collect", stub)])
    run_id = awaiting(client)
    approve_all(client, run_id)
    run = wait(client, run_id, lambda r: r["status"] == "failed")
    assert run["error"] == "stage collect is not built yet (B-ingest)" and run["stage"] == "collect"


def test_run_completes_when_stages_end(client, patched):
    patched.setattr(orchestrator, "STAGES", [("plan", "plan"), ("approve", "approve")])
    run_id = awaiting(client)
    approve_all(client, run_id)
    run = wait(client, run_id, lambda r: r["status"] == "done")
    assert run["stage"] == "complete"
    assert events(client, run_id)[-1].type == "run.completed"
    error(client.post(f"{P}/runs/{run_id}/stop", json={}), 409, "invalid_state")


def test_gateway_not_built_answers_409(client, patched):
    def not_built(self, *, emit: bool = True):
        raise NotImplementedError("B-gateway")

    patched.setattr(Gateway, "build_policy", not_built)
    run_id = awaiting(client)
    err = error(client.post(f"{P}/runs/{run_id}/approval",
                            json={"decisions": [{"source_id": "S-001", "approval": "approved"}]}), 409, "invalid_state")
    assert "not built yet" in err.message
    plan = m.Plan.model_validate(client.get(f"{P}/runs/{run_id}/plan").json())
    assert all(s.approval == "pending" for s in plan.sources)
    assert client.get(f"{P}/runs/{run_id}").json()["status"] == "awaiting_approval"


def test_with_the_real_gateway(settings, monkeypatch):
    """No gateway fakes: approval, revoke and stop through B-gateway's policy code."""
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setattr(orchestrator, "STAGES", [("plan", "plan"), ("approve", "approve"), ("collect", hold)])
    with TestClient(create_app(settings)) as c:
        run_id = awaiting(c)
        seq0 = c.get(f"{P}/runs/{run_id}").json()["last_seq"]
        resp = approve_all(c, run_id)
        assert {"R-sys-models", "R-allow-S-001", "R-allow-S-002"} <= {x.rule_id for x in resp.policy.rules}
        new = [e for e in events(c, run_id) if e.seq > seq0]
        assert [e.type for e in new][:2] == ["approval.committed", "policy.updated"]
        assert [e.type for e in new].count("policy.updated") == 1
        seq1 = new[-1].seq
        rev = m.RevokeResponse.model_validate(c.post(f"{P}/runs/{run_id}/sources/S-002/revoke",
                                                     json={"reason": "demo"}).json())
        assert rev.source.status == "revoked" and any(r.rule_id == "R-revoke-S-002" for r in rev.policy.rules)
        types = [e.type for e in events(c, run_id) if e.seq > seq1]
        assert types.count("policy.updated") == 1 and types.count("source.status") == 1
        error(c.post(f"{P}/runs/{run_id}/sources/S-002/revoke", json={}), 409, "conflict")
        stopped = m.Run.model_validate(c.post(f"{P}/runs/{run_id}/stop", json={}).json())
        assert stopped.status == "stopped"
        assert m.Policy.model_validate(c.get(f"{P}/runs/{run_id}/policy").json()).kill_switch
        time.sleep(0.1)
        assert c.get(f"{P}/runs/{run_id}").json()["status"] == "stopped"
        # a stop before approval also gives the run a (kill-switch) policy; revoke still says "after approval"
        other = awaiting(c)
        c.post(f"{P}/runs/{other}/stop", json={})
        error(c.post(f"{P}/runs/{other}/sources/S-001/revoke", json={}), 409, "invalid_state")


def test_tier_move_and_rank_at_approval(client):
    run_id = awaiting(client)
    r = client.post(f"{P}/runs/{run_id}/approval", json={"decisions": [
        {"source_id": "S-001", "approval": "approved", "tier": "tier_2", "rank": 1},
        {"source_id": "S-002", "approval": "rejected"}]})
    assert r.status_code == 200, r.text
    plan = m.Plan.model_validate(client.get(f"{P}/runs/{run_id}/plan").json())
    by = {s.source_id: s for s in plan.sources}
    assert (by["S-001"].tier, by["S-001"].rank, by["S-002"].rank) == ("tier_2", 1, 2)
    assert by["S-002"].approval == "rejected" and by["S-002"].status == "skipped"
    rule_ids = {x.rule_id for x in m.Policy.model_validate(client.get(f"{P}/runs/{run_id}/policy").json()).rules}
    assert {"R-allow-S-001", "R-reject-S-002"} <= rule_ids


# ---------------------------------------------------------------------------
# documents, items and demo controls (other plans' code, called through the API)
# ---------------------------------------------------------------------------


def _add_document(client: TestClient, run_id: str) -> None:
    state = client.app.state.rt.store.get(run_id)
    state.documents.append(m.Document(
        doc_id="DOC-001", source_id="S-001", title="t", url="https://ir.healthineers-sample.test/a.pdf",
        content_type="pdf", paginated=True, pages=2, sha256="0" * 64, bytes=10, fetched_at=now_iso(),
        vault_paths=m.DocVaultPaths(raw="r", text="t", meta="x"), scan=m.ScanResult(scanner="none", verdict="skipped")))
    state.pages["DOC-001"] = ["page one", "page two"]


def test_document_routes(client, patched):
    run_id = awaiting(client)
    _add_document(client, run_id)
    docs = m.DocumentList.model_validate(client.get(f"{P}/runs/{run_id}/documents").json())
    assert [d.doc_id for d in docs.documents] == ["DOC-001"]
    m.Document.model_validate(client.get(f"{P}/runs/{run_id}/documents/DOC-001").json())
    error(client.get(f"{P}/runs/{run_id}/documents/DOC-404"), 404, "not_found")
    error(client.get(f"{P}/runs/{run_id}/documents/DOC-001/pages/3"), 404, "not_found")
    error(client.get(f"{P}/runs/{run_id}/documents/DOC-001/pages/0"), 404, "not_found")

    def not_built(state, doc_id, page):
        raise NotImplementedError("B-ingest")

    patched.setattr(ingest, "document_page", not_built)
    assert "not built yet" in error(client.get(f"{P}/runs/{run_id}/documents/DOC-001/pages/1"), 409,
                                    "invalid_state").message

    def built(state, doc_id, page):
        return m.DocumentPage(doc_id=doc_id, page=page, pages=2, content_type="pdf", text=state.pages[doc_id][page - 1],
                              highlights=[])

    patched.setattr(ingest, "document_page", built)
    page = m.DocumentPage.model_validate(client.get(f"{P}/runs/{run_id}/documents/DOC-001/pages/2").json())
    assert page.text == "page two"


def test_item_and_demo_routes(client, patched, fixture_report):
    run_id = awaiting(client)
    state = client.app.state.rt.store.get(run_id)
    chart = next(i for i in fixture_report.items if i.kind == "chart")
    data = next(i for i in fixture_report.items if i.kind == "data")
    state.items[chart.id] = chart
    state.items[data.id] = data

    def not_built(*args, **kwargs):
        raise NotImplementedError("B-cli")

    patched.setattr(demo, "patch_item", not_built)
    patched.setattr(demo, "inject_errors", not_built)
    error(client.patch(f"{P}/runs/{run_id}/items/{data.id}", json={"text": "x"}), 409, "invalid_state")
    error(client.patch(f"{P}/runs/{run_id}/items/{chart.id}", json={"text": "x"}), 422, "validation_error")
    error(client.patch(f"{P}/runs/{run_id}/items/D-999", json={"text": "x"}), 404, "not_found")
    error(client.post(f"{P}/runs/{run_id}/demo/inject-errors", json={"count": 2}), 409, "invalid_state")

    async def fake_patch(ctx, item_id, patch):
        return ctx.state.items[item_id].model_copy(update={"statement": patch.text})

    async def fake_inject(ctx, req):
        return m.InjectErrorsResponse(item_ids=[data.id], note="Demo control (test)")

    patched.setattr(demo, "patch_item", fake_patch)
    patched.setattr(demo, "inject_errors", fake_inject)
    r = client.patch(f"{P}/runs/{run_id}/items/{data.id}", json={"text": "edited"})
    assert r.status_code == 200 and r.json()["statement"] == "edited" and r.json()["kind"] == "data"
    r = client.post(f"{P}/runs/{run_id}/demo/inject-errors", json={"count": 1})
    assert m.InjectErrorsResponse.model_validate(r.json()).item_ids == [data.id]
    r = client.post(f"{P}/runs/{run_id}/comments", json={"target_type": "item", "target_id": data.id, "text": "why?"})
    assert r.status_code == 201


def test_events_paging_via_api(client):
    run_id = awaiting(client)
    total = len(events(client, run_id))
    page = m.EventsPage.model_validate(client.get(f"{P}/runs/{run_id}/events", params={"after": 2, "limit": 3}).json())
    assert [e.seq for e in page.events] == [3, 4, 5] and page.last_seq == 5 and page.run_status == "awaiting_approval"
    page = m.EventsPage.model_validate(client.get(f"{P}/runs/{run_id}/events", params={"after": total}).json())
    assert page.events == [] and page.last_seq == total


# ---------------------------------------------------------------------------
# restart and replay
# ---------------------------------------------------------------------------


def _finished_run(store: Store, fixture_report: m.AgentReport) -> str:
    state = store.create_run(m.CreateRunRequest(**SHL))
    ctx = RunContext.create(state, store, store.vault, store.settings, gateway=object())  # type: ignore[arg-type]
    ctx.emit("run.created", {"run": state.run}, message="created")
    for item in fixture_report.items:
        state.items[item.id] = item
    state.rollup, state.report_certainty, state.holes = fixture_report.rollup, fixture_report.certainty, \
        list(fixture_report.holes)
    state.report_generated_at = now_iso()
    ctx.emit("report.ready", {"items": len(state.items), "passed": 1, "fixed": 0, "failed": 0, "holes": 0},
             message="ready", stage="repair")
    ctx.emit("run.status", {"status": "done", "stage": "complete"}, message="done", stage="complete")
    ctx.emit("run.completed", {}, message="complete", stage="complete")
    store.vault.put_text(state.run.run_id, "09-Analysis/agent-report.json", state.report().model_dump_json())
    store.save(state)
    return state.run.run_id


def test_finished_run_survives_restart(settings, patched, fixture_report):
    run_id = _finished_run(Store(settings), fixture_report)
    with TestClient(create_app(settings)) as c:            # a fresh app over the same DATA_DIR
        runs = m.RunList.model_validate(c.get(f"{P}/runs").json()).runs
        assert [r.run_id for r in runs] == [run_id] and runs[0].status == "done"
        rep = m.AgentReport.model_validate(c.get(f"{P}/runs/{run_id}/report").json())
        assert rep.status == "final" and len(rep.items) == len(fixture_report.items) and rep.rollup is not None
        page = m.EventsPage.model_validate(c.get(f"{P}/runs/{run_id}/events").json())
        assert page.run_status == "done" and page.events[-1].type == "run.completed"
        tree = m.VaultNode.model_validate(c.get(f"{P}/runs/{run_id}/vault/tree").json())
        analysis = next(n for n in tree.children or [] if n.name == "09-Analysis")
        assert [n.name for n in analysis.children or []] == ["agent-report.json"]


def test_live_run_survives_restart_as_failed(settings, patched):
    with TestClient(create_app(settings)) as c:
        run_id = awaiting(c)
    with TestClient(create_app(settings)) as c:
        run = c.get(f"{P}/runs/{run_id}").json()
        assert run["status"] == "failed" and "Interrupted" in run["error"]
        plan = m.Plan.model_validate(c.get(f"{P}/runs/{run_id}/plan").json())
        assert plan.ready and len(plan.sources) == 2


def test_replay_folder_loads_as_replay(settings, patched, fixture_report, tmp_path):
    store = Store(settings)
    run_id = _finished_run(store, fixture_report)
    store.export_run(run_id, tmp_path / "replay" / "shl-demo")
    other = load_settings(env_file=None, environ={"STORE": "local", "DATA_DIR": str(tmp_path / "fresh")})
    with TestClient(create_app(other, store=Store(other, replay_dir=tmp_path / "replay"))) as c:
        runs = m.RunList.model_validate(c.get(f"{P}/runs").json()).runs
        assert [(r.run_id, r.mode) for r in runs] == [(run_id, "replay")]
        assert m.AgentReport.model_validate(c.get(f"{P}/runs/{run_id}/report").json()).status == "final"
        tree = c.get(f"{P}/runs/{run_id}/vault/tree").json()
        vf = c.get(f"{P}/runs/{run_id}/vault/file",
                   params={"path": f"{tree['path']}/09-Analysis/agent-report.json"})
        assert vf.status_code == 200 and m.VaultFile.model_validate(vf.json()).text
