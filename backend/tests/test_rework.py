"""V3-rework: rerun one goon in the same run, with instructions (CONTRACT.md 14). Offline (FAKE_MODELS=1).

The run is the mock fixture's finished report and brief (a fictional company, Northwind Instruments), loaded
into a real Store, Vault, Gateway and RunContext, with the goons the real pipeline would have registered
(extractors by page, one drafter per criteria group, the summarizer, a judge and a repairer). Model answers
come from fakes (simulated). Every check is the real code checker.
"""
from __future__ import annotations

import asyncio
import copy
import re
import time

import pytest
from fastapi.testclient import TestClient

from backend.axelrod import gates, rework
from backend.axelrod.agents import drafter, extractor, judge, repairer, summarizer
from backend.axelrod.agents.base import AgentRunner, register_fake, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.contract import m
from backend.axelrod.runtime import InvalidState, RunContext
from backend.axelrod.stages import draft, extract, finalize, repair, summarize
from backend.axelrod.store import Store

P = "/api/v1"
FIXTURE = m.CreateRunRequest(company="Northwind Instruments (fixture)", ticker="NWI-X", created_via="api")
PAGE_OF = {"extractor-01": ("S-002", 1), "extractor-02": ("S-002", 2), "extractor-03": ("S-002", 3),
           "extractor-04": ("S-005", 1), "extractor-05": ("S-006", 1)}


# ---------------------------------------------------------------------------
# A finished fixture run with the goons the pipeline registers
# ---------------------------------------------------------------------------


def _defaults() -> None:
    drafter.register_fakes()
    judge.register_fakes()
    repairer.register_fakes()
    register_fake(extractor.TOOL, extractor.fake_extract)
    register_fake(tool_name(summarizer.FaithfulnessVerdict), summarizer.fake_judge)
    register_fake(summarize.SentenceRevision.TOOL_NAME, summarize.fake_revise_sentence)


@pytest.fixture(autouse=True)
def _fakes():
    _defaults()
    yield
    _defaults()


def _agent(state, agent_id: str, role: str, task: str, targets: list[str], model: str | None) -> None:
    state.register_agent(m.Agent(agent_id=agent_id, role=role, model_id=model, status="done", task=task,
                                 target_ids=targets, started_at="2026-11-08T03:00:00.000Z",
                                 finished_at="2026-11-08T03:00:05.000Z"))


def build_state(store: Store, settings, bundle: dict, report: m.AgentReport):
    state = store.create_run(FIXTURE)
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    gates.spawn_bobby(ctx)
    state.needs = [m.DataNeed.model_validate(n) for n in bundle["needs"]]
    state.sources = []
    for raw in bundle["sources"]:
        s = m.Source.model_validate(raw)
        s.approval, s.status = ("approved" if s.tier == "priority" else "auto_approved"), "extracted"
        state.sources.append(s)
    state.documents = [m.Document.model_validate(d) for d in copy.deepcopy(bundle["docs"])]
    for d in state.documents:
        state.pages[d.doc_id] = [bundle["page_text"](d.doc_id, p) for p in range(1, d.pages + 1)]
    state.as_of = "2026-11-08"
    small, large = settings.model_small, settings.model_large
    for aid, (sid, page) in PAGE_OF.items():
        _agent(state, aid, "extractor", f"Extract {sid} page {page}", [sid], small)
    _agent(state, "extractor-06", "extractor", "Read S-001 structured facts by code (no model)", ["S-001"], None)
    groups = draft.criterion_groups(finalize.in_scope("screen"), state.needs)
    group_of = {c: f"drafter-{gi + 1:02d}" for gi, g in enumerate(groups) for c in g}
    for gi, g in enumerate(groups):
        _agent(state, f"drafter-{gi + 1:02d}", "drafter", f"Draft {', '.join(g)}", [], large)
    _agent(state, "fetcher-01", "fetcher", "Fetch the approved sources", [], None)
    for item in report.items:
        if item.kind == "data" and item.agent_id == "xbrl-reader":
            item.agent_id = "extractor-06"
        elif item.kind == "conclusion":
            item.agent_id = group_of[item.criteria[0]]
        elif item.kind == "extrapolation":
            item.agent_id = group_of[item.criterion]
        state.items[item.id] = item
    _agent(state, "verifier-C-005-1", "verifier", "Judge C-005 (vote 1)", ["C-005"], small)
    _agent(state, "repairer-01", "repairer", "Repair C-005 (attempt 1)", ["C-005"], large)
    _agent(state, "summarizer-01", "summarizer", summarize.SUMMARIZER_TASK, [], large)
    state.holes = list(report.holes)
    state.rollup = report.rollup
    state.report_certainty = report.certainty
    state.summary = m.HumanReport.model_validate(copy.deepcopy(bundle["summary"]))
    for iid in state.items:
        prefix, n = iid.split("-")
        state.ids.counters[prefix] = max(state.ids.counters.get(prefix, 0), int(n))
    state.ids.counters["H"] = 15
    state.ids.counters["HOLE"] = len(state.holes)
    ctx.emit("report.ready", finalize.report_counts(state, len(state.holes)), message="report final", stage="repair")
    ctx.emit("summary.ready", {"sentences": 15, "words": 300}, message="brief final", stage="verify_summary")
    gates.finish_bobby(ctx)
    ctx.emit("run.status", {"status": "done", "stage": "complete"}, stage="complete", message="Run done")
    ctx.emit("run.completed", {}, stage="complete", message="Run complete")
    return state, ctx


@pytest.fixture
def run(settings, fixture_bundle, fixture_report):
    store = Store(settings)
    _, ctx = build_state(store, settings, fixture_bundle, fixture_report)
    return ctx


def since(ctx: RunContext, mark: int, type_: str | None = None) -> list[m.Event]:
    return [e for e in ctx.state.events if e.seq > mark and (type_ is None or e.type == type_)]


def page_fake(state, agent_id: str, edit=lambda d: d):
    """A simulated extractor that reads its page back as the old items say, through `edit`."""
    old = [i for i in state.data_items() if i.agent_id == agent_id]

    def fake(system, user, schema):
        items = []
        for d in old:
            ev = next(c for c in d.citations if c.role == "evidence")
            unit = next((c.quote for c in d.citations if c.role == "unit_context"), "")
            v = d.value
            items.append(edit({"statement": d.statement, "metric": d.statement.split(" was ")[0],
                               "period": v.period if v else "", "value_as_reported": v.as_reported if v else "",
                               "unit": v.unit if v else "", "scale": v.scale if v else "one", "quote": ev.quote,
                               "unit_quote": unit, "period_quote": "", "need_ids": [d.need_id] if d.need_id else []}))
        return {"items": items}
    return fake


async def settle_task(task: asyncio.Task, timeout: float = 30) -> rework.Outcome:
    return await asyncio.wait_for(task, timeout)


# ---------------------------------------------------------------------------
# Extractor: the same page again, the same ids
# ---------------------------------------------------------------------------


async def test_extractor_rerun_keeps_ids_and_keeps_the_old_version_as_an_attempt(run):
    ctx, st = run, run.state
    prompts: list[str] = []
    fake = page_fake(st, "extractor-03")
    register_fake(extractor.TOOL, lambda s, u, sch: prompts.append(u) or fake(s, u, sch))
    old = {i.id: i.model_copy(deep=True) for i in st.data_items() if i.agent_id == "extractor-03"}
    mark = st.run.last_seq
    agent, task = rework.start_rerun(ctx, "extractor-03", "Quote the whole table row.", True)
    assert (agent.agent_id, agent.parent_agent_id, agent.rerun_by, agent.role) == (
        "extractor-03.2", "extractor-03", "analyst", "extractor")
    assert agent.instructions == "Quote the whole table row." and agent.name.startswith("Bobby's goon #")
    assert agent.task == "Rerun of extractor-03: Extract S-002 page 3" and agent.target_ids == ["S-002"]
    out = await settle_task(task)
    assert sorted(out.updated) == sorted(old) and not out.created
    assert prompts and "Instructions (from the analyst):\nQuote the whole table row." in prompts[0]
    evs = since(ctx, mark)
    types_ = [e.type for e in evs]
    assert types_[0] == "run.status" and evs[0].data.status == "running"            # the finished run reopened
    assert types_[-2:] == ["run.status", "run.completed"] and evs[-2].data.status == "done"
    for iid, before in old.items():
        now = st.items[iid]
        assert now.agent_id == "extractor-03.2" and now.verification.status != "pending" and now.certainty
        rw = [a for a in now.verification.attempts if a.action == "rework"]
        assert len(rw) == 1 and rw[0].status == before.verification.status and rw[0].before == before.statement
        assert rw[0].checks == before.verification.checks and "extractor-03.2" in rw[0].note
        upd = next(i for i, e in enumerate(evs) if e.type == "item.updated" and e.data.item.id == iid)
        ver = next(i for i, e in enumerate(evs) if e.type == "item.verified" and e.data.item_id == iid)
        assert evs[upd].data.reason == "rework" and upd < ver
    rechecked = {e.data.item_id for e in evs if e.type == "item.verified"} - set(old)
    assert "C-005" in out.rechecked and "C-005" in rechecked                          # C-005 cites D-028 and D-031
    assert st.agents["extractor-03.2"].status == "done" and st.run.status == "done"
    assert any(e.type == "report.ready" for e in evs)                               # the final report again


async def test_a_rerun_of_a_rerun_counts_on_from_the_root(run):
    ctx, st = run, run.state
    register_fake(extractor.TOOL, page_fake(st, "extractor-03"))
    await settle_task(rework.start_rerun(ctx, "extractor-03", "First pass.", False)[1])
    register_fake(extractor.TOOL, page_fake(st, "extractor-03.2"))
    agent, task = rework.start_rerun(ctx, "extractor-03.2", "Second pass.", False)
    assert agent.agent_id == "extractor-03.3" and agent.parent_agent_id == "extractor-03.2"
    assert agent.task == "Rerun of extractor-03.2: Extract S-002 page 3"
    out = await settle_task(task)
    for iid in out.updated:
        notes = [a.note for a in st.items[iid].verification.attempts if a.action == "rework"]
        assert len(notes) == 2 and "extractor-03.2" in notes[0] and "extractor-03.3" in notes[1]
        assert st.items[iid].agent_id == "extractor-03.3"
    assert [a.attempt for a in st.items[out.updated[0]].verification.attempts] == \
        list(range(1, len(st.items[out.updated[0]].verification.attempts) + 1))


async def test_without_recheck_only_the_agents_own_outputs_change(run):
    ctx, st = run, run.state
    register_fake(extractor.TOOL, page_fake(st, "extractor-03"))
    mark = st.run.last_seq
    out = await settle_task(rework.start_rerun(ctx, "extractor-03", "Again.", False)[1])
    evs = since(ctx, mark)
    assert not out.rechecked and "run.completed" not in [e.type for e in evs]
    touched = {e.data.item_id for e in evs if e.type == "item.verified"}
    assert touched == set(out.updated) and st.run.status == "done"


async def test_a_new_item_gets_a_fresh_id_and_old_items_without_a_new_version_are_kept(run):
    ctx, st = run, run.state
    base = page_fake(st, "extractor-03")

    def fake(s, u, sch):
        out = base(s, u, sch)
        out["items"] = out["items"][:2] + [{"statement": "Senior notes due 2030 total $750 million.",
                                            "metric": "Senior notes due 2030", "period": "FY2025",
                                            "value_as_reported": "750", "unit": "USD", "scale": "million",
                                            "quote": "Senior notes due 2030 750",
                                            "unit_quote": "Contractual obligations (In millions)"}]
        return out

    register_fake(extractor.TOOL, fake)
    old_ids = sorted(i.id for i in st.data_items() if i.agent_id == "extractor-03")
    out = await settle_task(rework.start_rerun(ctx, "extractor-03", "Add the revolver.", False)[1])
    assert len(out.updated) == 2 and len(out.kept) == len(old_ids) - 2 and set(out.kept) <= set(old_ids)
    assert out.created == ["D-038"] and st.items["D-038"].agent_id == "extractor-03.2"
    assert all(st.items[i].agent_id == "extractor-03" for i in out.kept)


# ---------------------------------------------------------------------------
# Drafter, judge, repairer, summarizer
# ---------------------------------------------------------------------------


async def test_drafter_rerun_keeps_score_ids_by_criterion_and_checks_the_new_versions(run):
    ctx, st = run, run.state
    prompts: list[str] = []
    register_fake(tool_name(drafter.DraftOutput), lambda s, u, sch: prompts.append(u) or drafter.fake_draft(s, u, sch))
    group = draft.group_of_task(st.agents["drafter-04"].task)
    assert group == ["leverage", "liquidity"]
    old_e = {e.criterion: e.id for e in st.extrapolations()}
    out = await settle_task(rework.start_rerun(ctx, "drafter-04", "Use the FY2025 free cash flow.", True)[1])
    assert prompts and "Instructions (from the analyst):" in prompts[0]
    assert old_e["leverage"] in out.updated and old_e["liquidity"] in out.updated
    for crit in group:
        e = st.items[old_e[crit]]
        assert e.agent_id == "drafter-04.2" and e.verification.status != "pending"
        assert any(a.action == "rework" for a in e.verification.attempts)
    assert all(st.items[i].verification.status != "pending" for i in out.changed)
    assert st.run.status == "done"


async def test_judge_rerun_asks_a_fresh_panel_that_never_sees_instructions(run):
    ctx, st = run, run.state
    prompts: list[str] = []
    register_fake(tool_name(judge.JudgeVerdict), lambda s, u, sch: prompts.append(u) or judge.fake_judge(s, u, sch))
    st.items["C-005"].verification.status = "pass"                  # code checks pass, so the judges run
    agent, task = rework.start_rerun(ctx, "verifier-C-005-1", "Look at the sign of D-031.", False)
    assert agent.agent_id == "verifier-C-005-1.2" and agent.model_id is None and agent.target_ids == ["C-005"]
    out = await settle_task(task)
    assert out.updated == ["C-005"]
    c = st.items["C-005"]
    support = next(r for r in c.verification.checks if r.check == "support")
    assert support.votes_total == 3 and len(prompts) == 3
    assert all("Instructions" not in p and "sign of D-031" not in p for p in prompts)
    new_judges = [a for a in st.agents if a.startswith("verifier-C-005-") and a[len("verifier-C-005-"):].isdigit()]
    assert sorted(new_judges) == ["verifier-C-005-1", "verifier-C-005-2", "verifier-C-005-3", "verifier-C-005-4"]
    assert c.verification.attempts[-1].action == "rework" and "Look at the sign" in c.verification.attempts[-1].note


async def test_repairer_rerun_weakens_its_item_and_the_checks_decide(run):
    ctx, st = run, run.state
    prompts: list[str] = []
    register_fake(tool_name(repairer.RepairOutput),
                  lambda s, u, sch: prompts.append(u) or repairer.fake_repair(s, u, sch))
    before = st.items["C-005"].model_copy(deep=True)
    out = await settle_task(rework.start_rerun(ctx, "repairer-01", "Drop the ratio.", False)[1])
    assert out.updated == ["C-005"] and prompts and "Instructions (from the analyst):\nDrop the ratio." in prompts[0]
    c = st.items["C-005"]
    rw = [a for a in c.verification.attempts if a.action == "rework"]
    assert rw and rw[-1].before == before.text and c.verification.status in ("pass", "fixed", "fail", "blocked")


async def test_summarizer_rerun_on_one_sentence_replaces_it_when_it_passes(run):
    ctx, st = run, run.state
    old = summarize.find_sentence(st.summary, "H-009").model_copy(deep=True)
    register_fake(summarize.SentenceRevision.TOOL_NAME,
                  lambda s, u, sch: {"text": st.items["C-005"].text, "mode": "copied", "derived_from": ["C-005"]})
    agent, task = rework.start_rerun(ctx, "summarizer-01", "Copy the conclusion as it is.", True, focus=["H-009"])
    assert agent.agent_id == "summarizer-01.2"
    out = await settle_task(task)
    new = summarize.find_sentence(st.summary, "H-009")
    assert out.sentences == ["H-009"] and new.verification.status == "pass" and new.certainty is not None
    assert new.text == st.items["C-005"].text and new.mode == "copied" and new.derived_from == ["C-005"]
    assert new.verification.attempts[-1].action == "rework" and new.verification.attempts[-1].before == old.text


async def test_a_failing_sentence_version_never_replaces_the_old_one(run):
    ctx, st = run, run.state
    old = summarize.find_sentence(st.summary, "H-010").model_copy(deep=True)
    register_fake(summarize.SentenceRevision.TOOL_NAME,
                  lambda s, u, sch: {"text": "Free cash flow will triple by 2030.", "mode": "derived",
                                     "derived_from": old.derived_from})
    register_fake(tool_name(summarizer.FaithfulnessVerdict),
                  lambda s, u, sch: {"supported": False, "unsupported_spans": ["will triple by 2030"],
                                     "reason": "No forecast in the evidence."})
    out = await settle_task(rework.start_rerun(ctx, "summarizer-01", "Say it will triple.", True, focus=["H-010"])[1])
    now = summarize.find_sentence(st.summary, "H-010")
    assert not out.sentences and now.text == old.text and now.verification.status == old.verification.status
    last = now.verification.attempts[-1]
    assert last.action == "rework" and last.status == "fail" and "keeps the previous one" in last.note
    assert last.after == "Free cash flow will triple by 2030."


async def test_a_whole_brief_rerun_writes_and_checks_a_new_brief(run):
    ctx, st = run, run.state
    mark = st.run.last_seq
    out = await settle_task(rework.start_rerun(ctx, "summarizer-01", "Lead with leverage.", True)[1])
    assert out.brief and out.sentences and st.summary.status == "final"
    assert min(int(s.split("-")[1]) for s in out.sentences) > 15                     # fresh H ids
    types_ = [e.type for e in since(ctx, mark)]
    assert "summary.sentence" in types_ and "summary.ready" in types_ and types_[-1] == "run.completed"


# ---------------------------------------------------------------------------
# What can't be rerun, and the route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_id,needle", [
    ("bobby", "Bobby can't be rerun"), ("fetcher-01", "Fetchers can't be rerun"),
    ("extractor-06", "reads structured data by code"), ("nobody-01", "No agent nobody-01"),
])
async def test_what_cant_be_rerun_is_refused_before_any_agent_is_registered(run, agent_id, needle):
    n = len(run.state.agents)
    with pytest.raises(InvalidState, match=re.escape(needle)):
        rework.start_rerun(run, agent_id, "x", True)
    assert len(run.state.agents) == n


async def test_a_working_agent_a_revoked_page_and_empty_instructions_are_refused(run):
    st = run.state
    st.agents["extractor-01"].status = "working"
    with pytest.raises(InvalidState, match="still working. Stop it first"):
        rework.start_rerun(run, "extractor-01", "x", True)
    st.sources_by_id()["S-006"].status = "revoked"
    with pytest.raises(InvalidState, match="S-006 was revoked"):
        rework.start_rerun(run, "extractor-05", "x", True)
    with pytest.raises(InvalidState, match="needs instructions"):
        rework.start_rerun(run, "extractor-03", "   ", True)


def test_repairs_count_from_the_latest_rework():
    at = "2026-11-08T03:00:00.000Z"
    att = [m.Attempt(attempt=1, status="fail", action="downgrade", at=at),
           m.Attempt(attempt=2, status="fail", action="delete", at=at)]
    assert repair.repairs_so_far(m.Verification(status="fail", attempts=att)) == 2
    att.append(m.Attempt(attempt=3, status="fail", action="rework", at=at))
    assert repair.repairs_so_far(m.Verification(status="fail", attempts=att)) == 0
    att.append(m.Attempt(attempt=4, status="fail", action="recompute", at=at))
    assert repair.repairs_so_far(m.Verification(status="fail", attempts=att)) == 1


def test_ids_of_reruns():
    st = type("S", (), {"agents": {"extractor-03": 1, "extractor-03.2": 1, "extractor-030": 1}})()
    assert rework.new_agent_id(st, "extractor-03") == "extractor-03.3"
    assert rework.new_agent_id(st, "extractor-03.2") == "extractor-03.3"
    assert rework.new_agent_id(st, "extractor-030") == "extractor-030.2"
    assert rework.base_task("Rerun of a.2: Rerun of a: Extract S-001 page 4") == "Extract S-001 page 4"


def test_the_route_answers_201_at_once_and_the_work_lands(settings, fixture_bundle, fixture_report):
    store = Store(settings)
    state, _ = build_state(store, settings, fixture_bundle, fixture_report)
    register_fake(extractor.TOOL, page_fake(state, "extractor-03"))
    with TestClient(create_app(settings, store=store)) as client:
        r = f"{P}/runs/{state.run.run_id}"
        got = client.post(f"{r}/agents/extractor-03/rerun", json={"instructions": "Whole rows, please."})
        assert got.status_code == 201, got.text
        new = m.Agent.model_validate(got.json())
        assert new.agent_id == "extractor-03.2" and new.rerun_by == "analyst" and new.parent_agent_id == "extractor-03"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run_ = client.get(r).json()
            agents = {a["agent_id"]: a for a in client.get(f"{r}/agents").json()["agents"]}
            if run_["status"] == "done" and agents["extractor-03.2"]["status"] == "done":
                break
            time.sleep(0.05)
        assert run_["status"] == "done" and agents["extractor-03.2"]["status"] == "done"
        items = {i["id"]: i for i in client.get(f"{r}/report").json()["items"]}
        assert any(a["action"] == "rework" for a in items["D-028"]["verification"]["attempts"])
        bad = client.post(f"{r}/agents/extractor-06/rerun", json={"instructions": "x"})
        assert bad.status_code == 409 and "reads structured data by code" in bad.json()["error"]["message"]


# ---------------------------------------------------------------------------
# A code-only JSON reader makes no gateway call, so extract checks the analyst's stop itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stopped_at", [1, 2])      # before reading, or after the facts were read and checked
async def test_a_json_reader_stopped_by_the_analyst_adds_nothing(run, monkeypatch, stopped_at):
    ctx, st = run, run.state
    n = len(st.items)
    calls = {"n": 0}

    def is_stopped(state, agent_id):
        calls["n"] += 1
        return calls["n"] >= stopped_at

    monkeypatch.setattr(extract, "is_stopped", is_stopped)
    job = extract.JsonJob("S-001", "DOC-002")
    job.runner = AgentRunner(ctx, "extractor", "Read S-001 structured facts by code (no model)", ["S-001"], None)
    mark = st.run.last_seq
    assert await extract.extract_json(ctx, job, extract.readable_pages(st)) == []
    assert len(st.items) == n and st.agents[job.runner.agent_id].status == "stopped"
    assert not since(ctx, mark, "item.created") and calls["n"] == stopped_at
    monkeypatch.setattr(extract, "is_stopped", lambda state, agent_id: False)
    again = extract.JsonJob("S-001", "DOC-002")
    assert len(await extract.extract_json(ctx, again, extract.readable_pages(st))) == 9
