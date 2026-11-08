"""B-cli: the axelrod CLI against the real API (uvicorn on a free port, STORE=local, FAKE_MODELS=1), the demo
controls (inject_errors, patch_item) and scripts/export_run.py. All offline.

The report data for the demo tests is the mock's fixture (a fictional company), loaded into a RunState.
The traffic stage in the CLI test is simulated: one real gateway decision (a default-deny block, decided
before any network call) and labeled stand-in events for the allowed, flagged and model-call lines.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from backend.axelrod import cli, demo, orchestrator
from backend.axelrod.api import ApiError, create_app
from backend.axelrod.contract import m
from backend.axelrod.gateway import Blocked
from backend.axelrod.ids import new_run_id, now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import RunState, Store

RUN_ID_RE = re.compile(r"run_\d{8}_[a-z0-9]{6}")
EVENT = TypeAdapter(m.Event)
ITEM = TypeAdapter(m.Item)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_run.py"


# ---------------------------------------------------------------------------
# helpers and fixtures
# ---------------------------------------------------------------------------


def fixture_state(bundle: dict, *, status: str = "done") -> RunState:
    """The mock's finished report (fictional company) as a live RunState with stored page texts."""
    b = copy.deepcopy({k: v for k, v in bundle.items() if k != "page_text"})
    now = now_iso()
    run = m.Run(run_id=new_run_id(), company=b["company"], ticker=b["ticker"], status=status,
                stage="complete" if status == "done" else "collect", created_at=now, updated_at=now,
                created_via="api", notes="test fixture (fictional company)", counts=m.RunCounts(),
                usage=m.Usage(budget_usd=5.0))
    state = RunState(
        run=run, sources=[m.Source.model_validate(s) for s in b["sources"]],
        documents=[m.Document.model_validate(d) for d in b["docs"]],
        pages={d["doc_id"]: [bundle["page_text"](d["doc_id"], p) for p in range(1, d["pages"] + 1)]
               for d in b["docs"]},
        items={i["id"]: ITEM.validate_python(i) for i in b["items"]})
    state.recount()
    return state


@pytest.fixture
def store(settings, tmp_path) -> Store:
    return Store(settings, replay_dir=tmp_path / "replay")


@pytest.fixture
def fx_ctx(settings, store, fixture_bundle) -> RunContext:
    state = fixture_state(fixture_bundle)
    store.put(state)
    return RunContext.create(state, store, store.vault, settings)


@pytest.fixture
def server(settings, store):
    """Start the real API on a free loopback port in a thread. Yields a function that returns the base URL."""
    import uvicorn

    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start() -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(create_app(settings, store=store), host="127.0.0.1", port=port,
                                            log_level="warning", lifespan="on", loop="asyncio"))
        t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
        t.start()
        deadline = time.monotonic() + 15
        while not srv.started:
            assert t.is_alive() and time.monotonic() < deadline, "the API did not start"
            time.sleep(0.02)
        servers.append((srv, t))
        return f"http://127.0.0.1:{port}"            # no /api/v1: the CLI adds it

    yield start
    for srv, t in servers:
        srv.should_exit = True
        t.join(15)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.delenv(cli.API_ENV, raising=False)
    monkeypatch.delenv(cli.KEY_ENV, raising=False)


def run_cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.build_app(), list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def wait_for(fn, timeout: float = 15.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def ev(type: str, data: dict, *, seq: int = 1, agent_id: str | None = None, stage: str = "collect") -> dict:
    """A contract-valid event as the API serves it (JSON)."""
    raw = {"seq": seq, "ts": "2026-11-08T03:04:05.000Z", "run_id": "run_20261108_test01", "type": type,
           "stage": stage, "agent_id": agent_id, "level": "info", "message": f"{type} (test)", "data": data}
    return EVENT.validate_python(raw).model_dump(mode="json")


def gw(decision: str, action: str = "fetch", **kw) -> dict:
    data = {"request_id": "REQ-0001", "role": "fetcher" if action == "fetch" else "extractor", "action": action,
            "target": kw.pop("target", "https://www.example-filings.com/ar/2025.pdf"), "decision": decision,
            "rule_id": kw.pop("rule_id", None), "reason": kw.pop("reason", "approved source"),
            "source_id": kw.pop("source_id", None), "latency_ms": 12}
    data.update(kw)
    return ev("gateway.request", data, agent_id="fetcher-01" if action == "fetch" else "extractor-03")


# ---------------------------------------------------------------------------
# CLI pieces (no server)
# ---------------------------------------------------------------------------


def test_api_base_adds_the_prefix_once(monkeypatch) -> None:
    assert cli.api_base("https://abc.execute-api.us-east-1.amazonaws.com") == \
        "https://abc.execute-api.us-east-1.amazonaws.com/api/v1"
    assert cli.api_base("http://localhost:8000/api/v1/") == "http://localhost:8000/api/v1"
    assert cli.api_base(None) == cli.DEFAULT_API
    monkeypatch.setenv(cli.API_ENV, "http://10.0.0.5:8000")
    assert cli.api_base(None) == "http://10.0.0.5:8000/api/v1"


def test_ui_link_forms() -> None:
    assert cli.ui_link("run_1", "https://ui.example.dev") == "https://ui.example.dev?run=run_1"
    assert cli.ui_link("run_1", "https://ui.example.dev/?speed=2") == "https://ui.example.dev/?speed=2&run=run_1"
    assert cli.ui_link("run_1", "https://ui.example.dev/runs/{run_id}") == "https://ui.example.dev/runs/run_1"
    assert cli.ui_link("run_1", "") is None


def test_decisions_for_all_and_reject() -> None:
    sources = [{"source_id": "S-001", "tier": "priority"}, {"source_id": "S-002", "tier": "tier_2"},
               {"source_id": "S-003", "tier": "tier_3"}]
    assert cli.decisions_for(sources, approve_all=True, reject=["S-003"]) == [
        {"source_id": "S-001", "approval": "approved"}, {"source_id": "S-002", "approval": "approved"},
        {"source_id": "S-003", "approval": "rejected"}]
    # Without --all, non-priority sources are left out, so the API auto-approves them.
    assert cli.decisions_for(sources, approve_all=False, reject=["S-001"]) == [
        {"source_id": "S-001", "approval": "rejected"}]


def test_tail_lines_are_colored_by_decision() -> None:
    st = cli.TailState()
    lines = {
        "allowed": cli.render_event(gw("allowed", source_id="S-001", rule_id="R-allow-S-001", http_status=200,
                                       bytes=2_400_000), st),
        "blocked": cli.render_event(gw("blocked", target="https://example.org/collect?id=7",
                                       reason="link-follow: not an approved source (default deny)"), st),
        "flagged": cli.render_event(gw("flagged", source_id="S-010", target="https://api.example.dev/test-pages/x.html",
                                       reason="Guardrails: prompt attack detected"), st),
    }
    for decision, line in lines.items():
        label = line.segments[2]
        assert label[0].strip() == decision.upper()
        assert cli.DECISION_STYLE[decision] in label[1]
    assert "HTTP 200 2.4 MB" in lines["allowed"].plain() and "(R-allow-S-001)" in lines["allowed"].plain()
    assert "example.org" in lines["blocked"].plain() and "default deny" in lines["blocked"].plain()
    assert "prompt attack" in lines["flagged"].plain()
    assert st.counts == {"allowed": 1, "blocked": 1, "flagged": 1}

    # Real ANSI colors on a terminal: green, red, yellow.
    buf = io.StringIO()
    console = cli.make_console(file=buf, force_terminal=True, color_system="standard", width=200)
    for line in lines.values():
        cli.print_line(console, line)
    out = buf.getvalue().splitlines()
    assert "\x1b[1;32mALLOWED" in out[0] and "\x1b[1;31mBLOCKED" in out[1] and "\x1b[1;33mFLAGGED" in out[2]


def test_tail_shows_running_cost_and_stage_changes() -> None:
    st = cli.TailState()
    first = cli.render_event(gw("allowed", action="model.invoke", target="us.anthropic.claude-haiku-4-5",
                                rule_id="R-sys-models", tokens_in=1234, tokens_out=210, cost_usd=0.0021), st)
    assert "1,234 in / 210 out" in first.plain() and "$0.0021" in first.plain() and "total $0.0021" in first.plain()
    usage = ev("usage.updated", {"usage": {"model_calls": 1, "tokens_in": 1234, "tokens_out": 210,
                                           "cost_usd": 0.0021, "budget_usd": 5.0}})
    assert cli.render_event(usage, st) is None                       # quiet by default, but it sets the total
    second = cli.render_event(gw("allowed", action="model.invoke", target="us.anthropic.claude-haiku-4-5",
                                 tokens_in=1000, tokens_out=100, cost_usd=0.0010), st)
    assert "total $0.0031" in second.plain()

    stage = cli.render_event(ev("run.status", {"status": "running", "stage": "extract"}), st)
    assert stage.rule and "stage extract" in stage.plain() and "cost $0.0031" in stage.plain()
    assert cli.render_event(ev("run.status", {"status": "running", "stage": "extract"}), st) is None
    stopped = cli.render_event(ev("run.stopped", {"reason": "demo stop"}), st)
    assert "STOPPED" in stopped.plain() and "kill switch: demo stop" in stopped.plain()
    # Noise stays out unless --all.
    agent = {"agent_id": "extractor-03", "role": "extractor", "status": "working", "task": "pages 1-3"}
    spawned = ev("agent.spawned", {"agent": agent})
    assert cli.render_event(spawned, st) is None
    assert cli.render_event(spawned, cli.TailState(show_all=True)) is not None


# ---------------------------------------------------------------------------
# demo.inject_errors
# ---------------------------------------------------------------------------


def test_digit_slips_change_the_reported_value_first() -> None:
    slips = list(demo.digit_slips("Revenue was 22,363 in fiscal 2025", "22,363"))
    assert slips[0] == "Revenue was 22,364 in fiscal 2025"
    assert all(len(s) == len("Revenue was 22,363 in fiscal 2025") for s in slips)


async def test_inject_errors_catches_three_of_three(fx_ctx: RunContext) -> None:
    state = fx_ctx.state
    before = {iid: it.model_copy(deep=True) for iid, it in state.items.items()}
    resp = await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=3), repair_delay=0)
    assert len(resp.item_ids) == 3
    assert "caught 3 of 3" in resp.note and "Demo control" in resp.note

    injected = [e for e in state.events if e.type == "item.updated" and e.data.reason == "demo_injection"]
    assert [e.data.item.id for e in injected] == resp.item_ids
    for e in injected:
        assert e.message.startswith("DEMO")
        old_q = next(c.quote for c in before[e.data.item.id].citations if c.role == "evidence")
        new_q = next(c.quote for c in e.data.item.citations if c.role == "evidence")
        diff = [(a, b) for a, b in zip(old_q, new_q) if a != b]
        assert len(old_q) == len(new_q) and len(diff) == 1 and all(ch.isdigit() for ch in diff[0])

    caught = [e for e in state.events if e.type == "item.verified" and e.data.item_id in resp.item_ids
              and e.data.verification.status == "fail"]
    assert len(caught) == 3
    for e in caught:
        qe = next(c for c in e.data.verification.checks if c.check == "quote_exact")
        assert qe.result == "fail" and qe.diff and qe.closest_locator is not None
        assert any(seg.op == "replace" for seg in qe.diff)
        assert e.data.certainty.band == "none"

    for iid in resp.item_ids:
        it, orig = state.items[iid], before[iid]
        assert it.verification.status == "fixed"
        last = it.verification.attempts[-1]
        assert last.action == "recite" and last.status == "fail" and "DEMO INJECTION" in last.note
        assert [c.quote for c in it.citations] == [c.quote for c in orig.citations]   # re-cited to the real text
        assert it.certainty.score == orig.certainty.score
    # Untouched items stay as they were, and the run counts see the fixed items.
    assert all(state.items[i].verification.status == before[i].verification.status
               for i in state.items if i not in resp.item_ids)
    assert state.run.counts.items_fixed == sum(i.verification.status == "fixed" for i in state.items.values())


async def test_inject_errors_dependents_lose_and_regain_certainty(fx_ctx: RunContext) -> None:
    resp = await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=1), repair_delay=0)
    iid = resp.item_ids[0]
    deps = demo.dependents(fx_ctx.state.items, iid)
    assert deps, "the fixture item should feed a conclusion"
    drops = [e for e in fx_ctx.state.events if e.type == "item.verified" and e.data.item_id == deps[0]]
    assert len(drops) == 2 and drops[0].data.certainty.score < drops[1].data.certainty.score


async def test_inject_errors_delayed_repair_shows_the_failure_first(fx_ctx: RunContext) -> None:
    resp = await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=2), repair_delay=0.05)
    assert "In 0.05 s" in resp.note
    assert all(fx_ctx.state.items[i].verification.status == "fail" for i in resp.item_ids)
    for _ in range(100):
        if all(fx_ctx.state.items[i].verification.status == "fixed" for i in resp.item_ids):
            break
        await asyncio.sleep(0.02)
    assert all(fx_ctx.state.items[i].verification.status == "fixed" for i in resp.item_ids)


async def test_inject_errors_only_uses_items_that_pass_now(fx_ctx: RunContext) -> None:
    """An item marked pass whose quote no longer matches the page is skipped: the failure must be the injection's."""
    state = fx_ctx.state
    first = demo._candidates(dict(state.items), demo._pages(state))[0]
    k = next(i for i, c in enumerate(first.citations) if c.role == "evidence")
    first.citations[k] = first.citations[k].model_copy(update={"quote": first.citations[k].quote + " (edited)"})
    resp = await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=3), repair_delay=0)
    assert first.id not in resp.item_ids and len(resp.item_ids) == 3


async def test_inject_errors_without_verified_quotes_is_409(settings, store) -> None:
    now = now_iso()
    state = RunState(run=m.Run(run_id=new_run_id(), company="Empty Co", status="running", stage="collect",
                               created_at=now, updated_at=now, counts=m.RunCounts(), usage=m.Usage()))
    store.put(state)
    ctx = RunContext.create(state, store, store.vault, settings)
    with pytest.raises(ApiError) as err:
        await demo.inject_errors(ctx, m.InjectErrorsRequest(count=3))
    assert err.value.status == 409


async def test_inject_errors_on_a_stopped_run_is_409(fx_ctx: RunContext) -> None:
    fx_ctx.state.kill_switch = True
    with pytest.raises(ApiError) as err:
        await demo.inject_errors(fx_ctx, m.InjectErrorsRequest(count=1))
    assert err.value.status == 409


# ---------------------------------------------------------------------------
# demo.patch_item
# ---------------------------------------------------------------------------


async def test_patch_item_reverifies_and_cascades(fx_ctx: RunContext) -> None:
    state = fx_ctx.state
    did = "D-022"
    dep = demo.dependents(state.items, did)[0]
    good = state.items[did].statement
    assert state.items[dep].verification.status in ("pass", "fixed")

    bad = await demo.patch_item(fx_ctx, did, m.ItemPatch(text="The normalized tax rate is 35%."))
    assert bad.verification.status == "fail"
    assert any(c.check == "numbers_traced" and c.result == "fail" for c in bad.verification.checks)
    att = bad.verification.attempts[-1]
    assert att.action == "user_edit" and att.before == good and att.after == "The normalized tax rate is 35%."
    assert state.items[dep].verification.status in ("fail", "blocked")
    reasons = [e.data.reason for e in state.events if e.type == "item.updated"]
    assert reasons == ["user_edit"]

    fixed = await demo.patch_item(fx_ctx, did, m.ItemPatch(text=good))
    assert fixed.verification.status == "fixed" and fixed.certainty.band != "none"
    assert state.items[dep].verification.status == "fixed"      # it failed in between, then passed again
    assert len(fixed.verification.attempts) == len(bad.verification.attempts) + 1


async def test_patch_item_marks_old_judge_votes_as_stale(fx_ctx: RunContext) -> None:
    state = fx_ctx.state
    judged = next(i for i in state.conclusions()
                  if any(c.method == "llm_judge" for c in i.verification.checks) and i.verification.status == "pass")
    edited = await demo.patch_item(fx_ctx, judged.id, m.ItemPatch(text=judged.text + " "))   # same text: no-op
    assert edited is state.items[judged.id] and not [e for e in state.events if e.type == "item.updated"]
    edited = await demo.patch_item(fx_ctx, judged.id, m.ItemPatch(text=judged.text.rstrip(".") + ", as reported."))
    judges = [c for c in edited.verification.checks if c.method == "llm_judge"]
    assert judges and all(c.detail.startswith(demo.STALE_PREFIX) for c in judges)


async def test_patch_item_rejects_charts_and_empty_text(fx_ctx: RunContext) -> None:
    chart = fx_ctx.state.charts()[0]
    with pytest.raises(ApiError) as err:
        await demo.patch_item(fx_ctx, chart.id, m.ItemPatch(text="new title"))
    assert err.value.status == 422
    with pytest.raises(ApiError) as err:
        await demo.patch_item(fx_ctx, "D-022", m.ItemPatch(text="   "))
    assert err.value.status == 422
    with pytest.raises(ApiError) as err:
        await demo.patch_item(fx_ctx, "D-999", m.ItemPatch(text="x"))
    assert err.value.status == 404


# ---------------------------------------------------------------------------
# The CLI against the real API
# ---------------------------------------------------------------------------


async def simulated_traffic(ctx: RunContext) -> None:
    """Test stand-in for collect (SIMULATED). One real gateway decision: example.org is not an approved source,
    so default deny blocks it before any network call. The other lines are labeled stand-in events."""
    from backend.axelrod.agents.base import AgentRunner

    agent = AgentRunner(ctx, "fetcher", "simulated traffic for the CLI test", [], None).agent
    with pytest.raises(Blocked):
        await ctx.gateway.fetch(agent, "https://example.org/collect?id=7", why="link-follow (test)")
    s1 = ctx.state.sources_by_id()["S-001"]
    ctx.emit("gateway.request", m.GatewayRequestData(
        request_id=ctx.next_id("REQ"), role="fetcher", action="fetch", target=s1.url, decision="allowed",
        rule_id="R-allow-S-001", reason="approved source (simulated in test)", source_id="S-001", latency_ms=9,
        http_status=200, bytes=2_400_000), message="simulated allowed fetch", agent_id=agent.agent_id)
    ctx.emit("gateway.request", m.GatewayRequestData(
        request_id=ctx.next_id("REQ"), role="fetcher", action="fetch", target="https://test.invalid/injection.html",
        decision="flagged", reason="Guardrails: prompt attack (simulated in test)", source_id="S-010",
        latency_ms=30), message="simulated flagged page", agent_id=agent.agent_id)
    ctx.emit("gateway.request", m.GatewayRequestData(
        request_id=ctx.next_id("REQ"), role="extractor", action="model.invoke", target=ctx.settings.model_small,
        decision="allowed", rule_id="R-sys-models", reason="role scope (simulated in test)", latency_ms=800,
        tokens_in=1500, tokens_out=300, cost_usd=0.0123), message="simulated model call", agent_id="extractor-01")
    ctx.emit("usage.updated", {"usage": {"model_calls": 1, "tokens_in": 1500, "tokens_out": 300, "cost_usd": 0.0123,
                                         "budget_usd": 5.0}}, message="simulated usage")
    while True:
        ctx.check_stop()
        await asyncio.sleep(0.02)


def test_cli_run_approve_tail_revoke_stop(server, store, monkeypatch) -> None:
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setattr(orchestrator, "STAGES",
                        [("plan", "plan"), ("approve", "approve"), ("collect", simulated_traffic)])
    base = server()
    client = cli.Client(cli.api_base(base))

    code, out = run_cli("run", "Siemens Healthineers", "--ticker", "SHL", "--api", base)
    assert code == 0, out
    run_id = RUN_ID_RE.search(out).group(0)
    assert "created for Siemens Healthineers" in out and "via cli" in out and f"tail {run_id}" in out
    assert client.get(f"/runs/{run_id}")["created_via"] == "cli"

    code, out = run_cli("approve", run_id, "--all", "--reject", "s-009", "--api", base)
    assert code == 0, out
    assert "Gateway policy v" in out and "default deny" in out
    plan = {s["source_id"]: s["approval"] for s in client.get(f"/runs/{run_id}/plan")["sources"]}
    assert plan["S-009"] == "rejected" and plan["S-001"] == "approved" and plan["S-006"] == "approved"

    wait_for(lambda: client.get(f"/runs/{run_id}")["usage"]["cost_usd"] > 0)
    code, out = run_cli("tail", run_id, "--once", "--api", base)
    assert code == 0, out
    for needle in ("BLOCKED", "example.org", "ALLOWED", "HTTP 200 2.4 MB", "FLAGGED", "prompt attack",
                   "1,500 in / 300 out", "total $0.0123", "stage plan", "stage approve", "awaiting_approval",
                   "stage collect", "APPROVAL", "rejected S-009", "POLICY"):
        assert needle in out, (needle, out)

    code, out = run_cli("revoke", run_id, "S-006", "--reason", "demo revoke", "--api", base)
    assert code == 0, out
    assert "REVOKED" in out and "R-revoke-S-006" in out and "next request to it is blocked" in out
    code, out = run_cli("revoke", run_id, "S-006", "--api", base)
    assert code == 1 and "409" in out                                   # already revoked

    code, out = run_cli("status", run_id, "--api", base)
    # allowed 3: two fetches plus Bobby's plan-gate call (V3-rework), an allowed model call by the orchestrator
    assert code == 0 and "running at collect" in out and "allowed 3" in out and "blocked 1" in out \
        and "flagged 1" in out and "$0.0123" in out, out

    code, out = run_cli("stop", run_id, "--reason", "demo stop", "--api", base)
    assert code == 0 and "STOPPED" in out and "demo stop" in out, out
    wait_for(lambda: client.get(f"/runs/{run_id}")["status"] == "stopped")

    # Without --once, tail follows the run and exits by itself once it is finished.
    code, out = run_cli("tail", run_id, "--interval", "0.05", "--api", base)
    assert code == 0 and "STOPPED" in out and "kill switch: demo stop" in out and "stopped at" in out, out
    assert "SOURCE" in out and "S-006 revoked" in out

    code, out = run_cli("runs", "--api", base)
    assert code == 0 and run_id in out


def test_cli_inject_errors_three_of_three(server, store, fixture_bundle, monkeypatch) -> None:
    monkeypatch.setattr(demo, "REPAIR_DELAY_S", 0)
    state = fixture_state(fixture_bundle)
    store.put(state)
    base = server()
    code, out = run_cli("inject-errors", state.run.run_id, "--count", "3", "--api", base)
    assert code == 0, out
    assert "DEMO CONTROL" in out and "caught 3 of 3" in out
    ids = [e.data.item.id for e in state.events if e.type == "item.updated" and e.data.reason == "demo_injection"]
    assert len(ids) == 3 and all(state.items[i].verification.status == "fixed" for i in ids)
    code, out = run_cli("tail", state.run.run_id, "--once", "--api", base)
    assert out.count("DEMO") >= 6 and "FAIL" in out and "FIXED" in out, out

    # The analyst edit route runs demo.patch_item too: the response is the re-verified item.
    client = cli.Client(cli.api_base(base))
    item = ITEM.validate_python(client.request("PATCH", f"/runs/{state.run.run_id}/items/D-022",
                                               {"text": "The normalized tax rate is 35%."}))
    assert item.verification.status == "fail" and item.verification.attempts[-1].action == "user_edit"


def test_cli_errors_are_one_line(server, monkeypatch) -> None:
    code, out = run_cli("approve", "run_20261108_nope00")
    assert code == 2 and "--all" in out
    base = server()
    code, out = run_cli("status", "run_20261108_nope00", "--api", base)
    assert code == 1 and "404 not_found" in out
    with socket.socket() as s:                       # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    code, out = run_cli("status", "run_20261108_nope00", "--api", dead)
    assert code == 2 and "Cannot reach the Axelrod API" in out


def test_cli_sends_the_api_key(settings, store, monkeypatch) -> None:
    keyed = settings.model_copy(update={"api_key": "test-key-not-secret"})
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(keyed, store=store), log_level="warning", loop="asyncio"))
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    try:
        wait_for(lambda: srv.started)
        base = f"http://127.0.0.1:{port}"
        code, out = run_cli("runs", "--api", base)
        assert code == 1 and "401" in out
        monkeypatch.setenv(cli.KEY_ENV, "test-key-not-secret")
        code, out = run_cli("runs", "--api", base)
        assert code == 0 and "test-key-not-secret" not in out
    finally:
        srv.should_exit = True
        t.join(15)


# ---------------------------------------------------------------------------
# scripts/export_run.py
# ---------------------------------------------------------------------------


def _export_module():
    spec = importlib.util.spec_from_file_location("export_run", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_run_writes_a_text_only_replay(settings, store, fixture_bundle, tmp_path, capsys) -> None:
    state = fixture_state(fixture_bundle)
    store.put(state)
    store.append(state, "run.created", {"run": state.run}, message="Run created (test fixture)", stage="plan")
    store.append(state, "run.completed", {}, message="Run complete (test fixture)", stage="complete")
    store.vault.put_text(state.run.run_id, "01-Annual-Reports/fy2025.md", "# FY2025\n\nRevenue €22,363 million")
    store.vault.put_bytes(state.run.run_id, "01-Annual-Reports/fy2025.pdf", b"%PDF-1.7 not really")
    store.save(state)

    export = _export_module()
    dest = tmp_path / "replay" / "shl-test"
    assert export.main([state.run.run_id, "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "Exported" in out and "2 events" in out
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert "state.json" in files and "events.jsonl" in files
    assert any(f.endswith("fy2025.md") for f in files) and not any(f.endswith(".pdf") for f in files)

    replay_store = Store(settings.model_copy(update={"data_dir": tmp_path / "empty"}), replay_dir=tmp_path / "replay")
    replay_store.load_all()
    loaded = replay_store.get(state.run.run_id)
    assert loaded.run.mode == "replay" and len(loaded.events) == 2 and len(loaded.items) == len(state.items)

    assert export.main([state.run.run_id, "--dest", str(dest)]) == 1          # never overwrites
    assert export.main(["run_20261108_nope00", "--dest", str(tmp_path / "x")]) == 1


def test_export_run_refuses_unfinished_runs(settings, store, fixture_bundle, tmp_path) -> None:
    state = fixture_state(fixture_bundle, status="running")
    store.put(state)
    store.save(state)
    assert _export_module().main([state.run.run_id, "--dest", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()
