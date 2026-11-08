"""V3-rework: Bobby at the stage gates and on comments (CONTRACT.md 14). Offline (FAKE_MODELS=1).

Bobby's answers come from fakes (simulated). Code validates every call: the goons exist and are goons a rerun
can redo, each once, at most 3 per gate, within the budget. Only code checks decide pass or fail. The run is
the mock fixture's finished report (fictional company), built by test_rework.build_state.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from backend.axelrod import comments, gates, orchestrator
from backend.axelrod.agents import bobby, extractor
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.contract import m
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.store import Store
from test_rework import _defaults, build_state, page_fake, since

GATE = tool_name(bobby.GateCall)
COMMENT = tool_name(bobby.CommentCall)


@pytest.fixture(autouse=True)
def _fakes():
    _defaults()
    bobby.register_fakes()
    yield
    _defaults()
    bobby.register_fakes()


@pytest.fixture
def run(settings, fixture_bundle, fixture_report):
    store = Store(settings)
    _, ctx = build_state(store, settings, fixture_bundle, fixture_report)
    return ctx


def reopen_for_gate(ctx: RunContext, stage: str) -> None:
    """Put the finished fixture run back at a gate, as the pipeline would be (Bobby working, run running)."""
    ctx.emit("run.status", {"status": "running", "stage": stage}, stage=stage, message=f"Stage {stage}")
    b = ctx.state.agents["bobby"]
    b.status, b.finished_at = "working", None


def gate_fake(answer: dict, seen: list[str] | None = None):
    def fake(system, user, schema):
        if seen is not None:
            seen.append(user)
        return answer
    return fake


def decisions(ctx: RunContext, mark: int = 0) -> list[m.AgentDecisionData]:
    return [e.data for e in since(ctx, mark, "agent.decision")]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_a_gate_with_a_proceed_call_records_the_decision_through_the_gateway(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "extract")
    seen: list[str] = []
    register_fake(GATE, lambda s, u, sch: seen.append(u) or bobby.fake_gate(s, u, sch))
    mark = st.run.last_seq
    await gates.after_stage(ctx, "extract")
    [d] = decisions(ctx, mark)
    assert (d.agent_id, d.stage, d.decision, d.reworks, d.comment_id) == ("bobby", "extract", "proceed", [], None)
    assert d.summary.startswith("Simulated") and st.decisions[-1] == d
    reqs = [e for e in since(ctx, mark, "gateway.request")]
    assert len(reqs) == 1 and reqs[0].data.role == "orchestrator" and reqs[0].data.decision == "allowed"
    assert reqs[0].data.target == ctx.settings.model_large and reqs[0].agent_id == "bobby"
    assert "Stage gate: after extract." in seen[0] and "Data items:" in seen[0] and "Failures by check" in seen[0]
    assert st.agents["bobby"].usage.model_calls == 1 and st.agents["bobby"].status == "working"


async def test_a_valid_rework_runs_the_goon_and_the_gate_waits_for_it(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "extract")
    register_fake(extractor.TOOL, page_fake(st, "extractor-03"))
    register_fake(GATE, gate_fake({"decision": "rework", "summary": "D-028 needs its row again.",
                                   "reworks": [{"agent_id": "extractor-03", "instructions": "Quote the whole row."}]}))
    mark = st.run.last_seq
    await gates.after_stage(ctx, "extract")
    [d] = decisions(ctx, mark)
    assert d.decision == "rework" and [(r.agent_id, r.instructions) for r in d.reworks] == [
        ("extractor-03", "Quote the whole row.")]
    new = st.agents["extractor-03.2"]
    assert new.rerun_by == "orchestrator" and new.parent_agent_id == "extractor-03" and new.status == "done"
    assert any(a.action == "rework" for a in st.items["D-028"].verification.attempts)
    evs = since(ctx, mark)
    decided = next(i for i, e in enumerate(evs) if e.type == "agent.decision")
    spawned = next(i for i, e in enumerate(evs) if e.type == "agent.spawned" and e.agent_id == "extractor-03.2")
    assert decided < spawned
    assert "Instructions (from Bobby, the orchestrator):" in ctx.vault.get_text(
        ctx.run_id, "00-Meta/agents/extractor-03.2.jsonl")


async def test_code_rejects_invalid_reworks_and_keeps_at_most_three(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "extract")
    for aid in ("extractor-01", "extractor-02", "extractor-03", "extractor-04"):
        register_fake(extractor.TOOL, page_fake(st, aid))       # the last one wins: fine, each only re-reads
    st.agents["extractor-05"].status = "stopped"
    orders = [{"agent_id": a, "instructions": "Again, carefully."} for a in
              ("nobody-99", "fetcher-01", "bobby", "extractor-06", "extractor-05", "extractor-01", "extractor-01",
               "extractor-02", "extractor-03", "extractor-04")]
    orders.append({"agent_id": "extractor-02", "instructions": "   "})
    register_fake(GATE, gate_fake({"decision": "rework", "summary": "Lots to redo.", "reworks": orders}))
    mark = st.run.last_seq
    await gates.after_stage(ctx, "extract")
    [d] = decisions(ctx, mark)
    assert d.decision == "rework" and [r.agent_id for r in d.reworks] == ["extractor-01", "extractor-02", "extractor-03"]
    for needle in ("nobody-99: no such agent", "fetcher-01: Fetchers can't be rerun", "bobby: Bobby can't be rerun",
                   "extractor-06: extractor-06 reads structured data by code", "extractor-05: stopped by the analyst",
                   "extractor-01: already sent back at this gate", "extractor-04: at most 3 goons per gate"):
        assert needle in d.summary, (needle, d.summary)
    assert "extractor-04.2" not in st.agents and "extractor-01.2" in st.agents and "extractor-01.3" not in st.agents


async def test_a_rework_call_with_nothing_valid_proceeds(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "repair")
    register_fake(GATE, gate_fake({"decision": "rework", "summary": "Redo it.",
                                   "reworks": [{"agent_id": "drafter-99", "instructions": "x"}]}))
    mark = st.run.last_seq
    n_agents = len(st.agents)
    await gates.after_stage(ctx, "repair")
    [d] = decisions(ctx, mark)
    assert d.decision == "proceed" and d.reworks == [] and "drafter-99: no such agent" in d.summary
    assert "Nothing to rework" in d.summary and len(st.agents) == n_agents


async def test_the_budget_left_limits_reworks(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "repair")
    st.run.usage.cost_usd = ctx.settings.budget_usd - 0.65     # the rest of the run needs 0.60 after repair
    register_fake(GATE, gate_fake({"decision": "rework", "summary": "Redraft liquidity.",
                                   "reworks": [{"agent_id": "drafter-04", "instructions": "Use FY2025."}]}))
    mark = st.run.last_seq
    await gates.after_stage(ctx, "repair")
    [d] = decisions(ctx, mark)
    assert d.decision == "proceed" and "can't cover a drafter rerun" in d.summary and "drafter-04.2" not in st.agents


async def test_escalate_is_a_note_for_the_analyst_and_reworks_on_other_calls_are_ignored(run):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "repair")
    register_fake(GATE, gate_fake({"decision": "escalate", "summary": "No peer data: rotic_vs_peers needs a source.",
                                   "reworks": [{"agent_id": "drafter-01", "instructions": "x"}]}))
    mark = st.run.last_seq
    await gates.after_stage(ctx, "repair")
    [d] = decisions(ctx, mark)
    assert d.decision == "escalate" and d.reworks == [] and "Code ignored the 1 rework order" in d.summary
    assert "drafter-01.2" not in st.agents
    assert next(e for e in since(ctx, mark, "agent.decision")).level == "warn"


@pytest.mark.parametrize("how", ["status", "rule"])
async def test_a_stopped_bobby_means_proceed_at_every_gate_without_a_model_call(run, how):
    ctx, st = run, run.state
    reopen_for_gate(ctx, "extract")
    calls: list[str] = []
    register_fake(GATE, gate_fake({"decision": "rework", "summary": "x", "reworks": []}, calls))
    if how == "status":
        b = st.agents["bobby"]
        b.status = "stopped"
        ctx.emit("agent.status", m.AgentStatusData(agent_id="bobby", status="stopped", task=b.task, usage=b.usage,
                                                   reason="Stopped by the analyst"), agent_id="bobby",
                 message="bobby stopped")
    else:
        ctx.gateway.build_policy()
        st.policy.rules.append(m.PolicyRule(rule_id="R-stop-bobby", effect="deny", action="model.invoke",
                                            roles=["orchestrator"], origin="user_agent_stop", agent_id="bobby",
                                            note="Stopped by the analyst", created_at=now_iso()))
    mark = st.run.last_seq
    for stage in gates.GATE_STAGES:
        await gates.after_stage(ctx, stage)
    ds = decisions(ctx, mark)
    assert [d.decision for d in ds] == ["proceed"] * 4 and all("his calls are off" in d.summary for d in ds)
    assert not calls and not since(ctx, mark, "gateway.request")
    assert any("his calls are off" in e.data.text for e in since(ctx, mark, "agent.log"))


def test_digests_are_short_and_name_the_goons(run):
    ctx = run
    for stage in gates.GATE_STAGES:
        text = gates.build_digest(ctx, stage)
        assert text.startswith(f"Stage gate: after {stage}.") and "Budget: $" in text
        assert len(text) < 2500, (stage, len(text))                 # a few hundred tokens: a gate costs cents
    rep = gates.build_digest(ctx, "repair")
    assert "Criteria scored: 4 of 6." in rep and "- liquidity" in rep and "drafter-" in rep
    assert "summarizer-01" in gates.build_digest(ctx, "verify_summary")
    assert "Goons you may send back: none." in gates.build_digest(ctx, "plan")
    prompt = bobby.GATE_SYSTEM + bobby.gate_user(rep)
    assert len(prompt) < 4500                                       # about 1,100 tokens in, at most


async def test_the_pipeline_asks_bobby_at_the_four_gates(settings, monkeypatch):
    async def noop(ctx):
        return None

    monkeypatch.setattr(orchestrator, "STAGES", [(s, noop) for s, _ in orchestrator.STAGES])
    store = Store(settings)
    ctx = await orchestrator.start_run(store, store.vault, settings,
                                       m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
    await ctx.task
    st = ctx.state
    assert st.run.status == "done", st.run.error
    assert [(d.stage, d.decision) for d in st.decisions] == [(s, "proceed") for s in gates.GATE_STAGES]
    types_ = [e.type for e in st.events]
    last_decision = max(i for i, t in enumerate(types_) if t == "agent.decision")
    assert last_decision < types_.index("run.completed") and st.agents["bobby"].status == "done"


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def add_comment(ctx: RunContext, target_type: str, target_id: str | None, text: str) -> m.Comment:
    """What POST /comments does before it hands the comment to Bobby."""
    c = m.Comment(comment_id=ctx.next_id("CM"), target_type=target_type, target_id=target_id, text=text,
                  status="queued", created_at=now_iso())
    ctx.state.comments.append(c)
    ctx.emit("comment.created", {"comment": c}, message=f"Comment {c.comment_id}")
    return c


async def test_a_comment_on_a_data_item_sends_its_extractor_back_and_resolves(run):
    ctx, st = run, run.state
    register_fake(extractor.TOOL, page_fake(st, "extractor-03"))
    seen: list[str] = []
    register_fake(COMMENT, lambda s, u, sch: seen.append(u) or bobby.fake_comment(s, u, sch))
    c = add_comment(ctx, "item", "D-028", "The total debt row also lists the 2030 notes.")
    mark = st.run.last_seq
    await comments.on_comment(ctx, c)
    assert "Producing goon: extractor-03 (extractor" in seen[0] and "Total debt 1,150" in seen[0]
    assert c.status == "applied" and "D-028" in c.changed_item_ids and c.resolved_at
    assert c.response.startswith("Simulated") and "D-028 is" in c.response
    [d] = decisions(ctx, mark)
    assert d.comment_id == c.comment_id and d.decision == "rework" and d.reworks[0].agent_id == "extractor-03"
    assert d.reworks[0].instructions == c.text and st.agents["extractor-03.2"].rerun_by == "orchestrator"
    evs = since(ctx, mark)
    types_ = [e.type for e in evs]
    reopened = next(i for i, e in enumerate(evs) if e.type == "run.status" and e.data.status == "running")
    spawned = next(i for i, e in enumerate(evs) if e.type == "agent.spawned" and e.agent_id == "extractor-03.2")
    assert reopened < spawned                              # the finished run reopens for the rework
    assert types_.index("comment.resolved") < types_.index("run.completed") and st.run.status == "done"
    resolved = next(e for e in since(ctx, mark, "comment.resolved"))
    assert resolved.data.comment.status == "applied"


async def test_a_comment_on_the_run_is_answered_without_a_change(run):
    ctx, st = run, run.state
    c = add_comment(ctx, "run", None, "Why is research_stage not scored?")
    mark = st.run.last_seq
    n_agents = len(st.agents)
    await comments.on_comment(ctx, c)
    assert c.status == "applied" and c.changed_item_ids == [] and "nothing changes" in c.response
    [d] = decisions(ctx, mark)
    assert d.decision == "proceed" and d.comment_id == c.comment_id and len(st.agents) == n_agents
    assert st.run.status == "done" and "run.completed" not in [e.type for e in since(ctx, mark)]


async def test_bobby_cant_rework_what_code_wont_rerun_and_says_so(run):
    ctx, st = run, run.state
    register_fake(COMMENT, gate_fake({"action": "rework", "answer": "I'll have it reread.",
                                      "instructions": "Reread the facts."}))
    c = add_comment(ctx, "item", "D-001", "Check this XBRL value.")       # read by code (extractor-06, no model)
    await comments.on_comment(ctx, c)
    assert c.status == "applied" and "Code sent nobody back" in c.response and "reads structured data by code" in c.response
    assert "extractor-06.2" not in st.agents and st.decisions[-1].decision == "proceed"


async def test_a_comment_on_a_brief_sentence_reworks_only_that_sentence(run):
    ctx, st = run, run.state
    c = add_comment(ctx, "summary_sentence", "H-009", "Say it plainly, as the conclusion does.")
    await comments.on_comment(ctx, c)
    assert st.agents["summarizer-01.2"].parent_agent_id == "summarizer-01"
    assert c.status == "applied" and st.decisions[-1].reworks[0].agent_id == "summarizer-01"
    from backend.axelrod.stages import summarize
    s = summarize.find_sentence(st.summary, "H-009")
    assert s.verification.attempts[-1].action == "rework"


async def test_with_bobby_stopped_comments_are_rejected_without_a_model_call(run):
    ctx, st = run, run.state
    calls: list[str] = []
    register_fake(COMMENT, gate_fake({"action": "answer", "answer": "x"}, calls))
    b = st.agents["bobby"]
    b.status = "stopped"
    ctx.emit("agent.status", m.AgentStatusData(agent_id="bobby", status="stopped", task=b.task, usage=b.usage,
                                               reason="Stopped by the analyst"), agent_id="bobby", message="stopped")
    c = add_comment(ctx, "item", "D-028", "Fix this.")
    await comments.on_comment(ctx, c)
    assert c.status == "rejected" and "his calls are off" in c.response and not calls


async def test_a_blocked_bobby_call_rejects_the_comment_with_the_reason(run):
    ctx, st = run, run.state
    st.run.usage.budget_usd = 0.0
    st.run.usage.cost_usd = 0.01
    c = add_comment(ctx, "run", None, "Anything else?")
    await comments.on_comment(ctx, c)
    assert c.status == "rejected" and c.response.startswith("Bobby could not answer: blocked by the gateway")


# ---------------------------------------------------------------------------
# One live gate call (Sonnet 5, through the gateway). Run with AXELROD_LIVE=1.
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_one_gate_call_costs_cents(fixture_bundle, fixture_report):
    from backend.axelrod.config import get_settings

    settings = get_settings().model_copy(update={"store": "local", "data_dir": Path("backend/.data/v3live"),
                                                 "fake_models": False})
    store = Store(settings)
    _, ctx = build_state(store, settings, fixture_bundle, fixture_report)
    reopen_for_gate(ctx, "repair")
    t0 = time.perf_counter()
    call = await bobby.gate_call(ctx, "repair", gates.build_digest(ctx, "repair"))
    seconds = time.perf_counter() - t0
    req = [e.data for e in ctx.state.events if e.type == "gateway.request"][-1]
    print(f"\nlive gate call: {call.decision}: {call.summary!r} reworks={[(r.agent_id, r.instructions) for r in call.reworks]}"
          f"\n{req.tokens_in} in / {req.tokens_out} out, ${req.cost_usd:.4f}, {seconds:.1f} s, model {req.target}")
    assert req.decision == "allowed" and req.role == "orchestrator" and req.cost_usd < 0.05
    assert os.environ.get("AXELROD_LIVE") == "1"


async def test_bobby_stopped_while_his_gate_call_is_in_flight_means_proceed(run):
    """The analyst stops Bobby while he thinks: the gateway discards the late answer and code proceeds."""
    ctx, st = run, run.state
    reopen_for_gate(ctx, "repair")
    ctx.gateway.build_policy()

    def fake(system, user, schema):
        st.policy.rules.append(m.PolicyRule(rule_id="R-stop-bobby", effect="deny", action="model.invoke",
                                            origin="user_agent_stop", agent_id="bobby", created_at=now_iso()))
        return {"decision": "rework", "summary": "Redraft.", "reworks": [{"agent_id": "drafter-04",
                                                                          "instructions": "x"}]}

    register_fake(GATE, fake)
    mark = st.run.last_seq
    await gates.after_stage(ctx, "repair")
    [d] = decisions(ctx, mark)
    assert d.decision == "proceed" and "his calls are off" in d.summary and "drafter-04.2" not in st.agents
    req = [e.data for e in since(ctx, mark, "gateway.request")]
    assert len(req) == 1 and req[0].decision == "blocked"            # metered, then discarded
