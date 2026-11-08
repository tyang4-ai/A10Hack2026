"""B-replay: CACHE_MODELS=replay_only, chat always live, stable ids. Offline (FAKE_MODELS=1).

Every model answer is a fake (simulated) on the planted pages of a fictional company. replay_only serves
recorded answers and blocks a miss instead of calling a model, so the page's work becomes a hole. Ids are
assigned in the plan's order, so two runs whose pages answer in a different order still match.
"""
from __future__ import annotations

import asyncio
import random

import pytest

from backend.axelrod import bedrock, cache, gateway
from backend.axelrod.agents import extractor as ex
from backend.axelrod.agents.base import AgentRunner, register_fake
from backend.axelrod.gateway import REPLAY_ONLY_REASON, Blocked, live_model_calls
from backend.axelrod.stages import extract

from test_model_cache import VERDICT, Verdict, calls, live_call_is_a_failure, planted_ctx, records, with_cache


@pytest.fixture(autouse=True)
def _restore_fakes():
    saved = dict(bedrock._FAKES)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


@pytest.fixture
def jitter(monkeypatch):
    """Random model latency, so pages answer in a different order each run."""
    real = bedrock.converse
    rng = random.Random()

    async def slow(*a, **kw):
        await asyncio.sleep(rng.random() * 0.02)
        return await real(*a, **kw)

    monkeypatch.setattr(bedrock, "converse", slow)


def snapshot(ctx) -> list[tuple[str, str, str]]:
    return [(it.id, it.statement, it.citations[0].quote) for it in ctx.state.data_items()]


async def test_ids_do_not_depend_on_answer_order(settings, jitter):
    runs = []
    for _ in range(3):
        ctx = planted_ctx(settings)
        await extract.run(ctx)
        runs.append(snapshot(ctx))
    assert runs[0] and runs[0] == runs[1] == runs[2]
    assert [i for i, _, _ in runs[0]] == [f"D-{n:03d}" for n in range(1, len(runs[0]) + 1)]


async def test_record_then_replay_only_is_all_hits_and_identical(settings, jitter):
    first = planted_ctx(with_cache(settings, "record"))
    await extract.run(first)
    assert len(records(settings)) == 3
    register_fake(ex.TOOL, live_call_is_a_failure)
    second = planted_ctx(with_cache(settings, "replay_only"))
    assert second.state.run.cache_mode == "documents_and_models"
    await extract.run(second)
    rep = calls(second)
    assert len(rep) == 3 and all(c.cache == "hit" and c.cost_usd == 0.0 for c in rep)
    assert second.state.run.usage.cost_usd == 0.0
    assert snapshot(second) == snapshot(first)


async def test_replay_only_miss_is_blocked_and_the_page_fails(settings):
    register_fake(ex.TOOL, live_call_is_a_failure)
    ctx = planted_ctx(with_cache(settings, "replay_only"))
    await extract.run(ctx)
    got = calls(ctx)
    assert len(got) == 3 and all(c.decision == "blocked" and c.reason == REPLAY_ONLY_REASON for c in got)
    assert ctx.state.data_items() == []
    extractors = [a for a in ctx.state.agents.values() if a.role == "extractor"]
    assert extractors and all(a.status == "failed" for a in extractors)   # finalize declares these as holes
    assert records(settings) == []


async def test_chat_calls_are_live_in_replay_only(settings):
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": "SUPPORTED"})
    ctx = planted_ctx(with_cache(settings, "replay_only"))
    runner = AgentRunner(ctx, "verifier", "Chat (simulated)", [], "small")
    with live_model_calls():
        out = await runner.ask("You are a judge.", "Is D-001 right?", Verdict, max_tokens=200)
    assert out.verdict == "SUPPORTED"
    got = calls(ctx)
    assert len(got) == 1 and got[0].decision == "allowed" and got[0].cache is None and got[0].cost_usd > 0
    assert records(settings) == []                     # chat is never recorded
    assert gateway.LIVE_ONLY.get() is False
    with pytest.raises(Blocked):                       # outside chat, the same call is a replay_only miss
        await ctx.gateway.invoke(runner.agent, "small", system="You are a judge.", user="Is D-001 right?",
                                 tool=VERDICT, max_tokens=200)


async def test_the_decision_comes_before_the_cache(settings):
    first = planted_ctx(with_cache(settings, "record"))
    await extract.run(first)
    ctx = planted_ctx(with_cache(settings, "replay_only"))
    ctx.gateway.ctx.settings = ctx.gateway.ctx.settings.model_copy(update={"budget_usd": 0.0})
    runner = AgentRunner(ctx, "extractor", "x", [], "small")
    with pytest.raises(Blocked) as e:
        await ctx.gateway.invoke(runner.agent, "large", system="s", user="u", tool=VERDICT, max_tokens=10)
    assert e.value.reason != REPLAY_ONLY_REASON          # extractors may not call the large model: role scope


def test_key_v2_strips_spend_and_durations_but_not_values():
    a = cache.strip_volatile("Spent $0.42 so far, $1.20 left. cost_usd: 0.031, latency_ms: 812, took 3.2 s, 40 ms.")
    b = cache.strip_volatile("Spent $1.97 so far, $0.05 left. cost_usd: 0.9, latency_ms: 77, took 12 s, 9 ms.")
    assert a == b
    v = "Revenue was EUR 22,363 million, cost of revenue 13,000, spent EUR 1,780 million on R&D, $5 billion."
    assert cache.strip_volatile(v) == v
