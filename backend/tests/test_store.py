"""Store, events, vault, the plan stage and the approval rules (B-store-api). Offline, except the live round trip."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import threading
from pathlib import Path

import pytest

from backend.axelrod import ids as axids
from backend.axelrod.config import load_settings
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext, StopRequested
from backend.axelrod.stages import approve as approve_stage
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import INTERRUPTED, RunState, Store
from backend.axelrod.store import events as ev
from backend.axelrod.store.vault import FOLDERS, TEXT_LIMIT, Vault, company_slug, register_run

SAMPLE_PLAN = Path(__file__).parent / "data" / "store" / "plan.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_ctx(store: Store, company: str = "Siemens Healthineers AG", ticker: str | None = "SHL") -> RunContext:
    state = store.create_run(m.CreateRunRequest(company=company, ticker=ticker, created_via="api"))
    return RunContext.create(state, store, store.vault, store.settings, gateway=object())  # type: ignore[arg-type]


def log_event(ctx: RunContext, i: int) -> m.Event:
    return ctx.emit("agent.log", {"text": f"line {i}", "transcript_path": None}, message=f"log {i}")


def sample_sources() -> list[m.Source]:
    raw = json.loads(SAMPLE_PLAN.read_text(encoding="utf-8"))
    return [m.Source.model_validate(s) for s in raw["sources"]]


def finished_state(store: Store, fixture_report: m.AgentReport) -> RunState:
    """A done run with realistic items (fixture items: fictional company)."""
    ctx = make_ctx(store)
    ctx.emit("run.created", {"run": ctx.state.run}, message="created")
    for item in fixture_report.items:
        ctx.state.items[item.id] = item
    ctx.state.holes = list(fixture_report.holes)
    ctx.state.rollup = fixture_report.rollup
    ctx.state.report_certainty = fixture_report.certainty
    ctx.state.report_generated_at = axids.now_iso()
    ctx.emit("report.ready", {"items": len(fixture_report.items), "passed": 1, "fixed": 0, "failed": 0, "holes": 0},
             message="report ready", stage="repair")
    ctx.emit("run.status", {"status": "done", "stage": "complete"}, message="done", stage="complete")
    ctx.emit("run.completed", {}, message="complete", stage="complete")
    return ctx.state


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_emit_assigns_seq_and_updates_run(settings):
    store = Store(settings)
    ctx = make_ctx(store)
    e1 = ctx.emit("run.created", {"run": ctx.state.run}, message="created", stage="plan")
    e2 = ctx.emit("run.status", {"status": "running", "stage": "collect"}, message="collect", stage="collect")
    assert (e1.seq, e2.seq) == (1, 2)
    assert ctx.state.run.last_seq == 2
    assert ctx.state.run.updated_at == e2.ts
    assert (ctx.state.run.status, ctx.state.run.stage) == ("running", "collect")
    assert e1.data.run.last_seq == 0                      # a snapshot from before the event
    assert e1.ts.endswith("Z")


def test_emit_rejects_bad_events(settings):
    ctx = make_ctx(Store(settings))
    with pytest.raises(ValueError):
        ctx.emit("no.such.type", {}, message="x")
    with pytest.raises(ValueError):
        ctx.emit("run.status", {"status": "running", "stage": "plan", "typo": 1}, message="x")
    assert ctx.state.events == [] and ctx.state.run.last_seq == 0


def test_event_data_is_a_snapshot(settings):
    ctx = make_ctx(Store(settings))
    src = sample_sources()[0]
    ctx.state.sources.append(src)
    e = ctx.emit("plan.source", {"source": src}, message="s")
    src.approval = "approved"
    assert e.data.source.approval == "pending"


async def test_200_concurrent_emits_are_gapless(settings):
    ctx = make_ctx(Store(settings))

    async def one(i: int) -> None:
        await asyncio.sleep(0.001 * (i % 7))
        log_event(ctx, i)

    await asyncio.gather(*(one(i) for i in range(100)))
    await asyncio.gather(*(asyncio.to_thread(log_event, ctx, 100 + i) for i in range(100)))
    seqs = [e.seq for e in ctx.state.events]
    assert seqs == list(range(1, 201))
    assert ctx.state.run.last_seq == 200
    lines = (Path(settings.data_dir) / ctx.vault.root(ctx.run_id) / "00-Meta" / "events.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert [json.loads(x)["seq"] for x in lines] == list(range(1, 201))


def test_200_threaded_emits_are_gapless(settings):
    ctx = make_ctx(Store(settings))
    threads = [threading.Thread(target=log_event, args=(ctx, i)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ev.gapless(ctx.state.events) and len(ctx.state.events) == 200


def test_events_page_rules(settings):
    ctx = make_ctx(Store(settings))
    for i in range(30):
        log_event(ctx, i)
    page = ev.events_page(ctx.state)
    assert len(page.events) == 30 and page.last_seq == 30 and page.run_status == "running"
    page = ev.events_page(ctx.state, after=10, limit=5)
    assert [e.seq for e in page.events] == [11, 12, 13, 14, 15] and page.last_seq == 15
    page = ev.events_page(ctx.state, after=30)
    assert page.events == [] and page.last_seq == 30
    page = ev.events_page(ctx.state, after=99)
    assert page.events == [] and page.last_seq == 99          # echoes after, like the mock
    for i in range(2100):
        log_event(ctx, i)
    assert len(ev.events_page(ctx.state, limit=5000).events) == 2000


def test_counts_follow_state_and_gateway_events(settings):
    ctx = make_ctx(Store(settings))
    ctx.state.sources = sample_sources()
    ctx.state.sources[0].approval, ctx.state.sources[0].status = "approved", "fetched"
    ctx.state.agents["fetcher-01"] = m.Agent(agent_id="fetcher-01", role="fetcher", status="working", task="fetch")
    for decision in ("allowed", "allowed", "blocked", "flagged"):
        ctx.emit("gateway.request", {"request_id": ctx.next_id("REQ"), "role": "fetcher", "action": "fetch",
                                     "target": "https://x.test/", "decision": decision, "rule_id": None,
                                     "reason": "test", "source_id": None, "latency_ms": 1}, message="gw")
    c = ctx.state.run.counts
    assert (c.sources_total, c.sources_approved, c.sources_fetched) == (2, 1, 1)
    assert (c.agents_total, c.agents_active) == (1, 1)
    assert (c.gateway_allowed, c.gateway_blocked, c.gateway_flagged) == (2, 1, 1)
    c.gateway_allowed = 0
    ctx.state.recount_gateway()
    assert c.gateway_allowed == 2


def test_usage_fold_keeps_the_same_object(settings):
    ctx = make_ctx(Store(settings))
    usage = ctx.state.run.usage
    ctx.emit("usage.updated", {"usage": {"model_calls": 3, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.01,
                                         "budget_usd": None}}, message="usage")
    assert ctx.state.run.usage is usage and usage.model_calls == 3 and usage.budget_usd == 5.0


def test_check_stop(settings):
    ctx = make_ctx(Store(settings))
    ctx.check_stop()
    ctx.state.kill_switch = True
    ctx.state.run.stop_reason = "demo"
    with pytest.raises(StopRequested, match="demo"):
        ctx.check_stop()


def test_next_id_formats(settings):
    ctx = make_ctx(Store(settings))
    assert [ctx.next_id("D"), ctx.next_id("D"), ctx.next_id("CM"), ctx.next_id("REQ")] == \
        ["D-001", "D-002", "CM-001", "REQ-0001"]


# ---------------------------------------------------------------------------
# state views
# ---------------------------------------------------------------------------


def test_report_view_hides_final_parts_until_ready(settings, fixture_report):
    ctx = make_ctx(Store(settings))
    for item in fixture_report.items:
        ctx.state.items[item.id] = item
    ctx.state.rollup = fixture_report.rollup
    ctx.state.holes = list(fixture_report.holes)
    ctx.emit("run.status", {"status": "running", "stage": "verify"}, message="v", stage="verify")
    rep = ctx.state.report()
    assert rep.status == "verifying" and rep.rollup is None and rep.holes == []
    assert {s.category for s in rep.sections} <= {c for c in m.Category.__args__}  # type: ignore[attr-defined]
    assert sum(len(s.item_ids) for s in rep.sections) == len(fixture_report.items)
    ctx.emit("report.ready", {"items": 1, "passed": 1, "fixed": 0, "failed": 0, "holes": 0}, message="r")
    rep = ctx.state.report()
    assert rep.status == "final" and rep.rollup is not None and rep.holes
    assert len(ctx.state.data_items()) + len(ctx.state.conclusions()) + len(ctx.state.extrapolations()) + \
        len(ctx.state.charts()) == len(fixture_report.items)
    m.AgentReport.model_validate(rep.model_dump())


def test_agent_pages_empty_for_quarantined(settings):
    ctx = make_ctx(Store(settings))
    scan = m.ScanResult(scanner="bedrock_guardrails", verdict="flagged")
    paths = m.DocVaultPaths(raw="r", text="t", meta="x")
    for doc_id, quarantined in (("DOC-001", False), ("DOC-002", True)):
        ctx.state.documents.append(m.Document(doc_id=doc_id, source_id="S-001", title="t", url="https://x.test/",
                                              content_type="html", paginated=False, pages=1, sha256="0", bytes=1,
                                              fetched_at=axids.now_iso(), vault_paths=paths,
                                              quarantined=quarantined, scan=scan))
        ctx.state.pages[doc_id] = ["page one"]
    assert ctx.state.agent_pages("DOC-001") == ["page one"]
    assert ctx.state.agent_pages("DOC-002") == []
    assert ctx.state.agent_pages("DOC-404") == []


def test_policy_and_summary_placeholders(settings):
    ctx = make_ctx(Store(settings))
    pol = ctx.state.policy_view()
    assert pol.version == 0 and [r.rule_id for r in pol.rules] == ["R-sys-models"] and pol.default_effect == "deny"
    brief = ctx.state.human_report()
    assert brief.status == "not_started" and brief.sections == []


def test_pre_approval_policy_shows_what_the_gateway_enforces(settings):
    """B-g2fix finding 4: before approval GET /policy shows R-sys-models and the role scopes, as the mock does."""
    from fastapi.testclient import TestClient

    from backend.axelrod.api import create_app

    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api"))
    ctx = RunContext.create(state, store, store.vault, settings)          # the real Gateway
    view = state.policy_view()
    enforced = ctx.gateway._policy()
    assert view.version == 0 and view.default_effect == "deny" and not view.kill_switch
    assert view.rules == enforced.rules and view.role_scopes == enforced.role_scopes
    (sys_rule,) = view.rules
    assert (sys_rule.rule_id, sys_rule.effect, sys_rule.action, sys_rule.origin) == (
        "R-sys-models", "allow", "model.invoke", "system")
    scopes = {s.role: s for s in view.role_scopes}
    assert set(scopes) == {"orchestrator", "planner", "fetcher", "extractor", "drafter", "verifier", "repairer",
                           "summarizer", "reviser"}
    assert scopes["planner"].models == [settings.model_large] and scopes["fetcher"].actions == ["fetch"]
    assert scopes["orchestrator"].actions == ["model.invoke"] and scopes["orchestrator"].models == [settings.model_large]
    assert view.budget_usd == settings.budget_usd
    # The view matches what decide does: the planner may call its model, every fetch is default deny.
    planner = m.Agent(agent_id="planner-01", role="planner", status="working", task="t")
    fetcher = m.Agent(agent_id="fetcher-01", role="fetcher", status="working", task="t")
    assert ctx.gateway.decide(planner, "model.invoke", settings.model_large)[:2] == ("allowed", "R-sys-models")
    assert ctx.gateway.decide(fetcher, "fetch", "https://www.siemens-healthineers.com/")[:2] == ("blocked", None)
    # Same answer through the API route.
    with TestClient(create_app(settings, store=store)) as client:
        r = client.get(f"/api/v1/runs/{state.run.run_id}/policy")
    assert r.status_code == 200, r.text
    api_view = m.Policy.model_validate(r.json())
    assert api_view.rules == view.rules and api_view.role_scopes == view.role_scopes and api_view.version == 0
    # After approval the built policy replaces the base.
    ctx.gateway.build_policy()
    assert state.policy_view().version == 1 and state.policy_view().rules[0].rule_id == "R-sys-models"


def test_fake_model_runs_are_labeled_simulated(settings):
    """B-g2fix finding 6: FAKE_MODELS=1 puts a Simulated line in Run.notes. mode stays live."""
    from backend.axelrod.store import FAKE_MODELS_NOTE

    fake = Store(settings)
    plain = fake.create_run(m.CreateRunRequest(company="Siemens Healthineers AG"))
    noted = fake.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", notes="analyst note"))
    assert settings.fake_models and plain.run.mode == "live"
    assert plain.run.notes == FAKE_MODELS_NOTE == "Simulated: model answers come from offline fakes (FAKE_MODELS=1)"
    assert noted.run.notes == f"analyst note | {FAKE_MODELS_NOTE}"
    real = Store(settings.model_copy(update={"fake_models": False}))
    assert real.create_run(m.CreateRunRequest(company="Siemens Healthineers AG")).run.notes is None
    assert real.create_run(m.CreateRunRequest(company="X", notes="analyst note")).run.notes == "analyst note"


# ---------------------------------------------------------------------------
# persistence: save, restart, replay
# ---------------------------------------------------------------------------


def test_save_and_reload_local(settings, fixture_report):
    store = Store(settings)
    state = finished_state(store, fixture_report)
    store.save(state)
    again = Store(settings)
    again.load_all()
    loaded = again.get(state.run.run_id)
    assert loaded.run == state.run
    assert loaded.events == state.events
    assert set(loaded.items) == set(state.items)
    assert loaded.report().status == "final"
    assert [r.run_id for r in again.list_runs()] == [state.run.run_id]


def test_interrupted_run_is_marked_failed_on_load(settings):
    store = Store(settings)
    ctx = make_ctx(store)
    ctx.emit("run.created", {"run": ctx.state.run}, message="created")
    ctx.emit("run.status", {"status": "awaiting_approval", "stage": "approve"}, message="wait", stage="approve")
    store.save(ctx.state)
    again = Store(settings)
    again.load_all()
    loaded = again.get(ctx.run_id)
    assert loaded.run.status == "failed" and loaded.run.error == INTERRUPTED
    assert [e.type for e in loaded.events][-2:] == ["run.status", "run.failed"]
    assert ev.gapless(loaded.events)


def test_busy_agents_of_a_stopped_run_settle_on_load(settings):
    store = Store(settings)
    ctx = make_ctx(store)
    ctx.state.agents["fetcher-01"] = m.Agent(agent_id="fetcher-01", role="fetcher", status="working", task="fetch")
    ctx.emit("run.status", {"status": "stopped", "stage": "collect"}, message="stop", stage="collect")
    ctx.emit("run.stopped", {"reason": "demo"}, message="stopped", stage="collect")
    store.save(ctx.state)                                       # the process dies here, mid-fetch
    again = Store(settings)
    again.load_all()
    loaded = again.get(ctx.run_id)
    assert loaded.run.status == "stopped" and loaded.agents["fetcher-01"].status == "stopped"
    assert loaded.events[-1].type == "agent.status" and loaded.run.counts.agents_active == 0
    third = Store(settings)
    third.load_all()                                            # settled once, not on every load
    assert len(third.get(ctx.run_id).events) == len(loaded.events)


def test_export_and_replay_load(settings, fixture_report, tmp_path):
    store = Store(settings)
    state = finished_state(store, fixture_report)
    store.vault.put_text(state.run.run_id, "00-Meta/_plan.json", "{}")
    store.vault.put_bytes(state.run.run_id, "01-Annual-Reports/report.pdf", b"%PDF-1.7 binary")
    store.save(state)
    replay_dir = tmp_path / "replay"
    store.export_run(state.run.run_id, replay_dir / "shl-demo")
    exported = replay_dir / "shl-demo"
    assert (exported / "state.json").is_file() and (exported / "events.jsonl").is_file()
    assert (exported / "vault" / "00-Meta" / "_plan.json").is_file()
    assert not list(exported.rglob("*.pdf"))

    fresh = load_settings(env_file=None, environ={"STORE": "local", "DATA_DIR": str(tmp_path / "other")})
    other = Store(fresh, replay_dir=replay_dir)
    other.load_all()
    replay = other.get(state.run.run_id)
    assert replay.run.mode == "replay" and replay.run.status == "done"
    assert len(replay.events) == len(state.events) and replay.run.last_seq == state.run.last_seq
    vault = Vault(fresh)
    assert vault.get_text(state.run.run_id, "00-Meta/_plan.json") == "{}"
    names = {n.name for n in vault.tree(state.run.run_id).children or []}
    assert "00-Meta" in names


def test_parse_jsonl_stops_at_a_torn_line(settings):
    ctx = make_ctx(Store(settings))
    for i in range(3):
        log_event(ctx, i)
    text = ev.to_jsonl(ctx.state.events) + '{"seq": 4, "ts": "2026'
    assert [e.seq for e in ev.parse_jsonl(text)] == [1, 2, 3]


# ---------------------------------------------------------------------------
# vault
# ---------------------------------------------------------------------------


def test_company_slug():
    assert company_slug("Siemens Healthineers AG") == "siemens-healthineers"
    assert company_slug("Northwind Instruments (fixture)") == "northwind-instruments"
    assert company_slug("AG") == "ag"
    assert company_slug("???") == "company"


def test_vault_local_put_get_tree_file(settings):
    vault = Vault(settings)
    run_id = "run_20261107_vault1"
    root = register_run(run_id, "Siemens Healthineers AG")
    assert root == f"runs/{run_id}/siemens-healthineers"
    path = vault.put_text(run_id, "00-Meta/_plan.json", '{"a": "€"}')
    assert path == f"{root}/00-Meta/_plan.json"
    assert vault.get_text(run_id, path) == '{"a": "€"}'                      # full vault path works too
    vault.put_bytes(run_id, "01-Annual-Reports/ar.pdf", b"%PDF")
    tree = vault.tree(run_id)
    assert tree.name == "siemens-healthineers" and tree.path == root
    assert [c.name for c in tree.children or []] == FOLDERS
    meta = next(c for c in tree.children or [] if c.name == "00-Meta")
    assert meta.children and meta.children[0].type == "file" and meta.children[0].size == len('{"a": "€"}'.encode())
    f = vault.file(run_id, path)
    assert f.content_type == "application/json" and f.text == '{"a": "€"}' and not f.truncated
    pdf = vault.file(run_id, "01-Annual-Reports/ar.pdf")
    assert pdf.text is None and pdf.content_type == "application/pdf" and pdf.size == 4
    with pytest.raises(KeyError):
        vault.file(run_id, "00-Meta/nope.json")
    for bad in ("../../etc/passwd", "00-Meta/../../x", f"runs/{run_id}/other-co/x.md", "C:/x", "runs/run_x/y/z"):
        with pytest.raises(ValueError):
            vault.rel(run_id, bad)


def test_vault_truncates_big_text(settings):
    vault = Vault(settings)
    run_id = "run_20261107_vault2"
    register_run(run_id, "Big Co")
    vault.put_text(run_id, "09-Analysis/big.md", "x" * (TEXT_LIMIT + 10))
    f = vault.file(run_id, "09-Analysis/big.md")
    assert f.truncated and len(f.text or "") == TEXT_LIMIT and f.size == TEXT_LIMIT + 10


# ---------------------------------------------------------------------------
# aws mode, offline: small fakes for the S3 and DynamoDB clients
# ---------------------------------------------------------------------------


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}           # KeyError when missing

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        if Delimiter:
            prefixes = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                               for k in keys if Delimiter in k[len(Prefix):]})
            return {"CommonPrefixes": [{"Prefix": p} for p in prefixes], "IsTruncated": False}
        return {"Contents": [{"Key": k, "Size": len(self.objects[(Bucket, k)])} for k in keys], "IsTruncated": False}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://presigned.test/{Params['Key']}?ttl={ExpiresIn}"


class FakeDynamo:
    def __init__(self, unprocessed_once: bool = False) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.unprocessed_once = unprocessed_once
        self.batch_calls = 0

    def batch_write_item(self, RequestItems):
        self.batch_calls += 1
        (table, requests), = RequestItems.items()
        assert len(requests) <= 25
        if self.unprocessed_once and len(requests) > 1:
            self.unprocessed_once = False
            keep, rest = requests[:1], requests[1:]
        else:
            keep, rest = requests, []
        for r in keep:
            item = r["PutRequest"]["Item"]
            self.tables.setdefault(table, {})[(item["run_id"]["S"], int(item["seq"]["N"]))] = item
        return {"UnprocessedItems": {table: rest} if rest else {}}

    def put_item(self, TableName, Item):
        self.tables.setdefault(TableName, {})[(Item["run_id"]["S"],)] = Item

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, ConsistentRead=False,
              ExclusiveStartKey=None):
        rid = ExpressionAttributeValues[":r"]["S"]
        rows = sorted((k[1], v) for k, v in self.tables.get(TableName, {}).items() if k[0] == rid)
        start = int(ExclusiveStartKey["seq"]["N"]) if ExclusiveStartKey else 0
        rows = [r for r in rows if r[0] > start]
        page, more = rows[:7], len(rows) > 7                      # small pages, to exercise pagination
        resp = {"Items": [v for _, v in page]}
        if more:
            resp["LastEvaluatedKey"] = {"run_id": {"S": rid}, "seq": {"N": str(page[-1][0])}}
        return resp

    def scan(self, TableName, ProjectionExpression=None, ExclusiveStartKey=None):
        return {"Items": [{"run_id": v["run_id"]} for v in self.tables.get(TableName, {}).values()]}


def aws_settings(settings):
    return settings.model_copy(update={"store": "aws", "vault_bucket": "fake-bucket", "runs_table": "fake-runs",
                                       "events_table": "fake-events"})


def test_aws_store_round_trip_offline(settings, fixture_report):
    s3, ddb = FakeS3(), FakeDynamo(unprocessed_once=True)
    cfg = aws_settings(settings)
    store = Store(cfg, s3=s3, dynamodb=ddb)
    state = finished_state(store, fixture_report)               # sync emits: written to DynamoDB right away
    for i in range(40):
        store.append(state, "agent.log", {"text": f"l{i}", "transcript_path": None}, message="log")
    store.vault.put_text(state.run.run_id, "00-Meta/_plan.json", "{}")
    store.save(state)
    rid = state.run.run_id
    assert len([k for k in ddb.tables["fake-events"] if k[0] == rid]) == len(state.events)
    assert ("fake-bucket", f"runs/{rid}/state.json") in s3.objects
    root = store.vault.root(rid)
    assert ("fake-bucket", f"{root}/00-Meta/events.jsonl") in s3.objects
    row = ddb.tables["fake-runs"][(rid,)]
    assert m.Run.model_validate_json(row["run"]["S"]).status == "done"
    assert not list(Path(cfg.data_dir).rglob("state.json"))     # nothing local in aws mode

    again = Store(cfg, s3=s3, dynamodb=ddb)
    again.load_all()
    loaded = again.get(rid)
    assert loaded.events == state.events and loaded.run.last_seq == state.run.last_seq
    store.vault.put_bytes(rid, "01-Annual-Reports/ar.pdf", b"%PDF")
    pdf = again.vault.file(rid, "01-Annual-Reports/ar.pdf")
    assert pdf.download_url and pdf.download_url.startswith("https://presigned.test/")
    assert {c.name for c in again.vault.tree(rid).children or []} >= {"00-Meta", "01-Annual-Reports"}


async def test_aws_events_are_batched_off_the_loop(settings):
    s3, ddb = FakeS3(), FakeDynamo()
    store = Store(aws_settings(settings), s3=s3, dynamodb=ddb)
    ctx = make_ctx(store)
    for i in range(60):
        log_event(ctx, i)
    assert ddb.tables.get("fake-events") is None                # queued, not written inside emit
    await store.flush_events(ctx.run_id)
    assert len(ddb.tables["fake-events"]) == 60
    assert ddb.batch_calls == 3                                  # 25 + 25 + 10
    await store.save_async(ctx.state)
    assert ("fake-bucket", f"runs/{ctx.run_id}/state.json") in s3.objects


async def test_aws_background_flusher_writes_soon(settings, monkeypatch):
    from backend.axelrod import store as store_mod

    monkeypatch.setattr(store_mod, "FLUSH_DELAY_S", 0.01)
    ddb = FakeDynamo()
    store = Store(aws_settings(settings), s3=FakeS3(), dynamodb=ddb)
    ctx = make_ctx(store)
    log_event(ctx, 1)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ddb.tables.get("fake-events"):
            break
    assert len(ddb.tables["fake-events"]) == 1


@pytest.mark.live
def test_live_aws_round_trip():
    """Real DynamoDB and S3 (tables axelrod-runs and axelrod-events, the vault bucket). Cleans up after itself."""
    cfg = load_settings().model_copy(update={"store": "aws"})
    if not (cfg.vault_bucket and cfg.runs_table and cfg.events_table):
        pytest.skip("VAULT_BUCKET, RUNS_TABLE and EVENTS_TABLE must be set")
    store = Store(cfg)
    ctx = make_ctx(store, company="Live store test (delete me)", ticker=None)
    rid = ctx.run_id
    try:
        ctx.emit("run.created", {"run": ctx.state.run}, message="live store test")
        for i in range(30):
            log_event(ctx, i)
        ctx.emit("run.status", {"status": "done", "stage": "complete"}, message="done", stage="complete")
        store.vault.put_text(rid, "00-Meta/_plan.json", '{"live": true}')
        store.save(ctx.state)
        again = Store(cfg)
        again.load_all()
        loaded = again.get(rid)
        assert loaded.events == ctx.state.events and loaded.run.status == "done"
        assert again.vault.get_text(rid, "00-Meta/_plan.json") == '{"live": true}'
    finally:
        _cleanup_live(store, rid, len(ctx.state.events))


def _cleanup_live(store: Store, rid: str, n_events: int) -> None:
    """Remove only what this test wrote: its events, its run row and its S3 keys."""
    ddb = store.dynamodb()
    for seq in range(1, n_events + 1):
        ddb.delete_item(TableName=store.settings.events_table, Key={"run_id": {"S": rid}, "seq": {"N": str(seq)}})
    ddb.delete_item(TableName=store.settings.runs_table, Key={"run_id": {"S": rid}})
    s3 = store.vault.s3()
    resp = s3.list_objects_v2(Bucket=store.vault.bucket, Prefix=f"runs/{rid}/")
    for obj in resp.get("Contents", []):
        s3.delete_object(Bucket=store.vault.bucket, Key=obj["Key"])


# ---------------------------------------------------------------------------
# plan stage
# ---------------------------------------------------------------------------


def test_matches_aliases_and_tickers():
    plan = json.loads(SAMPLE_PLAN.read_text(encoding="utf-8"))
    for company, ticker in (("Siemens Healthineers AG", None), ("siemens healthineers", None), ("Healthineers", None),
                            ("Some Name", "SHL"), ("Some Name", "SHL.DE"), ("SIEMENS-HEALTHINEERS", None)):
        assert plan_stage.matches(plan, company, ticker), (company, ticker)
    for company, ticker in (("Siemens AG", "SIE"), ("Philips", "PHIA"), ("Healthineers Rival", None)):
        assert not plan_stage.matches(plan, company, ticker), (company, ticker)


def test_a_typo_in_the_company_name_still_finds_the_frozen_plan():
    """The live failure at 1:58 AM: "siemens heathineers" with no ticker. One or two typos pass, another
    company, a short name or a near-miss ticker never does."""
    plan = json.loads(SAMPLE_PLAN.read_text(encoding="utf-8"))
    for company in ("siemens heathineers", "Siemens Helthineers AG", "Seimens Healthineers", "siemenshealthineers",
                    "healtineers", "Siemens Healthinneers"):
        assert plan_stage.match_kind(plan, company, None) == "typo", company
    assert plan_stage.match_kind(plan, "Siemens Healthineers", None) == "exact"
    for company, ticker in (("NVIDIA", None), ("Nvidia Corp", "NVDA"), ("Siemens", None), ("Siemens AG", None),
                            ("Siemens Energy", None), ("Healthineers Rival", None), ("GE HealthCare", "GEHC"),
                            ("Philips Healthcare", None), ("Some Name", "SHW"), ("Some Name", "SH")):
        assert plan_stage.match_kind(plan, company, ticker) is None, (company, ticker)
    assert plan_stage.plan_name(plan) == "Siemens Healthineers"


async def test_plan_stage_takes_the_plans_spelling_on_a_typo(settings, monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    ctx = make_ctx(Store(settings), company="siemens heathineers", ticker=None)
    await plan_stage.run(ctx)
    run = ctx.state.run
    assert (run.company, run.ticker) == ("Siemens Healthineers", "SHL")
    assert "Company typed as 'siemens heathineers', read as Siemens Healthineers" in run.notes
    assert ctx.state.plan_ready


async def test_the_refusal_says_what_to_type(settings, monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    ctx = make_ctx(Store(settings), company="NVIDIA", ticker=None)
    with pytest.raises(plan_stage.PlanUnavailable, match=r"type Siemens Healthineers, or ticker SHL"):
        await plan_stage.run(ctx)


async def test_plan_stage_streams_the_frozen_plan(settings, monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    ctx = make_ctx(Store(settings))
    ctx.state.run.notes = "analyst note"
    await plan_stage.run(ctx)
    types = [e.type for e in ctx.state.events]
    assert types == ["plan.need", "plan.need", "plan.source", "plan.source", "plan.ready"]
    ready = ctx.state.events[-1]
    assert "Frozen demo plan" in ready.message and (ready.data.needs, ready.data.sources) == (2, 2)
    assert ctx.state.plan_ready and ctx.state.plan().ready
    assert ctx.state.run.notes.startswith("analyst note | Frozen demo plan")
    assert ctx.state.extract_pages == {"S-001": [[150, 156], [170, 172]]}
    assert all(s.approval == "pending" and s.status == "proposed" for s in ctx.state.sources)
    assert ctx.next_id("S") == "S-003" and ctx.next_id("N") == "N-03"
    plan_file = json.loads(ctx.vault.get_text(ctx.run_id, "00-Meta/_plan.json"))
    assert plan_file["frozen"] is True and len(plan_file["sources"]) == 2
    assert ctx.state.run.counts.sources_total == 2


async def test_plan_stage_refuses_other_companies(settings, monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    ctx = make_ctx(Store(settings), company="Philips", ticker="PHIA")
    with pytest.raises(plan_stage.PlanUnavailable, match="only the frozen Siemens Healthineers plan"):
        await plan_stage.run(ctx)
    assert ctx.state.events == []


async def test_plan_stage_reads_the_file_at_run_time(settings, monkeypatch, tmp_path):
    missing = tmp_path / "plan.json"
    monkeypatch.setattr(plan_stage, "PLAN_FILE", missing)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    ctx = make_ctx(Store(settings))
    with pytest.raises(plan_stage.PlanUnavailable):
        await plan_stage.run(ctx)
    missing.write_text(SAMPLE_PLAN.read_text(encoding="utf-8"), encoding="utf-8")
    await plan_stage.run(make_ctx(Store(settings)))


async def test_plan_stage_stops_on_kill_switch(settings, monkeypatch):
    monkeypatch.setattr(plan_stage, "PLAN_FILE", SAMPLE_PLAN)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    ctx = make_ctx(Store(settings))
    ctx.state.kill_switch = True
    with pytest.raises(StopRequested):
        await plan_stage.run(ctx)


# ---------------------------------------------------------------------------
# approval rules (commit, with a gateway, is tested through the API in test_api.py)
# ---------------------------------------------------------------------------


def awaiting_state(settings) -> RunState:
    ctx = make_ctx(Store(settings))
    ctx.state.sources = sample_sources() + [_extra("S-003", "priority", 2), _extra("S-004", "tier_3", 1)]
    ctx.emit("run.status", {"status": "awaiting_approval", "stage": "approve"}, message="wait", stage="approve")
    return ctx.state


def _extra(sid: str, tier: str, rank: int, proposed_by: str = "planner") -> m.Source:
    s = sample_sources()[0].model_copy(deep=True)
    return s.model_copy(update={"source_id": sid, "tier": tier, "rank": rank, "proposed_by": proposed_by,
                                "url": f"https://{sid.lower()}.test/x", "domain": f"{sid.lower()}.test"})


def _req(*decisions: dict) -> m.ApprovalRequest:
    return m.ApprovalRequest(decisions=[m.SourceDecision.model_validate(d) for d in decisions])


def test_validate_approval_errors(settings):
    state = awaiting_state(settings)
    with pytest.raises(approve_stage.ApprovalError) as e:
        approve_stage.validate(state, _req({"source_id": "S-001", "approval": "approved"}))
    assert (e.value.status, e.value.details) == (422, {"undecided": ["S-003"]})
    with pytest.raises(approve_stage.ApprovalError) as e:
        approve_stage.validate(state, _req({"source_id": "S-404", "approval": "approved"}))
    assert e.value.details == {"unknown": ["S-404"]}
    with pytest.raises(approve_stage.ApprovalError) as e:
        approve_stage.validate(state, _req({"source_id": "S-001", "approval": "approved"},
                                           {"source_id": "S-001", "approval": "rejected"},
                                           {"source_id": "S-003", "approval": "approved"}))
    assert e.value.details == {"duplicate": ["S-001"]}
    approve_stage.validate(state, _req({"source_id": "S-001", "approval": "approved"},
                                       {"source_id": "S-003", "approval": "rejected"}))
    state.run.status = "running"
    with pytest.raises(approve_stage.ApprovalError) as e:
        approve_stage.validate(state, _req())
    assert (e.value.status, e.value.code) == (409, "invalid_state")


def test_apply_decisions_rules(settings):
    state = awaiting_state(settings)
    state.sources.append(_extra("S-005", "priority", 3, proposed_by="user").model_copy(update={"approval": "approved"}))
    approved, rejected, auto = approve_stage.apply_decisions(state.sources, _req(
        {"source_id": "S-001", "approval": "approved"},
        {"source_id": "S-003", "approval": "approved", "tier": "tier_2", "rank": 1},   # demoted, placed first
        {"source_id": "S-004", "approval": "rejected"},
    ).decisions)
    by = state.sources_by_id()
    assert approved == ["S-001", "S-003", "S-005"] and rejected == ["S-004"] and auto == ["S-002"]
    assert by["S-002"].approval == "auto_approved" and by["S-002"].status == "queued"
    assert by["S-004"].status == "skipped"
    assert (by["S-003"].tier, by["S-003"].rank, by["S-002"].rank) == ("tier_2", 1, 2)
    assert [s.source_id for s in state.sorted_sources()] == ["S-001", "S-005", "S-003", "S-002", "S-004"]
    assert by["S-005"].rank == 2


def test_fixture_bundle_events_validate(fixture_bundle):
    """The mock's timeline events fit the same adapter the backend uses (guards against contract drift)."""
    raw = copy.deepcopy(fixture_bundle["timeline_pre"][:5])
    for i, e in enumerate(raw, start=1):
        e = {k: v for k, v in e.items() if k != "t" and not k.startswith("_")}
        ev.EVENT_ADAPTER.validate_python({"seq": i, "ts": axids.now_iso(), "run_id": "run_x", **e})
