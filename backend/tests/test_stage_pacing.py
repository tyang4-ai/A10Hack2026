"""STAGE_PACING (B-delete-pace): a pause at the start of every pipeline stage but approve, which STOP ends
at once. It replaces DEMO_PACING, the sleep before every fetch. Offline: the stages are tiny callables."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import time

import pytest

from backend.axelrod import gates, orchestrator
from backend.axelrod.config import BACKEND_DIR, Settings, load_settings
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext
from backend.axelrod.store import Store

PACE = 0.15


@pytest.fixture
def short_settings(settings: Settings) -> Settings:
    """DATA_DIR at backend/.data/sp<hex> (Windows MAX_PATH), removed afterwards."""
    data_dir = BACKEND_DIR / ".data" / f"sp{secrets.token_hex(3)}"
    data_dir.mkdir(parents=True)
    yield settings.model_copy(update={"data_dir": data_dir})
    shutil.rmtree(data_dir, ignore_errors=True)


def make_ctx(settings: Settings, pacing: float) -> RunContext:
    s = settings.model_copy(update={"stage_pacing": pacing})
    store = Store(s)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL", created_via="api"))
    return RunContext.create(state, store, store.vault, s)


def recorder(log: list, name: str):
    async def stage(ctx: RunContext) -> None:
        log.append((name, "start", time.monotonic()))
        log.append((name, "end", time.monotonic()))
    return stage


def test_setting_reads_stage_pacing_and_demo_pacing_is_gone():
    s = load_settings(env_file=None, environ={"STAGE_PACING": "0.5", "DEMO_PACING": "3"})
    assert s.stage_pacing == 0.5 and not hasattr(s, "demo_pacing")
    assert load_settings(env_file=None, environ={}).stage_pacing == 0.0


async def test_every_stage_but_approve_pauses(short_settings, monkeypatch):
    log: list = []
    names = ["plan", "approve", "collect", "extract", "draft"]
    monkeypatch.setattr(orchestrator, "STAGES", [(n, recorder(log, n)) for n in names])
    monkeypatch.setattr(gates, "GATE_STAGES", ())                # no Bobby gate time between the stages
    ctx = make_ctx(short_settings, PACE)
    t0 = time.monotonic()
    await orchestrator.run_pipeline(ctx)
    assert ctx.state.run.status == "done"
    start = {n: t for n, what, t in log if what == "start"}
    end = {n: t for n, what, t in log if what == "end"}
    assert start["plan"] - t0 >= PACE - 0.02
    assert start["approve"] - end["plan"] < PACE / 2              # approve waits on the analyst, not a pause
    for prev, nxt in [("approve", "collect"), ("collect", "extract"), ("extract", "draft")]:
        assert start[nxt] - end[prev] >= PACE - 0.02, (prev, nxt)
    status = [e for e in ctx.state.events if e.type == "run.status" and e.data.status == "running"]
    assert [e.data.stage for e in status] == names                 # the pause comes after the stage shows


async def test_zero_pacing_does_not_pause(short_settings, monkeypatch):
    log: list = []
    monkeypatch.setattr(orchestrator, "STAGES", [(n, recorder(log, n)) for n in ("collect", "extract", "draft")])
    monkeypatch.setattr(gates, "GATE_STAGES", ())
    ctx = make_ctx(short_settings, 0)
    t0 = time.monotonic()
    await orchestrator.run_pipeline(ctx)
    assert time.monotonic() - t0 < 0.5 and ctx.state.run.status == "done"


@pytest.mark.parametrize("how", ["stop", "kill_switch"])
async def test_stop_during_a_pause_ends_it_at_once(short_settings, monkeypatch, how):
    log: list = []
    monkeypatch.setattr(orchestrator, "STAGES", [("collect", recorder(log, "collect"))])
    ctx = make_ctx(short_settings, 30)
    task = asyncio.create_task(orchestrator.run_pipeline(ctx))
    deadline = time.monotonic() + 5
    while not any(e.type == "run.status" and e.data.stage == "collect" for e in ctx.state.events):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)                                       # well inside the 30 s pause
    t_stop = time.monotonic()
    if how == "stop":
        ctx.state.kill_switch = True                               # what POST /stop sets
        ctx.state.run.stop_reason = "Stopped by user"
    else:
        ctx.gateway.build_policy()
        ctx.state.policy.kill_switch = True
    await asyncio.wait_for(task, timeout=2)
    assert time.monotonic() - t_stop < 0.2
    assert log == []                                               # the stage never started
    assert ctx.state.run.status == "stopped" and ctx.state.events[-1].type in ("run.stopped", "agent.status")
