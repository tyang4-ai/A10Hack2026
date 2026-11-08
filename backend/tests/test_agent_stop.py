"""V2-gateway: stop one agent while the run keeps going (CONTRACT.md 13, docs/BACKEND_V2.md step 2), offline.

stop_agent marks the agent stopped (agent.status with reason), then adds R-stop-<agent_id> with policy.updated.
decide() denies that agent right after the kill switch check, a result that lands after the stop is
discarded, the rule survives every later policy rebuild, the collect stage stops requesting for a stopped
fetcher, and finalize declares the stopped agent's work as a Hole. Stopping Bobby is visible to the gates.
Model answers and scans come from FAKE_MODELS fakes (simulated).
"""
from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.axelrod import agentstop, bedrock, gates
from backend.axelrod.agents.base import AgentRunner, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.contract import m
from backend.axelrod.fetch import http
from backend.axelrod.gateway import KILL_REASON, Blocked
from backend.axelrod.runtime import InvalidState, RunContext
from backend.axelrod.stages import collect, finalize
from backend.axelrod.store import Store

P = "/api/v1"
SHL = m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api")
URLS = {f"S-00{i}": f"https://www.ir.example.com/reports/report-{i}.json" for i in (1, 2, 3)}


class Verdict(BaseModel):
    """Test output for the fake model (simulated)."""

    TOOL_NAME: ClassVar[str] = "agent_stop_verdict"
    ok: bool


def src(sid: str, url: str, approval: str = "approved") -> m.Source:
    return m.Source(source_id=sid, title=f"Source {sid}", url=url, domain=url.split("/")[2], kind="data_api",
                    data_types=["financial_filing"], access=m.SourceAccess(), tier="priority", rank=int(sid[-1]),
                    scores=m.SourceScores(reliability=0.9, relevance=0.9), rationale="test (simulated)",
                    need_ids=["N-01"], criteria=["leverage"], vault_folder="01-Annual-Reports",
                    proposed_by="planner", approval=approval, status="queued")


def make_ctx(settings, *, approve: bool = True) -> RunContext:
    store = Store(settings)
    state = store.create_run(SHL)
    state.sources = [src(sid, url) for sid, url in URLS.items()]
    state.run.stage = "collect"
    ctx = RunContext.create(state, store, store.vault, settings)
    gates.spawn_bobby(ctx)
    if approve:
        ctx.gateway.build_policy()
    return ctx


def register(ctx: RunContext, agent_id: str, role: str, status: str = "working", **kw) -> m.Agent:
    return ctx.state.register_agent(m.Agent(agent_id=agent_id, role=role, status=status,
                                            task=kw.pop("task", f"test {role}"), **kw))


def events(ctx: RunContext, type_: str) -> list[m.Event]:
    return [e for e in ctx.state.events if e.type == type_]


@pytest.fixture(autouse=True)
def _clean_fakes():
    saved, saved_scan = dict(bedrock._FAKES), bedrock._fake_scan
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)
    bedrock.register_fake_scan(saved_scan)


@pytest.fixture
def net(monkeypatch) -> list[str]:
    """Every live GET. Each planned URL answers a small JSON document (simulated)."""
    calls: list[str] = []

    async def fake_get_once(url, *, user_agent, **_):
        calls.append(url)
        return 200, {"content-type": "application/json"}, b'{"facts": {"simulated": true}}'

    monkeypatch.setattr(http, "get_once", fake_get_once)
    return calls


# ---------------------------------------------------------------------------
# stop_agent and the rule
# ---------------------------------------------------------------------------


async def test_stop_marks_the_agent_then_adds_the_rule(settings):
    ctx = make_ctx(settings)
    register(ctx, "extractor-01", "extractor")
    before = ctx.state.policy.version
    seq = ctx.state.run.last_seq
    out = await agentstop.stop_agent(ctx, "extractor-01", "wrong page")
    assert out.status == "stopped" and out.finished_at and ctx.state.agents["extractor-01"].status == "stopped"
    new = [e for e in ctx.state.events if e.seq > seq]
    assert [e.type for e in new] == ["agent.status", "policy.updated"]
    assert (new[0].data.status, new[0].data.reason, new[0].agent_id) == ("stopped", "wrong page", "extractor-01")
    assert new[1].data.rule_ids == ["R-stop-extractor-01"] and new[1].data.version == before + 1
    rule = ctx.state.policy.rules[0]
    assert (rule.rule_id, rule.effect, rule.action, rule.agent_id, rule.origin, rule.note, rule.roles) == (
        "R-stop-extractor-01", "deny", "model.invoke", "extractor-01", "user_agent_stop", "wrong page", [])
    assert ctx.state.run.policy_version == before + 1
    view = ctx.state.policy_view(settings)
    assert any(r.rule_id == "R-stop-extractor-01" for r in view.rules)
    assert agentstop.is_stopped(ctx.state, "extractor-01") and not agentstop.is_stopped(ctx.state, "bobby")


async def test_a_fetcher_stop_rule_names_the_fetch_action(settings):
    ctx = make_ctx(settings)
    register(ctx, "fetcher-01", "fetcher")
    await agentstop.stop_agent(ctx, "fetcher-01", None)
    rule = ctx.state.policy.rules[0]
    assert rule.action == "fetch" and rule.note == "Agent stopped by the analyst"


async def test_stop_refuses_finished_agents_and_runs(settings):
    ctx = make_ctx(settings)
    register(ctx, "extractor-01", "extractor", status="done")
    with pytest.raises(InvalidState, match="already done"):
        await agentstop.stop_agent(ctx, "extractor-01", "x")
    with pytest.raises(KeyError):
        await agentstop.stop_agent(ctx, "nope", "x")
    register(ctx, "extractor-02", "extractor")
    ctx.state.run.status = "done"
    with pytest.raises(InvalidState, match="Run is already done"):
        await agentstop.stop_agent(ctx, "extractor-02", "x")


async def test_decide_order_stop_comes_right_after_the_kill_switch(settings):
    ctx = make_ctx(settings)
    stopped = register(ctx, "fetcher-01", "fetcher")
    other = register(ctx, "fetcher-02", "fetcher")
    await agentstop.stop_agent(ctx, "fetcher-01", "enough")
    gw = ctx.gateway
    assert gw.decide(stopped, "fetch", URLS["S-001"]) == (
        "blocked", "R-stop-fetcher-01", "Agent stopped by the analyst: enough")
    assert gw.decide(other, "fetch", URLS["S-001"])[0] == "allowed"            # the rest of the run continues
    assert gw.decide(stopped, "model.invoke", settings.model_small)[1] == "R-stop-fetcher-01"   # every request
    ctx.state.run.usage.cost_usd = 99.0                                         # over budget: stop still first
    assert gw.decide(stopped, "fetch", URLS["S-001"])[1] == "R-stop-fetcher-01"
    ctx.state.run.usage.cost_usd = 0.0
    gw.set_kill_switch("demo stop")
    assert gw.decide(stopped, "fetch", URLS["S-001"]) == ("blocked", None, KILL_REASON)


async def test_the_rule_survives_rebuilds_revokes_and_the_kill_switch(settings):
    ctx = make_ctx(settings, approve=False)
    register(ctx, "extractor-01", "extractor")
    await agentstop.stop_agent(ctx, "extractor-01", "before approval")
    assert ctx.state.policy is not None and ctx.state.policy.rules[0].rule_id == "R-stop-extractor-01"
    ctx.gateway.build_policy()                                                  # approval rebuilds the rules
    ids = [r.rule_id for r in ctx.state.policy.rules]
    assert ids[0] == "R-stop-extractor-01" and "R-allow-S-001" in ids and ids.count("R-stop-extractor-01") == 1
    ctx.gateway.revoke("S-002", "test")
    assert ctx.state.policy.rules[0].rule_id == "R-stop-extractor-01"
    ctx.gateway.set_kill_switch("stop")
    assert ctx.state.policy.rules[0].rule_id == "R-stop-extractor-01"
    v = ctx.state.policy.version
    ctx.gateway.stop_agent(ctx.state.agents["extractor-01"], "again")           # idempotent
    assert ctx.state.policy.version == v


# ---------------------------------------------------------------------------
# requests from a stopped agent
# ---------------------------------------------------------------------------


async def test_a_stopped_queued_agent_is_blocked_and_stays_stopped(settings):
    ctx = make_ctx(settings)
    bedrock.register_fake(tool_name(Verdict), lambda s, u, schema: {"ok": True})
    runner = AgentRunner(ctx, "extractor", "Extract S-001 page 3", ["S-001"], "small")
    await agentstop.stop_agent(ctx, runner.agent_id, "not needed")
    with pytest.raises(Blocked) as exc:
        with runner:
            await runner.ask("system", "user", Verdict)
    assert exc.value.rule_id == f"R-stop-{runner.agent_id}"
    assert ctx.state.agents[runner.agent_id].status == "stopped"                # not working, not failed
    req = events(ctx, "gateway.request")[-1].data
    assert (req.decision, req.rule_id, req.action) == ("blocked", f"R-stop-{runner.agent_id}", "model.invoke")
    assert ctx.state.run.usage.model_calls == 0


async def test_a_model_result_that_lands_after_the_stop_is_discarded(settings):
    ctx = make_ctx(settings)
    runner = AgentRunner(ctx, "extractor", "Extract S-001 page 4", ["S-001"], "small")

    async def answer_late(system, user, schema):
        await agentstop.stop_agent(ctx, runner.agent_id, "stopped mid-call")    # lands while in flight
        return {"ok": True}

    bedrock.register_fake(tool_name(Verdict), answer_late)
    with pytest.raises(Blocked) as exc:
        with runner:
            await runner.ask("system", "user", Verdict)
    assert exc.value.rule_id == f"R-stop-{runner.agent_id}" and "Result discarded" in exc.value.reason
    req = events(ctx, "gateway.request")[-1].data
    assert req.decision == "blocked" and req.tokens_in and "in flight" in req.reason   # still metered
    assert ctx.state.agents[runner.agent_id].status == "stopped"


async def test_a_fetch_that_lands_after_the_stop_is_discarded(settings, monkeypatch):
    ctx = make_ctx(settings)
    agent = register(ctx, "fetcher-01", "fetcher")

    async def slow_get(url, *, user_agent, **_):
        await agentstop.stop_agent(ctx, "fetcher-01", "mid-fetch")
        return 200, {"content-type": "application/json"}, b"{}"

    monkeypatch.setattr(http, "get_once", slow_get)
    with pytest.raises(Blocked) as exc:
        await ctx.gateway.fetch(agent, URLS["S-001"], source_id="S-001")
    assert exc.value.rule_id == "R-stop-fetcher-01" and "Result discarded" in exc.value.reason


async def test_a_stopped_agent_never_reaches_the_network_or_the_scan(settings, net):
    ctx = make_ctx(settings)
    agent = register(ctx, "fetcher-01", "fetcher")
    await agentstop.stop_agent(ctx, "fetcher-01", "x")
    with pytest.raises(Blocked):
        await ctx.gateway.fetch(agent, URLS["S-001"], source_id="S-001")
    assert net == []
    bedrock.register_fake_scan(lambda text: False)
    scan = await ctx.gateway.scan(agent, URLS["S-001"], "S-001", "some page")
    assert scan.verdict == "error" and "Agent stopped" in scan.detail          # fail closed: quarantined


# ---------------------------------------------------------------------------
# collect and finalize
# ---------------------------------------------------------------------------


async def test_collect_stops_requesting_for_a_stopped_fetcher(settings, net, monkeypatch):
    ctx = make_ctx(settings)
    real = collect.collect_source

    async def then_stop(c, agent, source):
        doc = await real(c, agent, source)
        if source.source_id == "S-001":
            await agentstop.stop_agent(c, agent.agent_id, "enough sources")
        return doc

    monkeypatch.setattr(collect, "collect_source", then_stop)
    await collect.run(ctx)                                                       # the run keeps going
    by_id = ctx.state.sources_by_id()
    assert net == [URLS["S-001"]]
    assert by_id["S-001"].status == "fetched"
    assert by_id["S-002"].status == "blocked" and by_id["S-002"].error.startswith("Agent stopped by the analyst")
    assert by_id["S-003"].status == "skipped" and "fetcher-01 was stopped" in by_id["S-003"].error
    reqs = [e.data for e in events(ctx, "gateway.request")]
    assert [(r.decision, r.rule_id) for r in reqs] == [("allowed", "R-allow-S-001"), ("blocked", "R-stop-fetcher-01")]
    fetcher = ctx.state.agents["fetcher-01"]
    assert fetcher.status == "stopped"                                           # finish_fetcher kept it
    assert [e.data.status for e in events(ctx, "agent.status") if e.agent_id == "fetcher-01"] == ["stopped"]

    holes = finalize.build_holes(ctx, [])
    stopped = [h for h in holes if h.reason.startswith("Agent stopped by the analyst: fetcher-01")]
    assert len(stopped) == 1 and "enough sources" in stopped[0].reason and "S-003" in stopped[0].reason
    assert {h.reason.split(" ")[0] for h in holes if not h.reason.startswith("Agent")} == {"S-002", "S-003"}
    ctx.state.holes = holes
    again = finalize.build_holes(ctx, [])
    assert [h.hole_id for h in again] == [h.hole_id for h in holes]              # ids stay stable


async def test_collect_stop_mid_fetch_discards_that_source_and_skips_the_rest(settings, monkeypatch):
    ctx = make_ctx(settings)
    calls: list[str] = []

    async def get(url, *, user_agent, **_):
        calls.append(url)
        if url == URLS["S-002"]:
            await agentstop.stop_agent(ctx, "fetcher-01", "stop now")              # lands while in flight
        return 200, {"content-type": "application/json"}, b'{"facts": {}}'

    monkeypatch.setattr(http, "get_once", get)
    await collect.run(ctx)
    by_id = ctx.state.sources_by_id()
    assert calls == [URLS["S-001"], URLS["S-002"]]
    assert by_id["S-002"].status == "blocked" and "Result discarded" in by_id["S-002"].error
    assert by_id["S-003"].status == "skipped" and not by_id["S-002"].doc_ids
    assert [r.data.decision for r in events(ctx, "gateway.request")] == ["allowed", "blocked"]


async def test_a_stopped_extractor_is_one_stop_hole_not_a_failed_one(settings):
    ctx = make_ctx(settings)
    register(ctx, "extractor-01", "extractor", task="Extract S-001 page 138", target_ids=["S-001"])
    register(ctx, "extractor-02", "extractor", status="working", task="Extract S-001 page 140")
    await agentstop.stop_agent(ctx, "extractor-01", "wrong page")
    ctx.state.agents["extractor-01"].status = "failed"                           # even if a caller marked it
    ctx.state.agents["extractor-02"].status = "failed"
    reasons = [h.reason for h in finalize.build_holes(ctx, [])]
    assert sum(r.startswith("Agent stopped by the analyst: extractor-01") for r in reasons) == 1
    assert not any(r.startswith("extractor-01 failed") for r in reasons)
    assert any(r.startswith("extractor-02 failed") for r in reasons)
    assert any("Extract S-001 page 138" in r and "wrong page" in r for r in reasons)
    register(ctx, "extractor-01.2", "extractor", status="done", parent_agent_id="extractor-01")
    reasons = [h.reason for h in finalize.build_holes(ctx, [])]                  # its rerun redid the work
    assert not any(r.startswith("Agent stopped by the analyst: extractor-01") for r in reasons)
    assert ctx.gateway.decide(ctx.state.agents["extractor-01.2"], "model.invoke", settings.model_small)[0] == "allowed"


async def test_stopping_bobby_is_visible_to_the_gates(settings):
    ctx = make_ctx(settings)
    await agentstop.stop_agent(ctx, "bobby", "I'll make the calls")
    assert agentstop.is_stopped(ctx.state, "bobby") and ctx.state.agents["bobby"].status == "stopped"
    assert any(r.rule_id == "R-stop-bobby" and r.action == "model.invoke" for r in ctx.state.policy.rules)
    assert any("every later stage gate proceeds" in e.data.text for e in events(ctx, "agent.log"))
    gates.finish_bobby(ctx, "done")
    assert ctx.state.agents["bobby"].status == "stopped"
    bedrock.register_fake(tool_name(Verdict), lambda s, u, schema: {"ok": True})
    with pytest.raises(Blocked) as exc:
        await AgentRunner.attach(ctx, "bobby").ask("system", "digest", Verdict)
    assert exc.value.rule_id == "R-stop-bobby"
    assert any(h.reason.startswith("Agent stopped by the analyst: bobby (Bobby)") for h in finalize.build_holes(ctx, []))


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


@pytest.fixture
def api(settings):
    store = Store(settings)
    state = store.create_run(SHL)
    ctx = RunContext.create(state, store, store.vault, settings)
    gates.spawn_bobby(ctx)
    ctx.gateway.build_policy()
    state.register_agent(m.Agent(agent_id="extractor-01", role="extractor", status="working", task="Extract S-001 p3"))
    state.register_agent(m.Agent(agent_id="extractor-02", role="extractor", status="done", task="Extract S-001 p4"))
    state.run.stage = "extract"
    with TestClient(create_app(settings, store=store)) as client:
        yield client, state


def error(r, status: int, code: str = "invalid_state") -> str:
    assert r.status_code == status, r.text
    body = m.ErrorResponse.model_validate(r.json()).error
    assert body.code == code
    return body.message


def test_route_stops_one_agent_like_the_mock(api):
    client, state = api
    base = f"{P}/runs/{state.run.run_id}/agents"
    r = client.post(f"{base}/extractor-01/stop", json={"reason": "wrong page"})
    assert r.status_code == 200, r.text
    agent = m.Agent.model_validate(r.json())
    assert agent.status == "stopped" and agent.name and agent.agent_id == "extractor-01"
    assert error(client.post(f"{base}/extractor-01/stop", json={}), 409) == "extractor-01 is already stopped"
    assert error(client.post(f"{base}/extractor-02/stop"), 409) == "extractor-02 is already done"
    assert "No agent nope" in error(client.post(f"{base}/nope/stop", json={}), 404, "not_found")
    policy = m.Policy.model_validate(client.get(f"{P}/runs/{state.run.run_id}/policy").json())
    assert policy.rules[0].rule_id == "R-stop-extractor-01" and policy.rules[0].origin == "user_agent_stop"
    agents = m.AgentList.model_validate(client.get(f"{P}/runs/{state.run.run_id}/agents").json()).agents
    assert next(a for a in agents if a.agent_id == "extractor-01").status == "stopped"
    state.run.status = "stopped"
    assert error(client.post(f"{base}/bobby/stop", json={}), 409) == "Run is already stopped"
