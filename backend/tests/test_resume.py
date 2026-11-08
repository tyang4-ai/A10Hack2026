"""V6-resume: resume a stopped run (CONTRACT.md 13, docs/BACKEND_V2.md step 9), offline.

Resume works only on a stopped run (409 with the mock's text otherwise), turns the kill switch off with a
policy version bump (policy.updated, then run.resumed, then run.status running), keeps the analyst's agent
stop rules, brings Bobby back unless the analyst stopped him, and restarts the stage STOP cut into without
redoing finished work (counted on fake models). Also AgentRunner's terminal-status guard.
Model answers come from FAKE_MODELS fakes (simulated). Runs use a short DATA_DIR (Windows MAX_PATH).
"""
from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.axelrod import agentstop, api, bedrock, gates, orchestrator, resume
from backend.axelrod.agents import extractor as ex
from backend.axelrod.agents.base import AgentRunner, revive_orchestrator, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.contract import m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import InvalidState, RunContext
from backend.axelrod.stages import finalize, verify
from backend.axelrod.store import Store

P = "/api/v1"
DATA = Path(__file__).parent / "data" / "planted"
PLANTED = [(DATA / f"annual_report_p{i}.txt").read_text(encoding="utf-8") for i in (1, 2, 3)]
PAGE = re.compile(r"This is PDF page (\d+) of")
NEED = m.DataNeed(need_id="N-01", label="Returns on tangible capital", dimension="financials",
                  question="EBIT, taxes, total assets, cash, goodwill and current liabilities by year?",
                  data_types=["financial_filing"], criteria=["rotic_level"], rationale="test (simulated)")


class Verdict(BaseModel):
    """Test output for the fake model (simulated)."""

    TOOL_NAME: ClassVar[str] = "resume_verdict"
    ok: bool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def short(settings):
    """The offline settings with a short DATA_DIR under backend/.data, one small-model call at a time."""
    root = Path("backend/.data") / f"rs{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True)
    yield settings.model_copy(update={"data_dir": root, "max_concurrency_small": 1})
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_fakes():
    saved, saved_scan = dict(bedrock._FAKES), bedrock._fake_scan
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)
    bedrock.register_fake_scan(saved_scan)


@pytest.fixture
def no_gates(monkeypatch) -> list[str]:
    """Bobby's gates as no-ops that record the stage (his own tests cover his calls)."""
    seen: list[str] = []

    async def after_stage(ctx, stage):
        seen.append(stage)

    monkeypatch.setattr(gates, "after_stage", after_stage)
    return seen


def source(sid: str = "S-901") -> m.Source:
    return m.Source(source_id=sid, title=f"Meridian annual report FY2025 (simulated) {sid}",
                    url=f"https://example.com/{sid}", domain="example.com", kind="annual_report",
                    data_types=["financial_filing"], access=m.SourceAccess(), tier="priority", rank=1,
                    scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data (simulated)",
                    need_ids=["N-01"], criteria=["rotic_level"], vault_folder="01-Annual-Reports",
                    proposed_by="planner", approval="approved", status="fetched")


def document(doc_id: str, sid: str, pages: list[str]) -> m.Document:
    return m.Document(doc_id=doc_id, source_id=sid, title=f"{sid} (simulated)", url=f"https://example.com/{sid}",
                      content_type="pdf", paginated=True, pages=len(pages), sha256="0" * 64,
                      bytes=sum(len(p) for p in pages), fetched_at=now_iso(),
                      vault_paths=m.DocVaultPaths(raw=f"{sid}.raw", text=f"{sid}.md", meta=f"{sid}.meta.json"),
                      scan=m.ScanResult(scanner="none", verdict="skipped"))


def make_ctx(settings, *, stage: str = "extract") -> RunContext:
    """A run with one approved, stored three-page source (the planted annual report), policy built."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Meridian Diagnostics AG", ticker="MDX"))
    state.run.stage = stage
    state.needs = [NEED]
    src = source()
    state.sources.append(src)
    state.documents.append(document("DOC-901", src.source_id, PLANTED))
    state.pages["DOC-901"] = list(PLANTED)
    src.doc_ids.append("DOC-901")
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.gateway.build_policy()
    state.approval_request = m.ApprovalRequest(decisions=[m.SourceDecision(source_id=src.source_id,
                                                                            approval="approved")])
    return ctx


def stop(ctx: RunContext, reason: str = "pause") -> None:
    """What POST /stop does (api.stop_run), without the HTTP layer."""
    state = ctx.state
    state.kill_switch = True
    state.run.stop_reason = reason
    stage = state.run.stage
    gates.finish_bobby(ctx, "stopped")
    ctx.emit("run.status", {"status": "stopped", "stage": stage}, stage=stage, level="warn", message="Kill switch on")
    ctx.emit("run.stopped", {"reason": reason}, stage=stage, level="warn", message=f"Run stopped: {reason}")
    api._kill_policy(ctx, reason)
    ctx.approval.set()


def counting_extractor(ctx_box: list, stop_on: dict[int, int]) -> list[int]:
    """The default fake extractor, counting calls by page. stop_on {page: n}: STOP lands during the n-th
    call for that page (its answer is then discarded by the gateway)."""
    default = bedrock._FAKES[ex.TOOL]
    calls: list[int] = []

    async def fn(system, user, schema):
        page = int(PAGE.search(user).group(1))
        calls.append(page)
        if stop_on.get(page) == calls.count(page):
            stop(ctx_box[0])
        return default(system, user, schema)

    bedrock.register_fake(ex.TOOL, fn)
    return calls


def stages(monkeypatch, entries: list) -> None:
    monkeypatch.setattr(orchestrator, "STAGES", entries)


async def noop(ctx) -> None:
    return None


async def start(ctx: RunContext) -> None:
    ctx.task = asyncio.create_task(orchestrator.run_pipeline(ctx))
    await ctx.task


async def until(cond, limit: float = 5.0) -> None:
    """Let the loop run until cond() holds, failing instead of hanging."""
    await asyncio.wait_for(_poll(cond), limit)


async def _poll(cond) -> None:
    while not cond():
        await asyncio.sleep(0.001)


def of(ctx: RunContext, type_: str, after: int = 0) -> list[m.Event]:
    return [e for e in ctx.state.events if e.type == type_ and e.seq > after]


# ---------------------------------------------------------------------------
# The terminal-status guard (agents/base.py)
# ---------------------------------------------------------------------------


async def test_ask_never_flips_a_stopped_queued_agent_back_to_working(short):
    ctx = make_ctx(short)
    bedrock.register_fake(tool_name(Verdict), lambda s, u, schema: {"ok": True})
    runner = AgentRunner(ctx, "extractor", "Extract S-901 page 9", ["S-901"], "small")
    await agentstop.stop_agent(ctx, runner.agent_id, "not needed")
    seen = len(ctx.state.events)
    with pytest.raises(Exception):
        await runner.ask("system", "user", Verdict)
    statuses = [e.data.status for e in ctx.state.events[seen:] if e.type == "agent.status"]
    assert "working" not in statuses and ctx.state.agents[runner.agent_id].status == "stopped"
    for status in ("working", "queued", "done", "failed"):
        runner._set_status(status)
        assert runner.agent.status == "stopped"


def test_only_resume_revives_and_only_the_orchestrator(short):
    ctx = make_ctx(short)
    bobby = gates.spawn_bobby(ctx)
    goon = AgentRunner(ctx, "extractor", "Extract S-901 page 1", ["S-901"], "small")
    goon.finish("stopped")
    with pytest.raises(ValueError):
        revive_orchestrator(ctx, goon.agent_id)                     # a goon never comes back
    with pytest.raises(ValueError):
        revive_orchestrator(ctx, gates.BOBBY_ID)                    # Bobby is working, not stopped
    gates.finish_bobby(ctx, "stopped")
    assert revive_orchestrator(ctx, gates.BOBBY_ID).status == "working" and bobby.finished_at is None
    gates.finish_bobby(ctx, "done")
    with pytest.raises(ValueError):
        revive_orchestrator(ctx, gates.BOBBY_ID)                    # done is final


# ---------------------------------------------------------------------------
# Resume restarts the stage STOP cut into and keeps finished work
# ---------------------------------------------------------------------------


async def test_resume_in_extract_reads_only_the_pages_left(short, monkeypatch, no_gates):
    ctx = make_ctx(short)
    calls = counting_extractor([ctx], {2: 1})
    stages(monkeypatch, [("extract", "extract"), ("complete", noop)])
    await start(ctx)
    st = ctx.state
    assert st.run.status == "stopped" and st.run.stage == "extract" and calls == [1, 2]
    before = {i.id: i.statement for i in st.data_items()}
    assert before and no_gates == []                                 # page 1's items are in, page 2's discarded

    run = await resume.resume_run(ctx, "go on")
    assert run.status == "running"
    await ctx.task
    assert st.run.status == "done" and no_gates == ["extract"]
    assert sorted(calls) == [1, 2, 2, 3] and calls.count(1) == 1     # page 1 never asked again
    after = {i.id: i.statement for i in st.data_items()}
    assert len(after) == 14 and all(after[k] == v for k, v in before.items())
    assert len(set(after.values())) == len(after)                    # no item twice
    assert st.sources[0].status == "extracted"
    readers = [a for a in st.agents.values() if a.role == "extractor"]
    assert sorted(a.status for a in readers) == ["done", "done", "done", "stopped", "stopped"]


async def test_resume_in_draft_keeps_the_data_and_skips_a_finished_draft(short, monkeypatch, no_gates,
                                                                         fixture_report):
    ctx = make_ctx(short, stage="draft")
    ran: list[str] = []
    score = next(i for i in fixture_report.items if i.kind == "extrapolation")

    async def extract(c):
        ran.append("extract")

    async def draft(c):
        ran.append("draft")
        if ran.count("draft") == 1:
            stop(c)                                                   # STOP while the drafters work
            c.check_stop()
        c.state.items[score.id] = score.model_copy(deep=True)

    async def check(c):
        ran.append("verify")
        if ran.count("verify") == 1:
            stop(c)                                                   # the draft is in, STOP cuts verify
            c.check_stop()

    stages(monkeypatch, [("extract", extract), ("draft", draft), ("verify", check), ("complete", noop)])
    await start(ctx)
    assert ctx.state.run.status == "stopped" and resume.cut_stage(ctx.state) == "draft"
    await resume.resume_run(ctx)
    await ctx.task
    assert ran == ["extract", "draft", "draft", "verify"] and ctx.state.run.status == "stopped"
    assert resume.cut_stage(ctx.state) == "verify"
    await resume.resume_run(ctx)
    await ctx.task
    assert ran == ["extract", "draft", "draft", "verify", "verify"] and ctx.state.run.status == "done"
    assert orchestrator.stage_done(ctx.state, "draft")


async def test_resume_in_verify_checks_only_pending_items(short, monkeypatch, no_gates, fixture_report):
    """verify.pending is what the real verify stage checks: items checked before STOP are not judged again."""
    ctx = make_ctx(short, stage="verify")
    claims = [i.model_copy(deep=True) for i in fixture_report.items if i.kind == "conclusion"][:3]
    for c in claims:
        c.verification = m.Verification(status="pending")
        ctx.state.items[c.id] = c
    judged: list[str] = []

    async def check(c):
        for iid in verify.pending(c.state, ("conclusion",)):
            c.check_stop()
            judged.append(iid)
            item = c.state.items[iid]
            item.verification = m.Verification(status="pass", verified_at=now_iso())
            if len(judged) == 2 and judged.count(iid) == 1 and len(set(judged)) == 2:
                stop(c)

    stages(monkeypatch, [("verify", check), ("complete", noop)])
    await start(ctx)
    assert ctx.state.run.status == "stopped" and len(judged) == 2
    await resume.resume_run(ctx)
    await ctx.task
    assert ctx.state.run.status == "done" and sorted(judged) == sorted(c.id for c in claims)   # each once


# ---------------------------------------------------------------------------
# Policy, events, Bobby and agents stopped one by one
# ---------------------------------------------------------------------------


async def test_kill_switch_off_with_a_bump_and_the_contract_order(short, monkeypatch, no_gates):
    ctx = make_ctx(short)
    calls = counting_extractor([ctx], {1: 1})
    stages(monkeypatch, [("extract", "extract"), ("complete", noop)])
    await start(ctx)
    st = ctx.state
    stopped_version = st.policy.version
    assert st.policy.kill_switch and st.agents["bobby"].status == "stopped"
    seq = st.run.last_seq
    await resume.resume_run(ctx, "carry on")
    first = [e for e in st.events if e.seq > seq][:4]
    assert [e.type for e in first] == ["policy.updated", "run.resumed", "run.status", "agent.status"]
    assert first[0].data.version == stopped_version + 1 and first[0].data.change == resume.CHANGE
    assert (first[1].data.reason, first[1].data.stage) == ("carry on", "extract")
    assert (first[2].data.status, first[2].data.stage) == ("running", "extract")
    assert (first[3].agent_id, first[3].data.status) == ("bobby", "working")   # Bobby comes back
    assert not st.policy.kill_switch and not st.kill_switch and st.run.policy_version == stopped_version + 1
    assert st.run.stop_reason is None
    await ctx.task
    assert st.run.status == "done" and st.agents["bobby"].status == "done" and calls.count(1) == 2


async def test_an_agent_stopped_one_by_one_stays_stopped_and_is_a_hole(short, monkeypatch, no_gates):
    ctx = make_ctx(short)
    box = [ctx]
    default = bedrock._FAKES[ex.TOOL]
    calls: list[int] = []

    async def fn(system, user, schema):
        page = int(PAGE.search(user).group(1))
        calls.append(page)
        if page == 1 and calls.count(1) == 1:
            third = next(a for a in box[0].state.agents.values() if a.task.endswith("page 3"))
            await agentstop.stop_agent(box[0], third.agent_id, "not needed")   # the analyst stops page 3
            stop(box[0])                                                        # then the whole run
        return default(system, user, schema)

    bedrock.register_fake(ex.TOOL, fn)
    stages(monkeypatch, [("extract", "extract"), ("complete", noop)])
    await start(ctx)
    st = ctx.state
    third = next(a for a in st.agents.values() if a.task.endswith("page 3"))
    rule = f"R-stop-{third.agent_id}"
    await resume.resume_run(ctx)
    assert any(r.rule_id == rule and r.origin == "user_agent_stop" for r in st.policy.rules)   # kept
    await ctx.task
    assert st.run.status == "done" and sorted(calls) == [1, 1, 2]       # page 3 never read
    assert st.agents[third.agent_id].status == "stopped"
    assert not any(a.task.endswith("page 3") and a.agent_id != third.agent_id for a in st.agents.values())
    holes = finalize.build_holes(ctx, [])
    assert any(third.agent_id in h.reason and "stopped by the analyst" in h.reason for h in holes)


async def test_bobby_stopped_by_the_analyst_stays_stopped(short, monkeypatch, no_gates):
    ctx = make_ctx(short)
    stages(monkeypatch, [("extract", noop), ("draft", noop), ("complete", noop)])
    gates.spawn_bobby(ctx)
    await agentstop.stop_agent(ctx, "bobby", "I decide")
    stop(ctx)
    await resume.resume_run(ctx)
    assert ctx.state.agents["bobby"].status == "stopped" and gates.bobby_off(ctx)
    assert any(r.rule_id == "R-stop-bobby" for r in ctx.state.policy.rules)
    await ctx.task
    assert ctx.state.run.status == "done" and ctx.state.agents["bobby"].status == "stopped"


async def test_stop_resume_stop_resume(short, monkeypatch, no_gates):
    ctx = make_ctx(short)
    calls = counting_extractor([ctx], {1: 1, 2: 1})
    stages(monkeypatch, [("extract", "extract"), ("complete", noop)])
    await start(ctx)
    v1 = ctx.state.policy.version
    await resume.resume_run(ctx)
    await ctx.task
    assert ctx.state.run.status == "stopped" and ctx.state.policy.kill_switch
    await resume.resume_run(ctx)
    await ctx.task
    st = ctx.state
    assert st.run.status == "done" and st.policy.version > v1 + 2 and not st.policy.kill_switch
    assert sorted(calls) == [1, 1, 2, 2, 3] and len(st.data_items()) == 14
    assert [e.type for e in st.events].count("run.resumed") == 2


async def test_resume_waits_for_the_stopped_task_to_end(short, monkeypatch, no_gates):
    """A model call still in flight at resume is cancelled first, so its result can't land in the resumed run."""
    ctx = make_ctx(short)
    gate = asyncio.Event()

    async def slow(c):
        stop(c)
        await gate.wait()                                             # still "in flight"

    stages(monkeypatch, [("extract", slow), ("complete", noop)])
    ctx.task = asyncio.create_task(orchestrator.run_pipeline(ctx))
    await until(lambda: ctx.state.run.status == "stopped")
    old = ctx.task
    stages(monkeypatch, [("extract", noop), ("complete", noop)])
    await resume.resume_run(ctx)
    assert old.done() and ctx.task is not old
    await ctx.task
    assert ctx.state.run.status == "done"


async def test_resume_at_approve_waits_for_the_analyst_again(short, monkeypatch, no_gates):
    ctx = make_ctx(short, stage="approve")
    ctx.state.approval_request = None
    ctx.state.policy = None
    stages(monkeypatch, [("approve", "approve"), ("complete", noop)])
    ctx.task = asyncio.create_task(orchestrator.run_pipeline(ctx))
    await until(lambda: ctx.state.run.status == "awaiting_approval")
    stop(ctx)
    await ctx.task
    assert not orchestrator.stage_done(ctx.state, "approve")         # the kill-switch policy is no approval
    await resume.resume_run(ctx)
    await until(lambda: ctx.state.run.status == "awaiting_approval")
    assert not ctx.task.done()
    ctx.state.approval_request = m.ApprovalRequest(decisions=[])
    ctx.approval.set()
    await ctx.task
    assert ctx.state.run.status == "done"


# ---------------------------------------------------------------------------
# The route: 409 like the mock for anything but stopped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "done", "failed", "awaiting_approval"])
async def test_resume_refuses_any_status_but_stopped(short, status):
    ctx = make_ctx(short)
    ctx.state.run.status = status
    with pytest.raises(InvalidState) as e:
        await resume.resume_run(ctx)
    assert str(e.value) == f"Only a stopped run can resume. This one is {status}."


def test_route_answers_409_like_the_mock_and_resumes_a_stopped_run(short, monkeypatch):
    stages(monkeypatch, [("extract", noop), ("complete", noop)])
    monkeypatch.setattr(gates, "after_stage", noop2)
    store = Store(short)
    states = {}
    for status in ("running", "done", "failed"):
        st = store.create_run(m.CreateRunRequest(company="Meridian Diagnostics AG", ticker="MDX"))
        st.run.status = status
        states[status] = st
    stopped = store.create_run(m.CreateRunRequest(company="Meridian Diagnostics AG", ticker="MDX"))
    stopped.run.stage = "extract"
    with TestClient(create_app(short, store=store)) as client:
        for status, st in states.items():
            got = client.post(f"{P}/runs/{st.run.run_id}/resume", json={})
            assert got.status_code == 409 and got.json()["error"]["code"] == "invalid_state"
            assert got.json()["error"]["message"] == f"Only a stopped run can resume. This one is {status}."
        r = f"{P}/runs/{stopped.run.run_id}"
        assert client.post(f"{r}/stop", json={"reason": "pause"}).status_code == 200
        got = client.post(f"{r}/resume", json={"reason": "carry on"})
        assert got.status_code == 200 and m.Run.model_validate(got.json()).status == "running"
        types = [e.type for e in stopped.events]
        i = types.index("run.resumed")
        assert types[i - 1:i + 2] == ["policy.updated", "run.resumed", "run.status"]


async def noop2(ctx, stage) -> None:
    return None
