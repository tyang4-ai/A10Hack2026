"""FETCH_PACING and VERIFY_PACING (Sun 5 AM, the user's ask: longer fetching and verification, a full run in
2:00 to 2:30). Both pause in the gateway, end early on STOP, and never let a call through."""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.axelrod import gateway as gw


class _Ctx:
    def __init__(self, fetch=0.0, verify=0.0):
        self.stopped = False
        self.settings = type("S", (), {"fetch_pacing": fetch, "verify_pacing": verify})()

    def check_stop(self):
        if self.stopped:
            raise RuntimeError("stop")


def _gw(ctx):
    g = gw.Gateway.__new__(gw.Gateway)
    g.ctx = ctx
    return g


async def test_pace_waits_about_the_setting():
    g = _gw(_Ctx())
    t = time.monotonic()
    await g._pace(0.3)
    assert 0.28 <= time.monotonic() - t < 0.6


async def test_pace_off_returns_at_once():
    g = _gw(_Ctx())
    t = time.monotonic()
    await g._pace(0)
    await g._pace(None)
    assert time.monotonic() - t < 0.02


async def test_stop_ends_the_pause_early_without_raising():
    ctx = _Ctx()
    g = _gw(ctx)

    async def stop_soon():
        await asyncio.sleep(0.1)
        ctx.stopped = True

    t = time.monotonic()
    await asyncio.gather(g._pace(5), stop_soon())
    assert time.monotonic() - t < 0.4


def test_settings_read_the_env():
    from backend.axelrod.config import load_settings
    s = load_settings(env_file=None, environ={"FETCH_PACING": "1.5", "VERIFY_PACING": "4"})
    assert (s.fetch_pacing, s.verify_pacing) == (1.5, 4.0)
    d = load_settings(env_file=None, environ={})
    assert (d.fetch_pacing, d.verify_pacing) == (0.0, 0.0)
