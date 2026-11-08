"""V8-fork: rerun from a step (CONTRACT.md 13). Offline (FAKE_MODELS=1).

The parent is a whole fake-model run of a fictional company (simulated): the plan, approval and collect stages
are stand-ins that put the planted pages (tests/data/planted/, read only) in place, then the real extract,
draft, verify, repair, summarize, verify_summary and complete stages run with the agent modules' own fakes.
Bobby's gates are switched off here (they belong to V3 and are tested there). Forks run through the real
fork_run and orchestrator.start_from.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.axelrod import bedrock, fork, gates, orchestrator
from backend.axelrod.agents import drafter, extractor, judge, repairer, summarizer
from backend.axelrod.agents.base import AgentRunner, register_fake, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.contract import m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import InvalidState, RunContext
from backend.axelrod.stages import approve
from backend.axelrod.store import Store
from backend.axelrod.store.vault import data_root

PLANTED_DIR = Path(__file__).parent / "data" / "planted"
PLANTED = [(PLANTED_DIR / f"annual_report_p{i}.txt").read_text(encoding="utf-8") for i in (1, 2, 3)]
COMPANY = "Meridian Diagnostics AG"
SAY = "Prefer the consolidated statements and say so (simulated analyst instructions)."
BLOCK = "Instructions (from the analyst):"

NEEDS = [
    m.DataNeed(need_id="N-01", label="Returns on tangible capital", dimension="financials",
               question="EBIT, taxes, total assets, cash, goodwill and current liabilities by year?",
               data_types=["financial_filing"], criteria=["rotic_level", "rotic_vs_peers"], rationale="test"),
    m.DataNeed(need_id="N-05", label="Debt", dimension="financials", question="Financial debt and net debt?",
               data_types=["financial_filing"], criteria=["leverage", "liquidity"], rationale="test"),
]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    saved = dict(bedrock._FAKES)
    for mod in (drafter, judge, repairer):
        mod.register_fakes()

    async def no_gate(ctx, stage):          # Bobby's gates are V3's: off for these tests
        return None

    monkeypatch.setattr(gates, "after_stage", no_gate)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


def recording(tool: str) -> list[str]:
    """Wrap the registered fake for a tool so every user prompt it gets is kept."""
    real = bedrock._FAKES[tool]
    seen: list[str] = []

    def fn(system, user, schema):
        seen.append(user)
        return real(system, user, schema)

    register_fake(tool, fn)
    return seen


# ---------------------------------------------------------------------------
# The parent: a whole fake-model run (simulated)
# ---------------------------------------------------------------------------


def plan_source() -> m.Source:
    return m.Source(source_id="S-901", title="Meridian annual report FY2025 (simulated)",
                    url="https://example.com/meridian-ar-2025.pdf", domain="example.com", kind="annual_report",
                    data_types=["financial_filing"], access=m.SourceAccess(), tier="priority", rank=1,
                    scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test data (simulated)",
                    need_ids=["N-01", "N-05"], criteria=["rotic_level", "leverage", "liquidity"],
                    vault_folder="01-Annual-Reports", proposed_by="planner", approval="pending", status="proposed")


async def sim_plan(ctx: RunContext) -> None:
    """Stand-in for the frozen plan stage (simulated)."""
    st = ctx.state
    ctx.vault.put_text(ctx.run_id, "00-Meta/_plan.json", json.dumps({"simulated": True}))
    for need in NEEDS:
        st.needs.append(need)
        ctx.emit("plan.need", {"need": need}, message=f"Need {need.need_id}")
    src = plan_source()
    st.sources.append(src)
    ctx.emit("plan.source", {"source": src}, message="Source S-901")
    ctx.emit("plan.ready", {"needs": len(NEEDS), "sources": 1}, message="Plan ready")


async def sim_approve(ctx: RunContext) -> None:
    """The analyst approves S-901 and moves it to rank 1 of tier_2 (simulated)."""
    ctx.emit("run.status", {"status": "awaiting_approval", "stage": "approve"}, message="Waiting")
    approve.commit(ctx, m.ApprovalRequest(decisions=[m.SourceDecision(source_id="S-901", approval="approved")]))


async def sim_collect(ctx: RunContext) -> None:
    """Stand-in fetcher: the planted pages stored as DOC-001 (simulated, no network)."""
    st = ctx.state
    with AgentRunner(ctx, "fetcher", "Fetch S-901 (simulated)", ["S-901"], None):
        raw = ctx.vault.put_bytes(ctx.run_id, "01-Annual-Reports/meridian-ar-2025.pdf", b"%PDF simulated")
        text = ctx.vault.put_text(ctx.run_id, "01-Annual-Reports/meridian-ar-2025.md", "\n\f".join(PLANTED))
        meta = ctx.vault.put_text(ctx.run_id, "01-Annual-Reports/meridian-ar-2025.pdf.meta.json", "{}")
        doc = m.Document(doc_id=ctx.next_id("DOC"), source_id="S-901", title="Meridian AR FY2025 (simulated)",
                         url=st.sources[0].url, content_type="pdf", paginated=True, pages=3, sha256="0" * 64,
                         bytes=14, fetched_at=now_iso(), vault_paths=m.DocVaultPaths(raw=raw, text=text, meta=meta),
                         scan=m.ScanResult(scanner="none", verdict="skipped"))
        st.documents.append(doc)
        st.pages[doc.doc_id] = list(PLANTED)
        src = st.sources[0]
        src.doc_ids, src.status = [doc.doc_id], "fetched"
        src.stats = m.SourceStats(bytes=14, pages=3)
        ctx.emit("doc.stored", {"doc_id": doc.doc_id, "source_id": "S-901", "pages": 3, "vault_path": raw,
                                "scan": doc.scan}, message="DOC-001 stored")
        ctx.emit("source.status", {"source_id": "S-901", "status": "fetched", "stats": src.stats}, message="fetched")


PIPELINE = [("plan", sim_plan), ("approve", sim_approve), ("collect", sim_collect), ("extract", "extract"),
            ("draft", "draft"), ("verify", "verify"), ("repair", "repair"), ("summarize", "summarize"),
            ("verify_summary", "verify_summary"), ("complete", "finalize")]


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(orchestrator, "STAGES", list(PIPELINE))


@pytest.fixture
async def parent(settings, pipeline) -> RunContext:
    store = Store(settings)
    ctx = await orchestrator.start_run(store, store.vault, settings, m.CreateRunRequest(company=COMPANY, ticker="MDX"))
    await asyncio.wait_for(ctx.task, 60)
    assert ctx.state.run.status == "done", ctx.state.run.error
    return ctx


TOOLS = {"extract": extractor.TOOL, "draft": tool_name(drafter.DraftOutput), "judge": tool_name(judge.JudgeVerdict),
         "brief": tool_name(summarizer.BriefDraft)}


async def forked(parent: RunContext, stage: str, text: str = SAY) -> tuple[RunContext, m.Run, dict, dict]:
    """Fork the parent from `stage` and run the fork to its end. Returns the fork's context, its Run as created,
    its state as created (items, agents, ids) and the prompts each tool saw while the fork ran."""
    before = parent.state.model_dump_json()
    files_before = parent.vault.list_files(parent.run_id)
    prompts = {name: recording(tool) for name, tool in TOOLS.items()}
    child = await fork.fork_run(parent.store, parent.vault, parent.settings, parent.state,
                                m.RerunRequest(from_stage=stage, instructions=text))
    created = child.state.run.model_copy(deep=True)
    start = {"items": {i: it.model_copy(deep=True) for i, it in child.state.items.items()},
             "agents": list(child.state.agents), "ids": dict(child.state.ids.counters),
             "sources": [s.model_copy(deep=True) for s in child.state.sources],
             "summary": child.state.summary.model_copy(deep=True) if child.state.summary else None,
             "policy": child.state.policy.model_copy(deep=True) if child.state.policy else None}
    await asyncio.wait_for(child.task, 60)
    assert child.state.run.status == "done", child.state.run.error
    assert parent.state.model_dump_json() == before                 # the parent never changes
    assert parent.vault.list_files(parent.run_id) == files_before
    return child, created, start, prompts


def check_fork_basics(parent: RunContext, child: RunContext, created: m.Run, stage: str) -> None:
    run = child.state.run
    assert run.run_id != parent.run_id and child.state.run.mode == "live"
    assert (run.parent_run_id, run.rerun_from, run.instructions) == (parent.run_id, stage, SAY)
    assert (created.status, created.stage) == ("running", stage)
    assert f"Rerun of {parent.run_id} from {stage}" in run.notes
    evs = child.state.events
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))      # its own log, from seq 1
    assert {e.run_id for e in evs} == {run.run_id}
    first = evs[0]
    assert first.type == "run.created" and first.data.run.parent_run_id == parent.run_id
    assert (first.data.run.rerun_from, first.data.run.instructions) == (stage, SAY)
    # Its own policy: the parent's rules as version 1, kill switch off.
    policy = child.state.policy
    assert policy is not None and policy is not parent.state.policy and policy.run_id == run.run_id
    assert [r.rule_id for r in policy.rules] == [r.rule_id for r in parent.state.policy.rules]
    assert not policy.kill_switch
    first_policy = next(e for e in evs if e.type == "policy.updated")
    assert first_policy.data.version == 1 and "Carried over from" in first_policy.data.change
    # Bobby is the fork's own and comes first. Its usage is its own.
    assert next(iter(child.state.agents)) == "bobby" and child.state.agents["bobby"] is not parent.state.agents["bobby"]
    calls = [e for e in evs if e.type == "gateway.request" and e.data.action == "model.invoke"]
    assert run.usage.model_calls == len(calls)


def instructed(prompts: list[str]) -> bool:
    return bool(prompts) and all(BLOCK in p and SAY in p for p in prompts)


# ---------------------------------------------------------------------------
# Forks from extract, draft and summarize
# ---------------------------------------------------------------------------


async def test_fork_from_extract_keeps_the_documents_and_redoes_extraction(parent):
    child, created, start, prompts = await forked(parent, "extract")
    check_fork_basics(parent, child, created, "extract")
    st, pst = child.state, parent.state
    assert start["items"] == {}
    assert [d.doc_id for d in st.documents] == [d.doc_id for d in pst.documents] and st.pages == pst.pages
    assert [s.status for s in start["sources"]] == ["fetched"]       # fetched, not extracted yet
    assert st.documents[0].vault_paths.text.startswith(f"runs/{child.run_id}/")
    rel = "01-Annual-Reports/meridian-ar-2025.md"
    assert child.vault.get_text(child.run_id, rel) == parent.vault.get_text(parent.run_id, rel)
    assert child.vault.get_text(child.run_id, "00-Meta/_plan.json")
    # The fetcher is copied with its name. The extractors are the fork's own, with the instructions.
    assert start["agents"] == ["fetcher-01"] and st.agents["fetcher-01"].name == pst.agents["fetcher-01"].name
    extractors = [a for a in st.agents.values() if a.role == "extractor"]
    assert len(extractors) == 3 and all(a.instructions == SAY for a in extractors)
    assert len(prompts["extract"]) == 3 and instructed(prompts["extract"])
    assert instructed(prompts["draft"]) and instructed(prompts["brief"])       # every agent from extract on
    assert prompts["judge"] and not any(SAY in p for p in prompts["judge"])   # never the judges
    assert all(a.instructions is None for a in st.agents.values() if a.role == "verifier")
    # Extraction is redone: ids count from D-001 again, the same planted facts come back.
    assert sorted(i.id for i in st.data_items()) == [f"D-{n:03d}" for n in range(1, 15)]
    assert sorted(i.statement for i in st.data_items()) == sorted(i.statement for i in pst.data_items())


async def test_fork_from_draft_keeps_the_data_items_and_their_ids(parent):
    child, created, start, prompts = await forked(parent, "draft")
    check_fork_basics(parent, child, created, "draft")
    st, pst = child.state, parent.state
    kept = {i: it for i, it in pst.items.items() if it.kind == "data"}
    assert start["items"] == kept                                     # the verified data items, ids and all
    assert {i: st.items[i] for i in kept} == kept                    # and the fork's checks leave them alone
    assert start["ids"]["D"] == 14 and "C" not in start["ids"] and "REQ" not in start["ids"]
    assert prompts["extract"] == []                                   # extraction is not redone
    assert instructed(prompts["draft"]) and instructed(prompts["brief"])
    assert not any(SAY in p for p in prompts["judge"])
    assert start["agents"] == ["fetcher-01", "extractor-01", "extractor-02", "extractor-03"]
    for aid in start["agents"]:
        assert st.agents[aid].name == pst.agents[aid].name and st.agents[aid].instructions is None
    drafters = [a for a in st.agents.values() if a.role == "drafter"]
    assert drafters and all(a.instructions == SAY for a in drafters)
    assert drafters[0].name == f"Bobby's goon #{len(start['agents']) + 1}"
    assert sorted(c.id for c in st.conclusions())[0] == "C-001"      # conclusions are redone
    assert st.report_ready and st.summary is not None and st.summary.status == "final"


async def test_fork_from_summarize_keeps_the_final_report_and_redoes_the_brief(parent):
    child, created, start, prompts = await forked(parent, "summarize")
    check_fork_basics(parent, child, created, "summarize")
    st, pst = child.state, parent.state
    assert start["items"] == pst.items                                # the verified agent report, as it is
    assert st.items == pst.items and st.rollup == pst.rollup and st.holes == pst.holes
    assert st.report_certainty == pst.report_certainty
    assert prompts["extract"] == prompts["draft"] == prompts["judge"] == []
    assert instructed(prompts["brief"])
    summarizers = [a for a in st.agents.values() if a.role == "summarizer"]
    assert len(summarizers) == 1 and summarizers[0].instructions == SAY
    assert st.summary is not None and st.summary.status == "final" and st.summary.run_id == child.run_id
    assert st.report_markdown_path.startswith(f"runs/{child.run_id}/")
    assert not any(e.type == "report.ready" for e in st.events)       # the report was final already


@pytest.mark.parametrize("stage", [s for s, _ in PIPELINE if s != "complete"])
async def test_a_fork_from_any_step_runs_to_the_end_and_never_touches_the_parent(parent, stage):
    child, created, start, prompts = await forked(parent, stage)
    run = child.state.run
    assert (run.parent_run_id, run.rerun_from, created.stage) == (parent.run_id, stage, stage)
    assert child.state.events[0].seq == 1 and child.state.events[0].type == "run.created"
    assert (start["policy"] is not None) == (stage not in ("plan", "approve"))
    if stage == "collect":                                            # approved, nothing fetched yet
        assert [(s.approval, s.status, s.doc_ids) for s in start["sources"]] == [("approved", "queued", [])]
        assert [r.rule_id for r in start["policy"].rules] == [r.rule_id for r in parent.state.policy.rules]
    if stage == "verify_summary":                                     # the brief, checked again
        sentences = [s for sec in start["summary"].sections for b in sec.blocks for s in getattr(b, "sentences", [])]
        assert sentences and all(s.verification.status == "pending" for s in sentences)
        assert prompts["brief"] == []
    if stage == "plan":
        assert start["items"] == {} and start["sources"] == [] and start["ids"] == {}


async def test_fork_from_verify_and_repair_rebuild_the_draft_from_the_parents_events(parent):
    pst = parent.state
    drafted = {e.data.item.id: e.data.item for e in pst.events if e.type == "item.created"
               and e.data.item.kind != "data"}
    child = await fork.fork_run(parent.store, parent.vault, parent.settings, pst,
                                m.RerunRequest(from_stage="verify", instructions=SAY))
    draft = {i: it for i, it in child.state.items.items() if it.kind != "data"}
    assert set(draft) == set(drafted) and all(it.verification.status == "pending" for it in draft.values())
    assert all(draft[i].model_dump(exclude={"verification", "certainty"}) ==
               drafted[i].model_dump(exclude={"verification", "certainty"}) for i in draft)
    await asyncio.wait_for(child.task, 60)
    assert child.state.run.status == "done"

    cut = fork.cut_seq(pst, "repair")
    checked = {e.data.item_id: e.data.verification.status for e in pst.events
               if e.type == "item.verified" and e.seq < cut and e.data.item_id in drafted}
    child = await fork.fork_run(parent.store, parent.vault, parent.settings, pst,
                                m.RerunRequest(from_stage="repair", instructions=SAY))
    assert {i: child.state.items[i].verification.status for i in checked} == checked
    await asyncio.wait_for(child.task, 60)
    assert child.state.run.status == "done"


async def test_fork_from_approve_waits_for_the_analyst_with_the_proposed_sources(parent, monkeypatch):
    monkeypatch.setattr(orchestrator, "STAGES", [(s, "approve" if s == "approve" else mod) for s, mod in PIPELINE])
    child = await fork.fork_run(parent.store, parent.vault, parent.settings, parent.state,
                                m.RerunRequest(from_stage="approve", instructions=SAY))
    st = child.state
    assert st.policy is None and st.items == {} and st.documents == [] and st.plan_ready
    assert [(s.source_id, s.approval, s.status) for s in st.sources] == [("S-901", "pending", "proposed")]
    for _ in range(100):
        if st.run.status == "awaiting_approval":
            break
        await asyncio.sleep(0.01)
    assert (st.run.status, st.run.stage) == ("awaiting_approval", "approve")   # the gate waits again
    approve.commit(child, m.ApprovalRequest(decisions=[m.SourceDecision(source_id="S-901", approval="approved")]))
    await asyncio.wait_for(child.task, 60)
    assert st.run.status == "done" and st.policy.run_id == child.run_id


# ---------------------------------------------------------------------------
# What the fork keeps of the parent's policy, and its ids
# ---------------------------------------------------------------------------


def test_stop_rules_survive_only_for_agents_the_fork_copies(settings, fixture_bundle):
    store = Store(settings)
    pst = store.create_run(m.CreateRunRequest(company=COMPANY))
    pst.run.status, pst.run.stage = "done", "complete"
    pst.sources = [plan_source().model_copy(update={"approval": "approved", "status": "extracted"})]
    for aid, role in (("fetcher-01", "fetcher"), ("extractor-01", "extractor"), ("drafter-01", "drafter")):
        pst.register_agent(m.Agent(agent_id=aid, role=role, status="done", task="t"))
    at = now_iso()
    stop = [m.PolicyRule(rule_id=f"R-stop-{a}", effect="deny", action="model.invoke", agent_id=a,
                         origin="user_agent_stop", created_at=at) for a in ("extractor-01", "drafter-01", "bobby")]
    pst.policy = m.Policy(run_id=pst.run.run_id, version=4, kill_switch=True, rules=stop, role_scopes=[],
                          updated_at=at, as_of_seq=0)
    st = fork.build_fork(pst, m.RerunRequest(from_stage="draft", instructions=SAY), run_id="run_20261108_fork01",
                         settings=settings)
    assert list(st.agents) == ["fetcher-01", "extractor-01"]         # by role: the fetcher and extractor ran before draft
    assert [r.rule_id for r in st.policy.rules] == ["R-stop-extractor-01"]   # its gap stays declared
    assert (st.policy.version, st.policy.kill_switch, st.policy.run_id) == (1, False, "run_20261108_fork01")
    assert st.ids.counters == {"S": 901, "fetcher": 1, "extractor": 1}
    st = fork.build_fork(pst, m.RerunRequest(from_stage="extract", instructions=SAY), run_id="run_20261108_fork02",
                         settings=settings)
    assert list(st.agents) == ["fetcher-01"] and st.policy.rules == []   # the fork's own extractor-01 is free


# ---------------------------------------------------------------------------
# Errors, exactly as the mock
# ---------------------------------------------------------------------------


def stopped_at_extract(store: Store) -> RunContext:
    state = store.create_run(m.CreateRunRequest(company=COMPANY))
    ctx = RunContext.create(state, store, store.vault, store.settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    ctx.emit("run.status", {"status": "running", "stage": "extract"}, stage="extract", message="Stage extract")
    state.kill_switch = True
    ctx.emit("run.status", {"status": "stopped", "stage": "extract"}, stage="extract", message="Kill switch on")
    ctx.emit("run.stopped", {"reason": "test"}, stage="extract", message="Run stopped")
    return ctx


async def test_fork_refuses_what_the_mock_refuses(settings):
    store = Store(settings)
    ctx = stopped_at_extract(store)
    req = lambda stage: m.RerunRequest(from_stage=stage, instructions=SAY)   # noqa: E731
    with pytest.raises(InvalidState, match="Nothing to rerun after complete. Pick an earlier step."):
        await fork.fork_run(store, store.vault, settings, ctx.state, req("complete"))
    with pytest.raises(InvalidState, match=f"{ctx.run_id} never reached draft"):
        await fork.fork_run(store, store.vault, settings, ctx.state, req("draft"))
    running = store.create_run(m.CreateRunRequest(company=COMPANY))
    with pytest.raises(InvalidState, match="Rerun forks a finished, stopped or failed run. This one is running."):
        await fork.fork_run(store, store.vault, settings, running, req("plan"))
    assert len(store.list_runs()) == 2                                # nothing was created


def test_the_rerun_route_answers_201_or_409_like_the_mock(settings, monkeypatch):
    async def noop(ctx):
        return None

    monkeypatch.setattr(orchestrator, "STAGES", [(s, noop) for s, _ in PIPELINE])
    store = Store(settings)
    parent = stopped_at_extract(store)
    running = store.create_run(m.CreateRunRequest(company=COMPANY))
    p = f"/api/v1/runs/{parent.run_id}/rerun"

    def err(r, status: int) -> str:
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == "invalid_state"
        return r.json()["error"]["message"]

    with TestClient(create_app(settings, store=store)) as client:
        assert err(client.post(p, json={"from_stage": "complete", "instructions": "x"}), 409) == \
            "Nothing to rerun after complete. Pick an earlier step."
        assert err(client.post(p, json={"from_stage": "verify", "instructions": "x"}), 409) == \
            f"{parent.run_id} never reached verify"
        assert err(client.post(f"/api/v1/runs/{running.run.run_id}/rerun",
                               json={"from_stage": "plan", "instructions": "x"}), 409) == \
            "Rerun forks a finished, stopped or failed run. This one is running."
        r = client.post(p, json={"from_stage": "extract", "instructions": SAY})
        assert r.status_code == 201, r.text
        run = m.Run.model_validate(r.json())
        assert (run.parent_run_id, run.rerun_from, run.instructions, run.status) == \
            (parent.run_id, "extract", SAY, "running")
        evs = client.get(f"/api/v1/runs/{run.run_id}/events").json()["events"]
        assert evs[0]["seq"] == 1 and evs[0]["type"] == "run.created"
        assert evs[0]["data"]["run"]["parent_run_id"] == parent.run_id
        assert client.get(f"/api/v1/runs/{parent.run_id}").json()["status"] == "stopped"


# ---------------------------------------------------------------------------
# start_from: run from a stage, skip what is done
# ---------------------------------------------------------------------------


async def test_start_from_runs_from_a_stage_and_skips_finished_work(settings, monkeypatch):
    ran: list[str] = []

    def stage(name: str):
        async def run(ctx):
            ran.append(name)
        return run

    monkeypatch.setattr(orchestrator, "STAGES", [(s, stage(s)) for s, _ in PIPELINE])
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=COMPANY))
    ctx = RunContext.create(state, store, store.vault, settings)
    state.report_ready = True                                         # repair's result is already in
    await orchestrator.start_from(ctx, "verify")
    assert ran == ["verify", "summarize", "verify_summary", "complete"]
    assert state.run.status == "done" and next(iter(state.agents)) == "bobby"
    with pytest.raises(ValueError):
        await orchestrator.start_from(ctx, "nowhere")
    assert orchestrator.stage_done(state, "repair") and not orchestrator.stage_done(state, "extract")


def test_a_fork_vault_copy_reports_what_it_could_not_find(settings):
    store = Store(settings)
    a = store.create_run(m.CreateRunRequest(company=COMPANY))
    b = store.create_run(m.CreateRunRequest(company=COMPANY))
    store.vault.put_text(a.run.run_id, "00-Meta/_plan.json", "{}")
    copied, missing = store.copy_vault_files(a.run.run_id, b.run.run_id, ["00-Meta/_plan.json", "00-Meta/none.json",
                                                                          "../escape.json"])
    assert copied == ["00-Meta/_plan.json"] and missing == ["00-Meta/none.json", "../escape.json"]
    assert (data_root(settings) / store.vault.root(b.run.run_id) / "00-Meta/_plan.json").is_file()
