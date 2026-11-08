"""V8-fork: the model cache (CACHE_MODELS=off|record|replay). Offline (FAKE_MODELS=1).

Every model answer here comes from a fake (simulated). record stores each live answer under
DATA_DIR/cache/models/, replay serves it back with cost 0 and cache "hit". The pages are the planted pages of a
fictional company (tests/data/planted/, read only). The gateway's decision always comes first: a blocked call
never reads or writes the cache.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from backend.axelrod import bedrock, cache
from backend.axelrod.agents import extractor as ex
from backend.axelrod.agents.base import AgentRunner, register_fake, tool_spec
from backend.axelrod.contract import m
from backend.axelrod.gateway import Blocked
from backend.axelrod.ids import now_iso
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import extract
from backend.axelrod.store import Store
from backend.axelrod.store.vault import data_root

PLANTED_DIR = Path(__file__).parent / "data" / "planted"
PLANTED = [(PLANTED_DIR / f"annual_report_p{i}.txt").read_text(encoding="utf-8") for i in (1, 2, 3)]
NEED = m.DataNeed(need_id="N-01", label="Returns on tangible capital", dimension="financials",
                  question="EBIT, taxes, total assets and cash by year?", data_types=["financial_filing"],
                  criteria=["rotic_level"], rationale="test (simulated)")


class Verdict(BaseModel):
    """A simulated judge verdict (test tool)."""

    TOOL_NAME: ClassVar[str] = "v8_test_verdict"
    verdict: str


VERDICT = tool_spec(Verdict)


@pytest.fixture(autouse=True)
def _restore_fakes():
    saved = dict(bedrock._FAKES)
    yield
    bedrock._FAKES.clear()
    bedrock._FAKES.update(saved)


def with_cache(settings, mode: str):
    """Settings with the model cache on and prices set, so a live (fake) call costs something."""
    prices = {settings.model_small: {"in": 1.0, "out": 5.0}, settings.model_large: {"in": 3.0, "out": 15.0}}
    return settings.model_copy(update={"cache_models": mode, "prices": prices})


def new_ctx(settings) -> RunContext:
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Meridian Diagnostics AG", ticker="MDX"))
    ctx = RunContext.create(state, store, store.vault, settings)
    ctx.emit("run.created", {"run": state.run}, message="created", stage="plan")
    return ctx


def planted_ctx(settings) -> RunContext:
    """A run at extract with one approved source and the three planted pages (simulated)."""
    ctx = new_ctx(settings)
    st = ctx.state
    st.needs = [NEED]
    src = m.Source(source_id="S-901", title="Meridian annual report FY2025 (simulated)", url="https://example.com/ar",
                   domain="example.com", kind="annual_report", data_types=["financial_filing"],
                   access=m.SourceAccess(), tier="priority", rank=1,
                   scores=m.SourceScores(reliability=0.95, relevance=0.9), rationale="test", need_ids=["N-01"],
                   criteria=["rotic_level"], vault_folder="01-Annual-Reports", proposed_by="planner",
                   approval="approved", status="fetched", doc_ids=["DOC-901"])
    st.sources = [src]
    st.documents = [m.Document(doc_id="DOC-901", source_id="S-901", title="S-901 (simulated)", url=src.url,
                               content_type="pdf", paginated=True, pages=3, sha256="0" * 64, bytes=1,
                               fetched_at=now_iso(), vault_paths=m.DocVaultPaths(raw="a.pdf", text="a.md", meta="a.json"),
                               scan=m.ScanResult(scanner="none", verdict="skipped"))]
    st.pages = {"DOC-901": list(PLANTED)}
    ctx.gateway.build_policy()
    ctx.emit("run.status", {"status": "running", "stage": "extract"}, stage="extract", message="Stage extract")
    return ctx


def calls(ctx: RunContext) -> list[m.GatewayRequestData]:
    return [e.data for e in ctx.state.events if e.type == "gateway.request" and e.data.action == "model.invoke"]


def records(settings) -> list[Path]:
    folder = data_root(settings) / cache.MODEL_PREFIX
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def comparable(ctx: RunContext) -> list[str]:
    """The run's data items as JSON with timestamps and run ids stripped, so two runs compare. Code numbers
    items in the order pages finish, which concurrency may change, so the D- id is left out and each item is
    known by its extractor (one per page, spawned in plan order) and its content."""
    out = []
    for it in ctx.state.data_items():
        raw = it.model_dump(mode="json")
        raw.pop("id")
        out.append(cache.strip_volatile(json.dumps(raw, sort_keys=True)).replace("<agent>", it.agent_id or ""))
    return sorted(out)


def live_call_is_a_failure(system, user, schema):
    raise AssertionError("replay made a live model call")


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def key_of(system: str, user: str, **kw) -> str:
    args = {"tool_name": "t", "tool_description": "d", "input_schema": {"type": "object"}, "max_tokens": 500}
    args.update(kw)
    return cache.model_key(args.pop("model_id", "model-a"), system, user, **args)


def test_the_key_ignores_run_ids_agent_ids_seq_numbers_and_timestamps():
    one = key_of("You are extractor-07 in run_20261108_abc123.",
                 'Run run_20261108_abc123 at 2026-11-08T03:00:00.000Z, "seq": 12, REQ-0042, judge verifier-C-005-1, '
                 "rerun extractor-03.2. Cite D-001 for FY2025 (as of 2026-11-07).")
    two = key_of("You are extractor-12 in run_20261109_zz9zz9.",
                 'Run run_20261109_zz9zz9 at 2026-11-09T11:22:33.456Z, "seq": 9876, REQ-1234, judge verifier-C-005-3, '
                 "rerun extractor-01.5. Cite D-001 for FY2025 (as of 2026-11-07).")
    assert one == two and len(one) == 64
    # Content still counts: item ids, numbers, plain dates, the model, the tool and max_tokens.
    base = ("s", "Cite D-001 for FY2025 (as of 2026-11-07).")
    assert key_of(*base) != key_of("s", "Cite D-002 for FY2025 (as of 2026-11-07).")
    assert key_of(*base) != key_of("s", "Cite D-001 for FY2025 (as of 2026-11-08).")
    assert key_of(*base) != key_of(*base, model_id="model-b")
    assert key_of(*base) != key_of(*base, max_tokens=501)
    assert key_of(*base) != key_of(*base, input_schema={"type": "object", "properties": {"x": {}}})
    assert key_of(*base) != key_of(*base, tool_name="other")
    assert cache.strip_volatile("bobby said D-001 at 2026-11-08") == "bobby said D-001 at 2026-11-08"


def test_record_layout_counts_repeated_prompts():
    k = "a" * 64
    assert cache.model_object_key(k) == f"cache/models/{k}.json"
    assert cache.model_object_key(k, 3) == f"cache/models/{k}.3.json"


@pytest.mark.parametrize("docs,models,label", [
    (False, "off", "off"), (True, "off", "documents"), (True, "record", "documents"),
    (False, "record", "off"), (True, "replay", "documents_and_models"), (False, "replay", "documents_and_models"),
])
def test_run_cache_mode_says_what_the_run_uses(settings, docs, models, label):
    s = settings.model_copy(update={"cache_documents": docs, "cache_models": models})
    assert cache.run_cache_mode(s) == label
    assert Store(s).create_run(m.CreateRunRequest(company="X")).run.cache_mode == label


# ---------------------------------------------------------------------------
# Record, then replay
# ---------------------------------------------------------------------------


async def test_record_then_replay_gives_identical_items_at_cost_zero(settings):
    rec_settings = with_cache(settings, "record")
    first = planted_ctx(rec_settings)
    await extract.run(first)
    rec_calls = calls(first)
    assert len(rec_calls) == 3 and all(c.cache == "miss" and c.cost_usd > 0 for c in rec_calls)
    assert first.state.run.usage.cost_usd > 0
    assert len(records(settings)) == 3
    stored = json.loads(records(settings)[0].read_text(encoding="utf-8"))
    assert stored["tool_name"] == ex.TOOL and stored["run_id"] == first.run_id and stored["tool_input"]["items"]

    register_fake(ex.TOOL, live_call_is_a_failure)        # any live call in the replay fails its page
    second = planted_ctx(with_cache(settings, "replay"))
    assert second.run_id != first.run_id and second.state.run.cache_mode == "documents_and_models"
    await extract.run(second)
    rep_calls = calls(second)
    assert len(rep_calls) == 3
    assert all(c.decision == "allowed" and c.cache == "hit" and c.cost_usd == 0.0 and c.cached_at for c in rep_calls)
    assert sorted((c.tokens_in, c.tokens_out) for c in rep_calls) == \
        sorted((c.tokens_in, c.tokens_out) for c in rec_calls)          # tokens as recorded
    usage = second.state.run.usage
    assert usage.cost_usd == 0.0 and usage.tokens_in == first.state.run.usage.tokens_in
    assert len(second.state.data_items()) == len(first.state.data_items()) == 14
    assert comparable(second) == comparable(first)                       # identical outputs
    assert len(records(settings)) == 3                                   # nothing new recorded


async def test_identical_prompts_keep_each_recorded_answer(settings):
    """Three judges see the same prompt. Replay gives back the same three answers, not one answer three times."""
    answers = iter(["SUPPORTED", "SUPPORTED", "UNSUPPORTED"])
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": next(answers)})

    async def judges(ctx: RunContext) -> list[str]:
        out = []
        for k in (1, 2, 3):
            runner = AgentRunner(ctx, "verifier", "Judge C-001 (simulated)", ["C-001"], "small",
                                 agent_id=f"verifier-C-001-{k}")
            user = f"Run {ctx.run_id}, agent {runner.agent_id}, at {now_iso()}: is C-001 supported by D-001?"
            out.append((await runner.ask("You judge one claim.", user, Verdict, max_tokens=200)).verdict)
            runner.finish()
        return out

    recorded = await judges(new_ctx(with_cache(settings, "record")))
    assert recorded == ["SUPPORTED", "SUPPORTED", "UNSUPPORTED"]
    names = {p.name for p in records(settings)}
    key = next(n for n in names if n.count(".") == 1)[:-5]
    assert names == {f"{key}.json", f"{key}.2.json", f"{key}.3.json"}  # one key, three recorded answers
    register_fake(VERDICT.name, live_call_is_a_failure)
    replay = new_ctx(with_cache(settings, "replay"))
    assert sorted(await judges(replay)) == sorted(recorded)
    assert [c.cache for c in calls(replay)] == ["hit"] * 3


async def test_a_replay_miss_calls_live_and_records_it(settings):
    seen = []
    register_fake(VERDICT.name, lambda system, user, schema: seen.append(user) or {"verdict": "SUPPORTED"})
    ctx = new_ctx(with_cache(settings, "replay"))
    runner = AgentRunner(ctx, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-009-1")
    assert (await runner.ask("sys", "Is C-009 supported?", Verdict, max_tokens=100)).verdict == "SUPPORTED"
    assert len(seen) == 1 and calls(ctx)[-1].cache == "miss" and calls(ctx)[-1].cost_usd > 0
    assert len(records(settings)) == 1
    again = new_ctx(with_cache(settings, "replay"))
    runner = AgentRunner(again, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-009-1")
    await runner.ask("sys", "Is C-009 supported?", Verdict, max_tokens=100)
    assert len(seen) == 1 and calls(again)[-1].cache == "hit"


async def test_a_bad_record_is_a_miss(settings):
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": "SUPPORTED"})
    ctx = new_ctx(with_cache(settings, "record"))
    runner = AgentRunner(ctx, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1")
    await runner.ask("sys", "Is C-001 supported?", Verdict, max_tokens=100)
    [path] = records(settings)
    path.write_text("{not json", encoding="utf-8")
    replay = new_ctx(with_cache(settings, "replay"))
    runner = AgentRunner(replay, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1")
    await runner.ask("sys", "Is C-001 supported?", Verdict, max_tokens=100)
    assert calls(replay)[-1].cache == "miss"
    assert json.loads(path.read_text(encoding="utf-8"))["tool_input"] == {"verdict": "SUPPORTED"}


async def test_cache_off_records_nothing_and_labels_nothing(settings):
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": "SUPPORTED"})
    ctx = new_ctx(settings)
    runner = AgentRunner(ctx, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1")
    await runner.ask("sys", "Is C-001 supported?", Verdict, max_tokens=100)
    assert calls(ctx)[-1].cache is None and records(settings) == []


# ---------------------------------------------------------------------------
# The decision comes first
# ---------------------------------------------------------------------------


@pytest.fixture
def spy(monkeypatch) -> list[str]:
    """Every model cache read and write, by method name."""
    seen: list[str] = []
    real_get, real_put = cache.ModelCache.get, cache.ModelCache.put

    def get(self, key, occurrence=1):
        seen.append("get")
        return real_get(self, key, occurrence)

    def put(self, rec):
        seen.append("put")
        return real_put(self, rec)

    monkeypatch.setattr(cache.ModelCache, "get", get)
    monkeypatch.setattr(cache.ModelCache, "put", put)
    return seen


def _kill(ctx, agent):
    ctx.state.kill_switch = True


def _stop(ctx, agent):
    ctx.gateway.stop_agent(agent, "analyst stopped it (test)")


def _broke(ctx, agent):
    ctx.state.run.usage.cost_usd = 99.0


@pytest.mark.parametrize("block,size,needle", [
    (_kill, "small", "Kill switch"), (_stop, "small", "Agent stopped by the analyst"),
    (_broke, "small", "Budget exceeded"), (None, "large", "may not call"),
])
@pytest.mark.parametrize("mode", ["record", "replay"])
async def test_a_blocked_call_never_reaches_the_cache(settings, spy, block, size, needle, mode):
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": "SUPPORTED"})
    # A recorded answer for exactly this call, so a cache read would be a hit.
    first = new_ctx(with_cache(settings, "record"))
    agent = AgentRunner(first, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1").agent
    await first.gateway.invoke(agent, "small", system="sys", user="Is C-001 supported?", tool=VERDICT, max_tokens=100)
    assert spy == ["put"] and len(records(settings)) == 1
    spy.clear()

    ctx = new_ctx(with_cache(settings, mode))
    agent = AgentRunner(ctx, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1").agent
    if block is not None:
        block(ctx, agent)
    with pytest.raises(Blocked) as err:
        await ctx.gateway.invoke(agent, size, system="sys", user="Is C-001 supported?", tool=VERDICT, max_tokens=100)
    assert needle in err.value.reason
    assert spy == []                                                     # never read, never written
    last = calls(ctx)[-1]
    assert last.decision == "blocked" and last.cache is None and last.tokens_in is None
    assert len(records(settings)) == 1


async def test_stop_while_reading_the_cache_discards_the_hit(settings, monkeypatch):
    register_fake(VERDICT.name, lambda system, user, schema: {"verdict": "SUPPORTED"})
    first = new_ctx(with_cache(settings, "record"))
    agent = AgentRunner(first, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1").agent
    await first.gateway.invoke(agent, "small", system="sys", user="q", tool=VERDICT, max_tokens=100)

    ctx = new_ctx(with_cache(settings, "replay"))
    agent = AgentRunner(ctx, "verifier", "Judge (simulated)", [], "small", agent_id="verifier-C-001-1").agent
    real = cache.ModelCache.aget

    async def stop_then_read(self, key, occurrence=1):
        ctx.state.kill_switch = True                                     # STOP lands while the cache is read
        return await real(self, key, occurrence)

    monkeypatch.setattr(cache.ModelCache, "aget", stop_then_read)
    with pytest.raises(Blocked) as err:
        await ctx.gateway.invoke(agent, "small", system="sys", user="q", tool=VERDICT, max_tokens=100)
    assert "Result discarded" in err.value.reason
    assert calls(ctx)[-1].decision == "blocked" and ctx.state.run.usage.model_calls == 0
