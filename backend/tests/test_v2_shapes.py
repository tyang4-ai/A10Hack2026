"""V1-shapes: the contract v0.2 and v0.3 shapes in the real backend (offline).

Agent names (Bobby first, then his goons in spawn order), the stage gates hook, the document cache key and
layout, the new settings, Document origin, the gateway's orchestrator scope and agent-scoped rules, the
source_revoked cascade reason, AgentRunner's rerun fields and Instructions block, and every new route: the
mock's status codes and errors, 409 "not built yet (<plan>)" while a control module is a stub, and the
module's answer once it is real. Model answers come from FAKE_MODELS fakes (simulated).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from typing import ClassVar

import pytest
from pydantic import BaseModel
from fastapi.testclient import TestClient

from backend.axelrod import agentstop, bedrock, cache, cascade, chat, comments, fork, gates, orchestrator, resume, rework
from backend.axelrod.agents.base import AgentRunner, instructions_block, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.config import load_settings
from backend.axelrod.contract import m
from backend.axelrod.fetch import ingest
from backend.axelrod.gateway import Blocked, FetchResult, role_scopes
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import InvalidState, RunContext, StopRequested
from backend.axelrod.store import Store
from backend.axelrod.store.state import RunState, agent_name

P = "/api/v1"
SHL = m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_ctx(settings) -> RunContext:
    store = Store(settings)
    state = store.create_run(SHL)
    return RunContext.create(state, store, store.vault, settings)


def agent(agent_id: str, role: str, status: str = "done") -> m.Agent:
    return m.Agent(agent_id=agent_id, role=role, status=status, task=f"test {role}", target_ids=[])


def types(ctx: RunContext) -> list[str]:
    return [e.type for e in ctx.state.events]


class Verdict(BaseModel):
    """Test output for the fake model."""

    TOOL_NAME: ClassVar[str] = "v2_shapes_verdict"
    ok: bool


# ---------------------------------------------------------------------------
# document cache key and layout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("HTTPS://Example.COM:443/Path/A?B=1&a=2#frag", "https://example.com/Path/A?B=1&a=2"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("https://example.com:80/x", "https://example.com:80/x"),
    ("https://User@Example.com/x", "https://User@example.com/x"),
    ("http://[2001:DB8::1]:80/a", "http://[2001:db8::1]/a"),
    ("  https://data.sec.gov/api/xbrl/companyfacts/CIK0000040545.json  ",
     "https://data.sec.gov/api/xbrl/companyfacts/CIK0000040545.json"),
    ("s3://Axelrod-Vault/archive/shl/01-Annual-Reports/AR 2019.pdf",
     "s3://axelrod-vault/archive/shl/01-Annual-Reports/AR 2019.pdf"),
    ("https://example.com", "https://example.com"),
    ("not a url#x", "not a url"),
])
def test_normalize_url(url, expected):
    assert cache.normalize_url(url) == expected


def test_doc_key_is_the_sha256_of_the_normalized_url():
    key = cache.doc_key("https://WWW.Example.com:443/report.pdf#page=3")
    assert key == hashlib.sha256(b"https://www.example.com/report.pdf").hexdigest() and len(key) == 64
    assert key == cache.doc_key("https://www.example.com/report.pdf")
    assert key != cache.doc_key("https://www.example.com/Report.pdf")          # path case is kept
    assert key != cache.doc_key("https://www.example.com/report.pdf?v=2")      # so is the query


def test_cache_layout(settings):
    key = cache.doc_key("https://example.com/a.pdf")
    assert cache.body_key(key) == f"cache/docs/{key}.bin" and cache.meta_key(key) == f"cache/docs/{key}.json"
    assert cache.archive_key("01-Annual-Reports\\AR 2019.pdf") == "archive/shl/01-Annual-Reports/AR 2019.pdf"
    assert cache.ARCHIVE_MANIFEST == "archive/shl/manifest.json"
    assert cache.local_path(settings, cache.body_key(key)) == settings.data_dir / "cache" / "docs" / f"{key}.bin"
    meta = cache.CacheMeta(url="https://example.com/a.pdf", normalized_url="https://example.com/a.pdf",
                           content_type="application/pdf", bytes=3, sha256="0" * 64, collected_at=now_iso(),
                           origin="archive", source_path="01-Annual-Reports/a.pdf")
    assert cache.CacheMeta.model_validate_json(meta.model_dump_json()) == meta
    assert cache.run_cache_mode(settings.model_copy(update={"cache_documents": True})) == "off"   # until V2


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_cache_settings_default_off_and_parse():
    s = load_settings(env_file=None, environ={})
    assert s.cache_documents is False and s.cache_models == "off"
    s = load_settings(env_file=None, environ={"CACHE_DOCUMENTS": "ON", "CACHE_MODELS": "Replay"})
    assert s.cache_documents is True and s.cache_models == "replay"
    assert load_settings(env_file=None, environ={"CACHE_DOCUMENTS": "off"}).cache_documents is False
    with pytest.raises(ValueError):
        load_settings(env_file=None, environ={"CACHE_MODELS": "sometimes"})


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------


def test_one_function_names_every_agent_in_spawn_order(settings):
    ctx = make_ctx(settings)
    gates.spawn_bobby(ctx)
    first = ctx.state.register_agent(agent("fetcher-01", "fetcher"))
    mine = agent("extractor-01", "extractor")
    mine.name = "Not my name"                                   # callers never name agents
    second = ctx.state.register_agent(mine)
    assert [a.name for a in ctx.state.agents.values()] == ["Bobby", "Bobby's goon #1", "Bobby's goon #2"]
    assert (first.name, second.name) == ("Bobby's goon #1", "Bobby's goon #2")
    again = ctx.state.register_agent(agent("fetcher-01", "fetcher"))
    assert again.name == "Bobby's goon #1" and len(ctx.state.agents) == 3     # no goon number spent twice
    assert agent_name("orchestrator", 7) == "Bobby" and agent_name("verifier", 7) == "Bobby's goon #7"


def test_bobby_is_spawned_once_working_on_the_large_model(settings):
    ctx = make_ctx(settings)
    b = gates.spawn_bobby(ctx)
    assert gates.spawn_bobby(ctx) is b
    assert (b.agent_id, b.role, b.name, b.status, b.model_id) == ("bobby", "orchestrator", "Bobby", "working",
                                                                   settings.model_large)
    spawned = [e for e in ctx.state.events if e.type == "agent.spawned"]
    assert len(spawned) == 1 and spawned[0].data.agent.name == "Bobby" and spawned[0].agent_id == "bobby"
    gates.finish_bobby(ctx)
    gates.finish_bobby(ctx, "failed")                                         # the first end wins
    assert b.status == "done" and types(ctx).count("agent.status") == 1


def test_loaded_snapshots_get_names(settings):
    store = Store(settings)
    state = store.create_run(SHL)
    state.agents = {a.agent_id: a for a in (agent("bobby", "orchestrator"), agent("fetcher-01", "fetcher"),
                                            agent("extractor-01", "extractor"))}
    state.run.status = "done"
    store.save(state)
    fresh = Store(settings)
    fresh.load_all()
    names = [a.name for a in fresh.get(state.run.run_id).agents.values()]
    assert names == ["Bobby", "Bobby's goon #1", "Bobby's goon #2"]


# ---------------------------------------------------------------------------
# the pipeline: Bobby first, the four gates, Bobby ends before the run's terminal events
# ---------------------------------------------------------------------------


def _noop(name: str):
    async def run(ctx: RunContext) -> None:
        return None
    run.__name__ = name
    return run


@pytest.fixture
def gate_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def record(ctx, stage):
        calls.append(stage)

    monkeypatch.setattr(gates, "after_stage", record)
    return calls


async def test_a_fake_model_run_names_every_agent_and_bobby_comes_first(settings, monkeypatch, gate_calls):
    prompts: list[str] = []

    def fake(system, user, schema):
        prompts.append(user)
        return {"ok": True}

    bedrock.register_fake(tool_name(Verdict), fake)

    async def collect(ctx):
        from backend.axelrod.stages import collect as collect_stage
        f = collect_stage.spawn_fetcher(ctx, [])
        collect_stage.finish_fetcher(ctx, f, "done")

    async def extract(ctx):
        for k in range(3):
            with AgentRunner(ctx, "extractor", f"Extract page {k}", ["S-001"], "small") as r:
                assert (await r.ask("system", f"page {k}", Verdict)).ok

    stages = [(s, _noop(s)) for s, _ in orchestrator.STAGES]
    stages[2], stages[3] = ("collect", collect), ("extract", extract)
    monkeypatch.setattr(orchestrator, "STAGES", stages)
    store = Store(settings)
    ctx = await orchestrator.start_run(store, store.vault, settings, SHL)
    await ctx.task
    st = ctx.state
    assert st.run.status == "done", st.run.error
    agents = list(st.agents.values())
    assert agents[0].agent_id == "bobby" and agents[0].name == "Bobby" and agents[0].role == "orchestrator"
    assert [a.name for a in agents[1:]] == [f"Bobby's goon #{n}" for n in range(1, 5)]
    assert all(a.name for a in agents) and all(a.status == "done" for a in agents)
    assert gate_calls == ["plan", "extract", "repair", "verify_summary"]
    t = types(ctx)
    assert t[:3] == ["run.created", "run.status", "agent.spawned"] and t[-1] == "run.completed"
    bobby_done = max(i for i, e in enumerate(st.events) if e.type == "agent.status" and e.agent_id == "bobby")
    assert bobby_done < t.index("run.completed") and st.events[bobby_done].data.status == "done"
    assert prompts and all("Instructions" not in p for p in prompts)      # no rerun, no instructions block
    bedrock.unregister_fake(tool_name(Verdict))


async def test_a_failing_gate_never_fails_the_run(settings, monkeypatch):
    async def broken(ctx, stage):
        raise RuntimeError("Bobby tripped")

    monkeypatch.setattr(gates, "after_stage", broken)
    monkeypatch.setattr(orchestrator, "STAGES", [("plan", _noop("plan")), ("complete", _noop("finalize"))])
    ctx = make_ctx(settings)
    await orchestrator.run_pipeline(ctx)
    assert ctx.state.run.status == "done"
    logs = [e for e in ctx.state.events if e.type == "agent.log" and e.agent_id == "bobby"]
    assert logs and logs[0].level == "warn" and "gate after plan failed" in logs[0].message


@pytest.mark.parametrize("outcome", ["failed", "stopped"])
async def test_bobby_ends_before_the_terminal_event(settings, monkeypatch, outcome):
    async def boom(ctx):
        raise StopRequested("analyst") if outcome == "stopped" else RuntimeError("stage broke")

    monkeypatch.setattr(orchestrator, "STAGES", [("plan", boom)])
    ctx = make_ctx(settings)
    await orchestrator.run_pipeline(ctx)
    assert ctx.state.run.status == outcome and ctx.state.agents["bobby"].status == outcome
    assert types(ctx)[-1] == ("run.failed" if outcome == "failed" else "run.stopped")


# ---------------------------------------------------------------------------
# AgentRunner: rerun fields, the Instructions block, attach
# ---------------------------------------------------------------------------


async def test_a_rerun_agent_carries_its_fields_and_the_instructions_block(settings):
    seen: list[str] = []
    bedrock.register_fake(tool_name(Verdict), lambda s, u, sch: seen.append(u) or {"ok": True})
    ctx = make_ctx(settings)
    r = AgentRunner(ctx, "extractor", "Rerun of extractor-03", ["S-001"], "small", agent_id="extractor-03.2",
                    parent_agent_id="extractor-03", instructions="Use the restated FY2024 column.",
                    rerun_by="orchestrator")
    a = ctx.state.agents["extractor-03.2"]
    assert (a.parent_agent_id, a.instructions, a.rerun_by, a.name) == (
        "extractor-03", "Use the restated FY2024 column.", "orchestrator", "Bobby's goon #1")
    assert ctx.state.events[-1].data.agent.parent_agent_id == "extractor-03"
    await r.ask("system", "the page", Verdict)
    assert seen == ["the page" + instructions_block("Use the restated FY2024 column.", "orchestrator")]
    assert "Instructions (from Bobby, the orchestrator):" in seen[0]
    bedrock.unregister_fake(tool_name(Verdict))


def test_a_forks_instructions_reach_its_goons_but_never_its_judges(settings):
    ctx = make_ctx(settings)
    ctx.state.run.instructions = "Weight the FY2025 report over press coverage."
    drafter = AgentRunner(ctx, "drafter", "Draft", [], "large").agent
    judge = AgentRunner(ctx, "verifier", "Judge C-001", ["C-001"], "small", agent_id="verifier-C-001-1").agent
    assert drafter.instructions == ctx.state.run.instructions and judge.instructions is None


async def test_attach_runs_as_an_existing_agent_and_keeps_its_transcript(settings):
    bedrock.register_fake(tool_name(Verdict), lambda s, u, sch: {"ok": True})
    ctx = make_ctx(settings)
    first = AgentRunner(ctx, "extractor", "t", [], "small")
    await first.ask("s", "first", Verdict)
    first.finish("done")
    n_spawned = types(ctx).count("agent.spawned")
    chat_runner = AgentRunner.attach(ctx, "extractor-01", track_status=False)
    assert chat_runner.size == "small" and chat_runner.agent is ctx.state.agents["extractor-01"]
    await chat_runner.ask("s", "second", Verdict)
    assert types(ctx).count("agent.spawned") == n_spawned and ctx.state.agents["extractor-01"].status == "done"
    text = ctx.vault.get_text(ctx.run_id, "00-Meta/agents/extractor-01.jsonl")
    assert '"user": "first"' in text and '"user": "second"' in text
    with pytest.raises(KeyError):
        AgentRunner.attach(ctx, "nobody")
    bedrock.unregister_fake(tool_name(Verdict))


# ---------------------------------------------------------------------------
# gateway, ingest, cascade
# ---------------------------------------------------------------------------


def test_the_orchestrator_may_call_the_large_model_only(settings):
    ctx = make_ctx(settings)
    b = gates.spawn_bobby(ctx)
    scope = next(s for s in role_scopes(settings) if s.role == "orchestrator")
    assert scope.actions == ["model.invoke"] and scope.models == [settings.model_large]
    assert ctx.gateway.decide(b, "model.invoke", settings.model_large)[0] == "allowed"
    assert ctx.gateway.decide(b, "model.invoke", settings.model_small)[0] == "blocked"
    assert ctx.gateway.decide(b, "fetch", "https://www.siemens-healthineers.com/")[0] == "blocked"


async def test_an_agent_scoped_rule_applies_to_that_agent_only(settings):
    ctx = make_ctx(settings)
    at = now_iso()
    ctx.state.policy = m.Policy(
        run_id=ctx.run_id, version=3, role_scopes=role_scopes(settings), updated_at=at, as_of_seq=0, rules=[
            m.PolicyRule(rule_id="R-stop-fetcher-02", effect="deny", action="fetch", agent_id="fetcher-02",
                         origin="user_agent_stop", note="Stopped by the analyst", created_at=at),
            m.PolicyRule(rule_id="R-allow-S-001", effect="allow", action="fetch", domain="example.com",
                         path_prefix="/", roles=["fetcher"], source_id="S-001", origin="user_approval",
                         created_at=at)])
    url = "https://example.com/report.pdf"
    assert ctx.gateway.decide(agent("fetcher-01", "fetcher", "working"), "fetch", url)[:2] == (
        "allowed", "R-allow-S-001")
    assert ctx.gateway.decide(agent("fetcher-02", "fetcher", "working"), "fetch", url)[:2] == (
        "blocked", "R-stop-fetcher-02")
    with pytest.raises(Blocked):
        await ctx.gateway.fetch(agent("fetcher-02", "fetcher", "working"), url)
    req = [e for e in ctx.state.events if e.type == "gateway.request"][-1]
    assert req.data.cache is None and req.data.cached_at is None            # the cache is off


async def test_a_live_fetch_is_stored_as_fetched_with_its_collection_time(settings):
    ctx = make_ctx(settings)
    src = m.Source(source_id="S-001", title="Annual report", url="https://example.com/ar.html",
                   domain="example.com", kind="annual_report", data_types=["other"], access=m.SourceAccess(),
                   tier="priority", rank=1, scores=m.SourceScores(reliability=0.9, relevance=0.9), rationale="t",
                   need_ids=["N-01"], criteria=["rotic_level"], vault_folder="01-Annual-Reports",
                   proposed_by="planner", approval="approved", status="fetching")
    ctx.state.sources = [src]
    body = b"<html><body><article><p>Revenue was 23,375 million euros in fiscal year 2025.</p></article></body></html>"
    res = FetchResult(url=src.url, final_url=src.url, http_status=200, content_type="text/html", body=body,
                      request_id="REQ-0001")
    doc = await ingest.ingest(ctx, src, res)
    assert doc.origin == "fetched" and doc.collected_at == doc.fetched_at
    cached = FetchResult(src.url, src.url, 200, "text/html", body, "REQ-0002", origin="archive",
                         collected_at="2026-07-01T00:00:00.000Z", cache="hit")
    assert ingest.doc_origin(cached, "2026-11-08T01:00:00.000Z") == ("archive", "2026-07-01T00:00:00.000Z")
    odd = FetchResult(src.url, src.url, 200, "text/html", body, "REQ-0003", origin="elsewhere")
    assert ingest.doc_origin(odd, "now") == ("fetched", "now")


def test_the_revoke_cascade_sends_source_revoked_before_item_verified(settings, fixture_bundle):
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Fixture Co", ticker="FIX", created_via="api"))
    state.sources = [m.Source.model_validate(s) for s in copy.deepcopy(fixture_bundle["sources"])]
    for s in state.sources:
        s.approval, s.status = ("approved" if s.tier == "priority" else "auto_approved"), "extracted"
    report = m.AgentReport.model_validate(copy.deepcopy(fixture_bundle["report"]))
    state.items = {i.id: i for i in report.items}
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    ctx.gateway.revoke("S-006", "test revoke")
    mark = state.run.last_seq
    res = cascade.revoke_cascade(ctx, "S-006")
    evs = [e for e in state.events if e.seq > mark]
    updated = [e for e in evs if e.type == "item.updated"]
    verified = [e for e in evs if e.type == "item.verified"]
    assert res.item_ids and [e.data.item.id for e in updated] == res.item_ids
    assert all(e.data.reason == "source_revoked" for e in updated)
    assert all(evs.index(u) < evs.index(v) for u, v in zip(updated, verified, strict=True))


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@pytest.fixture
def api(settings):
    """An API over one prepared run at stage extract: Bobby working, a done fetcher and extractor, a
    working judge."""
    store = Store(settings)
    state = store.create_run(SHL)
    ctx = RunContext.create(state, store, store.vault, settings)
    gates.spawn_bobby(ctx)
    for a in (agent("fetcher-01", "fetcher"), agent("extractor-01", "extractor"),
              agent("verifier-D-001-1", "verifier", "working")):
        state.register_agent(a)
    state.run.stage = "extract"
    with TestClient(create_app(settings, store=store)) as client:
        yield client, state


def err(r, status: int, code: str = "invalid_state") -> str:
    assert r.status_code == status, r.text
    body = m.ErrorResponse.model_validate(r.json()).error
    assert body.code == code
    return body.message


def run_path(state: RunState) -> str:
    return f"{P}/runs/{state.run.run_id}"


def test_new_routes_answer_like_the_mock_while_their_modules_are_stubs(api):
    client, state = api
    r = run_path(state)
    assert "No agent nope" in err(client.post(f"{r}/agents/nope/stop", json={}), 404, "not_found")
    assert err(client.post(f"{r}/agents/extractor-01/stop", json={}), 409) == "extractor-01 is already done"
    assert err(client.post(f"{r}/agents/verifier-D-001-1/stop", json={"reason": "x"}), 409) == \
        "not built yet (V2-gateway)"
    assert err(client.post(f"{r}/agents/verifier-D-001-1/stop"), 409) == "not built yet (V2-gateway)"
    err(client.get(f"{r}/agents/nope/messages"), 404, "not_found")
    assert err(client.get(f"{r}/agents/bobby/messages"), 409) == "not built yet (V7-chat)"
    assert err(client.post(f"{r}/agents/bobby/messages", json={"text": "why?"}), 409) == "not built yet (V7-chat)"
    err(client.post(f"{r}/agents/bobby/messages", json={"text": ""}), 422, "validation_error")
    err(client.post(f"{r}/agents/nope/rerun", json={"instructions": "x"}), 404, "not_found")
    assert "Bobby can't be rerun" in err(client.post(f"{r}/agents/bobby/rerun", json={"instructions": "x"}), 409)
    assert "Fetchers can't be rerun" in err(client.post(f"{r}/agents/fetcher-01/rerun",
                                                        json={"instructions": "x"}), 409)
    assert "still working. Stop it first" in err(client.post(f"{r}/agents/verifier-D-001-1/rerun",
                                                             json={"instructions": "x"}), 409)
    err(client.post(f"{r}/agents/extractor-01/rerun", json={"instructions": ""}), 422, "validation_error")
    assert err(client.post(f"{r}/agents/extractor-01/rerun", json={"instructions": "Use page 138", "recheck": True}),
               409) == "not built yet (V3-rework)"
    assert "Only a stopped run can resume. This one is running" in err(client.post(f"{r}/resume", json={}), 409)
    assert "This one is running" in err(client.post(f"{r}/rerun", json={"from_stage": "draft",
                                                                         "instructions": "x"}), 409)
    err(client.post(f"{r}/rerun", json={"from_stage": "nowhere", "instructions": "x"}), 422, "validation_error")
    err(client.post(f"{P}/runs/run_20000101_nope00/resume", json={}), 404, "not_found")


def test_a_stopped_run_rejects_the_controls_and_bobby_stops_first(api):
    client, state = api
    r = run_path(state)
    assert client.post(f"{r}/stop", json={"reason": "demo"}).status_code == 200
    evs = state.events
    bobby_stop = next(i for i, e in enumerate(evs) if e.type == "agent.status" and e.agent_id == "bobby")
    assert evs[bobby_stop].data.status == "stopped" and bobby_stop < [e.type for e in evs].index("run.stopped")
    assert err(client.post(f"{r}/agents/verifier-D-001-1/stop", json={}), 409) == "Run is already stopped"
    assert "Resume it to talk" in err(client.post(f"{r}/agents/bobby/messages", json={"text": "hi"}), 409)
    assert "Resume it before rerunning" in err(client.post(f"{r}/agents/extractor-01/rerun",
                                                           json={"instructions": "x"}), 409)
    assert err(client.post(f"{r}/resume"), 409) == "not built yet (V6-resume)"
    assert "Nothing to rerun after complete" in err(client.post(f"{r}/rerun", json={"from_stage": "complete",
                                                                                     "instructions": "x"}), 409)
    assert err(client.post(f"{r}/rerun", json={"from_stage": "repair", "instructions": "x"}), 409) == \
        f"{state.run.run_id} never reached repair"
    assert err(client.post(f"{r}/rerun", json={"from_stage": "extract", "instructions": "x"}), 409) == \
        "not built yet (V8-fork)"


def test_routes_return_what_the_real_modules_return(api, monkeypatch, settings):
    """Once a later plan fills a module, the route answers like the mock: status code and contract shape."""
    client, state = api
    r = run_path(state)
    calls: dict[str, tuple] = {}

    async def stop_agent(ctx, agent_id, reason):
        calls["stop"] = (agent_id, reason)
        a = ctx.state.agents[agent_id]
        a.status = "stopped"
        return a

    async def rerun_agent(ctx, agent_id, instructions, recheck, *, by="analyst"):
        calls["rerun"] = (agent_id, instructions, recheck, by)
        return AgentRunner(ctx, "extractor", "rerun", [], "small", agent_id=f"{agent_id}.2",
                           parent_agent_id=agent_id, instructions=instructions, rerun_by=by).agent

    async def send_message(ctx, agent_id, text):
        return m.AgentMessage(message_id="AM-001", agent_id=agent_id, author="analyst", text=text, status="sent",
                              created_at=now_iso())

    monkeypatch.setattr(agentstop, "stop_agent", stop_agent)
    monkeypatch.setattr(rework, "rerun_agent", rerun_agent)
    monkeypatch.setattr(chat, "send_message", send_message)
    monkeypatch.setattr(chat, "list_messages", lambda st, aid: m.AgentMessageList(messages=[]))

    got = client.post(f"{r}/agents/verifier-D-001-1/stop", json={})
    assert got.status_code == 200 and m.Agent.model_validate(got.json()).status == "stopped"
    assert calls["stop"] == ("verifier-D-001-1", "Stopped by the analyst")
    got = client.post(f"{r}/agents/extractor-01/rerun", json={"instructions": "Use page 138"})
    new = m.Agent.model_validate(got.json())
    assert got.status_code == 201 and new.agent_id == "extractor-01.2" and new.parent_agent_id == "extractor-01"
    assert new.rerun_by == "analyst" and new.name and calls["rerun"] == ("extractor-01", "Use page 138", True,
                                                                         "analyst")
    got = client.post(f"{r}/agents/bobby/messages", json={"text": "Why proceed?"})
    assert got.status_code == 201 and m.AgentMessage.model_validate(got.json()).message_id == "AM-001"
    assert client.get(f"{r}/agents/bobby/messages").json() == {"messages": []}

    async def refuse(ctx, *a, **k):
        raise InvalidState("This agent's page was quarantined")

    monkeypatch.setattr(rework, "rerun_agent", refuse)
    assert err(client.post(f"{r}/agents/extractor-01/rerun", json={"instructions": "x"}), 409) == \
        "This agent's page was quarantined"

    client.post(f"{r}/stop", json={})

    async def resume_run(ctx, reason=None):
        calls["resume"] = reason
        ctx.state.run.status = "running"
        return ctx.state.run

    async def fork_run(store, vault, settings_, parent_state, req):
        child = store.create_run(SHL)
        child.run.parent_run_id, child.run.rerun_from = parent_state.run.run_id, req.from_stage
        child.run.instructions = req.instructions
        return RunContext.create(child, store, vault, settings_)

    monkeypatch.setattr(fork, "fork_run", fork_run)
    got = client.post(f"{r}/rerun", json={"from_stage": "extract", "instructions": "Prefer primary filings"})
    child = m.Run.model_validate(got.json())
    assert got.status_code == 201 and child.parent_run_id == state.run.run_id and child.rerun_from == "extract"
    assert client.get(f"{P}/runs/{child.run_id}").status_code == 200

    monkeypatch.setattr(resume, "resume_run", resume_run)
    got = client.post(f"{r}/resume", json={"reason": "carry on"})
    assert got.status_code == 200 and m.Run.model_validate(got.json()).status == "running"
    assert calls["resume"] == "carry on"


def test_the_comment_route_hands_the_comment_to_bobby(api, monkeypatch):
    client, state = api
    r = run_path(state)
    seen: list[str] = []

    async def on_comment(ctx, comment):
        seen.append(comment.comment_id)

    monkeypatch.setattr(comments, "on_comment", on_comment)
    got = client.post(f"{r}/comments", json={"target_type": "run", "text": "Look at leverage again"})
    assert got.status_code == 201 and got.json()["status"] == "queued"
    deadline = time.monotonic() + 3
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [got.json()["comment_id"]]
    client.post(f"{r}/stop", json={})
    got = client.post(f"{r}/comments", json={"target_type": "run", "text": "after stop"})
    assert got.json()["status"] == "rejected"
    time.sleep(0.05)
    assert len(seen) == 1                                    # a rejected comment never reaches Bobby


def test_the_default_comment_hook_leaves_the_comment_queued(api):
    client, state = api
    got = client.post(f"{run_path(state)}/comments", json={"target_type": "run", "text": "noted"})
    assert got.status_code == 201
    time.sleep(0.05)
    assert client.get(f"{run_path(state)}/comments").json()["comments"][0]["status"] == "queued"


def test_new_runs_report_honest_v2_defaults(api):
    client, _ = api
    run = m.Run.model_validate(client.post(f"{P}/runs", json=SHL.model_dump()).json())
    assert run.cache_mode == "off" and run.parent_run_id is None and run.rerun_from is None
    assert run.instructions is None
    client.post(f"{P}/runs/{run.run_id}/stop", json={})


async def test_spawned_side_tasks_are_kept_and_errors_logged(settings, caplog):
    ctx = make_ctx(settings)

    async def fails():
        raise RuntimeError("side task broke")

    task = ctx.spawn(fails(), name="probe")
    assert task in ctx.background
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert task not in ctx.background and "side task" in caplog.text
