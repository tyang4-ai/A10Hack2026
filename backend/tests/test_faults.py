"""B-faults: planted faults behind DEMO_FAULTS (all simulated), and the real handling they show off.

Offline: FAKE_MODELS=1 judges (simulated) and a fake HTTP layer (fetch.http.get_once). Fault 1 is S-017, a planted
broken download that answers HTTP 503: the fetcher retries once, marks it failed and finalize declares the hole.
Fault 2 is one malformed judge verdict per run: the judge flags it, reruns once and the rerun's verdict counts.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.axelrod import cache, faults
from backend.axelrod.agents import judge
from backend.axelrod.agents.base import register_fake, tool_name
from backend.axelrod.api import create_app
from backend.axelrod.config import BACKEND_DIR
from backend.axelrod.contract import m
from backend.axelrod.fetch import http
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import collect, finalize, verify
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import Store

SHL = m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api")
HOST = "https://abc123def4.execute-api.us-east-1.amazonaws.com"
TEST_PAGE_URL = HOST + "/test-pages/injection.html"
BROKEN_URL = HOST + "/test-pages/broken.pdf"
AR_URL = "https://www.ir.example.com/reports/annual-report-2025.pdf"
PDF = b"%PDF-1.7 simulated annual report"
PLANTED_TITLE = "Axelrod test file: broken download (simulated)"
JUDGE = tool_name(judge.JudgeVerdict)


@pytest.fixture(autouse=True)
def _no_faults(monkeypatch):
    """Every test starts with DEMO_FAULTS unset and the module's fake judge (simulated), and no retry wait."""
    monkeypatch.delenv(faults.ENV_VAR, raising=False)
    monkeypatch.setattr(collect, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    judge.register_fakes()
    yield
    judge.register_fakes()


def demo_faults(monkeypatch, settings, value: str):
    """Turn planted faults on through the env var, and through settings once config.py has the field."""
    monkeypatch.setenv(faults.ENV_VAR, value)
    if "demo_faults" in type(settings).model_fields:
        return settings.model_copy(update={"demo_faults": value})
    return settings


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_kinds_parse_the_env_var(monkeypatch):
    assert faults.kinds() == set() and not faults.enabled(None, "fetch")
    monkeypatch.setenv(faults.ENV_VAR, " Fetch , judge,typo ")
    assert faults.kinds() == {"fetch", "judge"}
    assert faults.enabled(None, "judge") and faults.enabled(object(), "fetch")
    monkeypatch.setenv(faults.ENV_VAR, "")
    assert faults.kinds() == set()


def test_a_settings_field_wins_over_the_env(monkeypatch):
    class WithField:
        demo_faults = "judge"
    monkeypatch.setenv(faults.ENV_VAR, "fetch")
    assert faults.kinds(WithField()) == {"judge"}


# ---------------------------------------------------------------------------
# Plan: S-017 only with DEMO_FAULTS=fetch
# ---------------------------------------------------------------------------


async def _plan_run(settings) -> RunContext:
    store = Store(settings)
    state = store.create_run(SHL)
    ctx = RunContext.create(state, store, store.vault, settings)
    await plan_stage.run(ctx)
    return ctx


async def test_plan_leaves_s017_out_when_faults_are_off(settings):
    ctx = await _plan_run(settings)
    ids = [s.source_id for s in ctx.state.sources]
    assert "S-017" not in ids and len(ids) == 16
    assert not any(faults.is_broken_download(s) for s in ctx.state.sources)
    assert "DEMO_FAULTS" not in (ctx.state.run.notes or "")


async def test_plan_has_s017_last_and_served_by_our_api_with_fetch_on(settings, monkeypatch):
    s = demo_faults(monkeypatch, settings, "fetch").model_copy(update={"test_page_url": TEST_PAGE_URL})
    ctx = await _plan_run(s)
    s017 = ctx.state.sources_by_id()["S-017"]
    assert s017.title == PLANTED_TITLE and faults.is_broken_download(s017)
    assert s017.url == BROKEN_URL and s017.domain == HOST.removeprefix("https://")
    assert s017.tier == "tier_3" and s017.need_ids == [] and s017.criteria == []
    assert "SIMULATED" in s017.rationale and "503" in s017.rationale
    assert ctx.state.sources_by_id()["S-010"].url == TEST_PAGE_URL          # the injection page is unchanged
    approved = [x.model_copy(update={"approval": "auto_approved"}) for x in ctx.state.sources]
    assert [x.source_id for x in collect.collect_order(approved)][-1] == "S-017"     # fetched after S-010
    notes = ctx.state.run.notes or ""
    assert "S-017" in notes and "simulated" in notes and "DEMO_FAULTS=fetch" in notes


async def test_plan_keeps_the_plan_json_url_without_test_page_url(settings, monkeypatch):
    ctx = await _plan_run(demo_faults(monkeypatch, settings, "fetch"))
    s017 = ctx.state.sources_by_id()["S-017"]
    assert s017.url.endswith(faults.BROKEN_FILE)


def test_served_url_sits_next_to_the_injection_page():
    assert faults.served_url(TEST_PAGE_URL) == BROKEN_URL
    assert faults.served_url("http://127.0.0.1:8123/test-pages/injection.html") == (
        "http://127.0.0.1:8123/test-pages/broken.pdf")


# ---------------------------------------------------------------------------
# The test-pages route (FastAPI TestClient)
# ---------------------------------------------------------------------------


def test_route_answers_503_with_the_simulated_header(settings):
    with TestClient(create_app(settings)) as c:
        r = c.get("/test-pages/broken.pdf")
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        assert r.headers["x-axelrod-simulated"] == "true"
        assert "simulated" in r.text
        ok = c.get("/test-pages/injection.html")
        assert ok.status_code == 200 and ok.headers["x-axelrod-simulated"] == "true"
        assert ok.content == (BACKEND_DIR / "data" / "test_pages" / "injection.html").read_bytes()
    assert faults.test_page_response("injection.html") is None
    assert faults.test_page_response("other.pdf") is None


# ---------------------------------------------------------------------------
# Collect: one retry on 5xx or a timeout, then failed, and a hole
# ---------------------------------------------------------------------------


def src(sid: str, url: str, title: str, *, tier: str = "priority", need_ids=("N-01",)) -> m.Source:
    return m.Source(source_id=sid, title=title, url=url, domain=url.split("/")[2], kind="annual_report",
                    data_types=["financial_filing"], access=m.SourceAccess(), tier=tier, rank=1,
                    scores=m.SourceScores(reliability=0.9, relevance=0.9), rationale="test (simulated)",
                    need_ids=list(need_ids), criteria=["leverage"] if need_ids else [],
                    vault_folder="01-Annual-Reports", proposed_by="planner", approval="approved", status="queued")


def make_ctx(settings, sources: list[m.Source]) -> RunContext:
    s = settings.model_copy(update={"cache_documents": True})
    store = Store(s)
    state = store.create_run(SHL)
    state.sources = [x.model_copy(deep=True) for x in sources]
    state.run.stage = "collect"
    ctx = RunContext.create(state, store, store.vault, s)
    ctx.gateway.build_policy()
    return ctx


@pytest.fixture
def net(monkeypatch) -> dict:
    """URL -> list of answers, used in order: (status, headers, body) or an exception to raise."""
    table: dict = {"_calls": []}

    async def fake_get_once(url, *, user_agent, **_):
        table["_calls"].append(url)
        answer = table[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(http, "get_once", fake_get_once)
    return table


@pytest.fixture
def one_page_pdf(monkeypatch):
    from backend.axelrod.fetch import ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "pdf_pages", lambda body: ["page one text (simulated)"])


def fetches(ctx: RunContext, url: str) -> list[m.GatewayRequestData]:
    return [e.data for e in ctx.state.events if e.type == "gateway.request" and e.data.target == url]


BUSY = (503, {"content-type": "text/plain", "retry-after": "1"}, b"busy (simulated)")


async def test_broken_download_is_tried_twice_fails_and_becomes_a_hole(settings, monkeypatch, net, one_page_pdf):
    s = demo_faults(monkeypatch, settings, "fetch")
    ctx = make_ctx(s, [src("S-001", AR_URL, "Annual report (simulated)"),
                       src("S-017", BROKEN_URL, PLANTED_TITLE, tier="tier_3", need_ids=())])
    net[AR_URL] = [BUSY, (200, {"content-type": "application/pdf"}, PDF)]     # a flaky real site: 503, then 200
    net[BROKEN_URL] = [BUSY, BUSY]
    await collect.run(ctx)                                                   # the stage carries on to the end

    tries = fetches(ctx, BROKEN_URL)
    assert [(r.decision, r.http_status) for r in tries] == [("allowed", 503), ("allowed", 503)]
    assert tries[0].request_id != tries[1].request_id
    s017 = ctx.state.sources_by_id()["S-017"]
    assert s017.status == "failed" and s017.doc_ids == []
    assert s017.error == "HTTP 503 twice: the download failed (simulated, planted fault)"
    warn = [e for e in ctx.state.events if e.type == "source.status" and e.data.source_id == "S-017"
            and e.data.status == "failed"]
    assert len(warn) == 1 and warn[0].level == "warn"
    retry_logs = [e.data.text for e in ctx.state.events if e.type == "agent.log" and "Retrying once" in e.data.text]
    assert any(t.startswith("S-017") and "simulated" in t for t in retry_logs)
    assert any(t.startswith("S-001") and "simulated" not in t for t in retry_logs)

    s001 = ctx.state.sources_by_id()["S-001"]                                # 503 once, then stored normally
    assert s001.status == "fetched" and len(s001.doc_ids) == 1
    assert [r.http_status for r in fetches(ctx, AR_URL)] == [503, 200]
    assert ctx.state.agents["fetcher-01"].status == "done"

    doc_cache = cache.DocCache(ctx.settings)
    assert doc_cache.meta(BROKEN_URL) is None                                # a 5xx is never cached
    assert doc_cache.meta(AR_URL) is not None

    holes = finalize.build_holes(ctx, [])
    hole = [h for h in holes if "S-017" in h.reason]
    assert len(hole) == 1 and "simulated" in hole[0].reason and "could not be fetched" in hole[0].reason
    assert not any("S-001" in h.reason for h in holes)


async def test_a_timeout_gets_one_retry_too(settings, net, one_page_pdf):
    ctx = make_ctx(settings, [src("S-001", AR_URL, "Annual report (simulated)")])
    net[AR_URL] = [TimeoutError(), (200, {"content-type": "application/pdf"}, PDF)]
    await collect.run(ctx)
    assert [r.decision for r in fetches(ctx, AR_URL)] == ["blocked", "allowed"]
    assert ctx.state.sources_by_id()["S-001"].status == "fetched"


async def test_two_timeouts_fail_the_source_without_the_planted_label(settings, net):
    ctx = make_ctx(settings, [src("S-001", AR_URL, "Annual report (simulated)")])
    net[AR_URL] = [TimeoutError(), TimeoutError()]
    await collect.run(ctx)
    s = ctx.state.sources_by_id()["S-001"]
    assert s.status == "failed" and "twice" in s.error and "planted" not in s.error
    assert len(fetches(ctx, AR_URL)) == 2


async def test_a_404_or_a_policy_block_is_not_retried(settings, net):
    ctx = make_ctx(settings, [src("S-001", AR_URL, "Annual report (simulated)")])
    net[AR_URL] = [(404, {}, b"")]
    await collect.run(ctx)
    assert len(fetches(ctx, AR_URL)) == 1 and ctx.state.sources_by_id()["S-001"].error == "HTTP 404"
    ctx2 = make_ctx(settings, [src("S-002", AR_URL, "Annual report (simulated)")])
    ctx2.gateway.revoke("S-002", "analyst revoked it")
    await collect.run(ctx2)
    assert len(fetches(ctx2, AR_URL)) == 1 and ctx2.state.sources_by_id()["S-002"].status == "revoked"


def test_cache_never_stores_a_5xx():
    assert not cache.storable(503, None) and not cache.storable(500, None)
    assert cache.storable(200, None)


# ---------------------------------------------------------------------------
# Judge: one malformed verdict per run, flagged and rerun once
# ---------------------------------------------------------------------------


def conclusion(cid: str) -> m.Conclusion:
    return m.Conclusion(id=cid, category="financial_strength", criteria=["leverage"],
                        text=f"Net debt fell in fiscal year 2025 ({cid}, simulated).", data_ids=["D-001"], computations=[],
                        agent_id="drafter-01", verification=m.Verification(status="pending"))


def judge_ctx(settings) -> RunContext:
    store = Store(settings)
    state = store.create_run(SHL)
    ctx = RunContext.create(state, store, store.vault, settings)
    for cid in ("C-001", "C-002"):
        state.items[cid] = conclusion(cid)
    ctx.emit("run.status", {"status": "running", "stage": "verify"}, stage="verify", message="Stage verify")
    return ctx


def invokes(ctx: RunContext, item_id: str) -> list[m.GatewayRequestData]:
    return [e.data for e in ctx.state.events if e.type == "gateway.request" and e.data.action != "fetch"
            and (e.agent_id or "").startswith(f"verifier-{item_id}-")]


def logs(ctx: RunContext, needle: str) -> list[m.Event]:
    return [e for e in ctx.state.events if e.type == "agent.log" and needle in e.data.text]


def rerun_says_unsupported(system: str, user: str, schema: dict) -> dict:
    """Simulated judge: SUPPORTED, except on a rerun (its prompt shows the rejected call), where it dissents."""
    if "was rejected" in user:
        return {"score_direction": None, "verdict": "UNSUPPORTED", "unsupported_spans": [],
                "reason": "The rerun's own verdict (simulated)."}
    return judge.fake_judge(system, user, schema)


async def test_no_judge_fault_when_off(settings):
    ctx = judge_ctx(settings)
    res = await verify.judge_claim(ctx, ctx.state.items["C-001"])
    assert (res.result, res.votes_for, res.votes_total) == ("pass", 3, 3)
    assert len(invokes(ctx, "C-001")) == 3 and logs(ctx, "malformed verdict") == []


async def test_one_planted_malformed_verdict_is_flagged_rerun_and_the_rerun_counts(settings, monkeypatch):
    s = demo_faults(monkeypatch, settings, "judge")
    ctx = judge_ctx(s)
    register_fake(JUDGE, rerun_says_unsupported)
    res = await verify.judge_claim(ctx, ctx.state.items["C-001"])
    assert len(invokes(ctx, "C-001")) == 4                                    # three judges plus one rerun
    flagged = logs(ctx, faults.PLANTED_VERDICT)
    assert len(flagged) == 1 and flagged[0].level == "warn"
    aid = flagged[0].agent_id
    assert flagged[0].data.text == f"Judge {aid} returned a malformed verdict (simulated, planted fault). Rerunning it."
    assert len([e for e in ctx.state.events if e.type == "gateway.request" and e.agent_id == aid]) == 2
    assert (res.result, res.votes_for, res.votes_total) == ("pass", 2, 3)     # the rerun's UNSUPPORTED counted
    assert "The rerun's own verdict (simulated)." in res.detail
    transcript = ctx.vault.get_text(ctx.run_id, f"00-Meta/agents/{aid}.jsonl")
    assert "planted_fault" in transcript and "simulated, planted fault" in transcript

    again = await verify.judge_claim(ctx, ctx.state.items["C-002"])            # one per run, never a second
    assert len(invokes(ctx, "C-002")) == 3 and (again.votes_for, again.votes_total) == (3, 3)
    assert len(logs(ctx, faults.PLANTED_VERDICT)) == 1


async def test_judge_fault_only_in_the_verify_stage(settings, monkeypatch):
    ctx = judge_ctx(demo_faults(monkeypatch, settings, "judge"))
    ctx.state.run.stage = "repair"
    await verify.judge_claim(ctx, ctx.state.items["C-001"])
    assert len(invokes(ctx, "C-001")) == 3 and logs(ctx, faults.PLANTED_VERDICT) == []


async def test_a_real_malformed_verdict_twice_is_an_abstention_never_a_pass(settings):
    ctx = judge_ctx(settings)
    register_fake(JUDGE, lambda system, user, schema: {"verdict": "MAYBE", "unsupported_spans": [],
                                                       "reason": "unsure (simulated)"})
    res = await verify.judge_claim(ctx, ctx.state.items["C-001"])
    assert len(invokes(ctx, "C-001")) == 6                                    # each judge rerun once
    assert res.result == "blocked" and res.votes_for == 0 and res.votes_total == 3
    assert "abstained (malformed verdicts twice)" in res.detail and "never counts as support" in res.detail
    first = logs(ctx, "Rerunning it.")
    second = logs(ctx, "counts as an abstention")
    assert len(first) == 3 and len(second) == 3 and all(e.level == "warn" for e in first + second)
    assert all("simulated" not in e.data.text for e in first + second)        # a real fault is not labeled


async def test_one_abstention_is_named_and_two_supports_still_pass(settings):
    ctx = judge_ctx(settings)
    item = ctx.state.items["C-001"]
    register_fake(JUDGE, lambda system, user, schema: {"unsupported_spans": [], "reason": "no verdict (simulated)"})
    abstained: list[str] = []
    vote = await verify._one_judge(ctx, "C-001", 1, "prompt (simulated)", abstained)
    assert vote is None and abstained == ["verifier-C-001-1"]
    yes = judge.JudgeVerdict(verdict="SUPPORTED", reason="ok (simulated)")
    res = verify.tally(item, [None, yes, yes], "model-small", abstained)
    assert (res.result, res.votes_for, res.votes_total) == ("pass", 2, 3)
    assert "verifier-C-001-1 abstained (malformed verdict twice)" in res.detail
    assert verify.tally(item, [yes, yes, yes], "model-small").detail == "Supported by 3 of 3 judges"


def test_malformed_verdict_shapes():
    assert judge.malformed_verdict({}) and judge.malformed_verdict({"verdict": "supported"})
    assert judge.malformed_verdict(None) and judge.malformed_verdict({"verdict": None})
    assert not judge.malformed_verdict({"verdict": "UNSUPPORTED"})
    assert "verdict" not in faults.malformed({"verdict": "SUPPORTED", "reason": "x"})


# ---------------------------------------------------------------------------
# check_plan.py: 17 sources, S-017 planted
# ---------------------------------------------------------------------------


def _check_plan():
    import importlib.util
    import sys
    path = BACKEND_DIR / "scripts" / "check_plan.py"
    spec = importlib.util.spec_from_file_location("check_plan_faults", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _plan() -> dict:
    return plan_stage.load_plan()


def test_check_plan_accepts_the_planted_s017():
    cp = _check_plan()
    plan = _plan()
    assert len(plan["sources"]) == 17
    planted = [s["source_id"] for s in plan["sources"] if cp.is_planted_fault(s)]
    assert planted == ["S-017"]
    assert cp.check_structure(plan) == []
    sources = [m.Source.model_validate(s) for s in plan["sources"]]
    issues, lines = cp.check_urls(sources, lambda url: (_ for _ in ()).throw(AssertionError(url))
                                  if url.endswith("broken.pdf") else cp.Probe(True, 200, "HEAD", "ok"))
    assert any("S-017" in ln and "simulated" in ln and ln.lstrip().startswith("skip") for ln in lines)
    assert not any(i.where == "S-017" for i in issues)


def test_check_plan_catches_a_planted_fault_that_serves_a_need_or_comes_early():
    cp = _check_plan()
    plan = _plan()
    s017 = next(s for s in plan["sources"] if s["source_id"] == "S-017")
    s017["need_ids"], s017["criteria"] = ["N-04"], ["secular_growth"]
    found = [f"{i.where}: {i.message}" for i in cp.check_structure(plan) if i.level == "error"]
    assert any(e.startswith("S-017") and "serve no need" in e for e in found)
    plan = _plan()
    s017 = next(s for s in plan["sources"] if s["source_id"] == "S-017")
    s017["tier"], s017["rank"] = "tier_2", 2
    found = [f"{i.where}: {i.message}" for i in cp.check_structure(plan) if i.level == "error"]
    assert any("planted fault S-017 must be fetched after every other source" in e for e in found)
