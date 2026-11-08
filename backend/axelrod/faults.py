"""Planted faults for the demo, all labeled simulated (v0.3.1). Owned by B-faults.

Rehearsals only ever showed one failure, a budget block. DEMO_FAULTS plants two more, so the demo can show the
harness flagging a fault that is not about money:

- fetch: S-017, a planted source whose download (our own API's /test-pages/broken.pdf) always answers HTTP 503.
  The fetcher retries once, marks it failed and finalize declares the hole. The retry is real code for every
  source (stages/collect.py), so a flaky real site is handled the same way.
- judge: the first judge call of the verify stage gets its verdict replaced by a malformed one after the real
  model call returns. The judge flags it and reruns once (stages/verify.py). That handling is real code for any
  judge, so a real malformed answer goes the same way.

DEMO_FAULTS is a comma-separated list from FAULT_KINDS. Unset or empty plants nothing, which is what tests and
local runs get. Every planted fault says "simulated" in its log line, event message or hole reason.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .contract import m
    from .runtime import RunContext

ENV_VAR = "DEMO_FAULTS"
FAULT_KINDS = ("fetch", "judge")
LABEL = "simulated, planted fault"

# Fault 1: the broken download
BROKEN_PAGE = "broken.pdf"                        # the name under /test-pages/
BROKEN_FILE = "/data/test_pages/broken.pdf"       # how plan.json's URL for S-017 ends (never a real file)
SERVED_PATH = "/test-pages/" + BROKEN_PAGE
SIMULATED_HEADER = "X-Axelrod-Simulated"          # the same header api.py puts on every planted page
BROKEN_BODY = (b"503 Service Unavailable (simulated). Axelrod's planted broken download: this route always "
               b"fails on purpose, so the demo can show a fetch fault that is not a budget block.\n")

# Fault 2: the malformed judge verdict
PLANTED_VERDICT = f"returned a malformed verdict ({LABEL})"
JUDGE_LOG = "Judge {agent_id} " + PLANTED_VERDICT + ". Rerunning it."
JUDGE_STAGE = "verify"
_judge_fault_used: set[str] = set()               # run ids whose one planted judge fault is spent (this process)


def kinds(settings: Any = None) -> set[str]:
    """The planted faults that are on. A settings.demo_faults field wins when config.py has one, else the
    DEMO_FAULTS environment variable. Unknown names are ignored, so a typo plants nothing."""
    raw = getattr(settings, "demo_faults", None) if settings is not None else None
    if raw is None:
        raw = os.environ.get(ENV_VAR, "")
    parts = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    return {str(p).strip().lower() for p in parts} & set(FAULT_KINDS)


def enabled(settings: Any, kind: str) -> bool:
    """Is planted fault `kind` ("fetch" or "judge") on for this process?"""
    return kind in kinds(settings)


# ---------------------------------------------------------------------------
# Fault 1: fetch (S-017, a broken download)
# ---------------------------------------------------------------------------


def is_broken_download(source: m.Source) -> bool:
    """The planted broken download: its URL ends at broken.pdf (plan.json's or the served one) and its title
    says simulated. Found by URL and label, never by id, like the planted injection page."""
    path = urlsplit(source.url).path
    return "(simulated)" in source.title and (path.endswith(BROKEN_FILE) or path.endswith(SERVED_PATH))


def served_url(test_page_url: str) -> str:
    """broken.pdf next to the injection page under TEST_PAGE_URL: <base>/test-pages/broken.pdf."""
    parts = urlsplit(test_page_url.strip())
    folder = parts.path.rsplit("/", 1)[0] if "/" in parts.path else ""
    return f"{parts.scheme}://{parts.netloc}{folder}/{BROKEN_PAGE}"


def test_page_response(name: str) -> Any | None:
    """A planted fault page for /test-pages/{name}, or None for the ordinary test pages.

    broken.pdf always answers 503 with Retry-After: 1 and the simulated header, so the fetcher's retry and the
    failed source can be shown live. It is never cached (no-store, and the document cache refuses a 5xx)."""
    if name != BROKEN_PAGE:
        return None
    from fastapi import Response

    return Response(BROKEN_BODY, status_code=503, media_type="text/plain; charset=utf-8",
                    headers={"Retry-After": "1", SIMULATED_HEADER: "true", "Cache-Control": "no-store",
                             "X-Robots-Tag": "noindex"})


def fetch_error(source: m.Source, error: str) -> str:
    """The failed source's error, with the planted label when it is the planted download."""
    return f"{error} ({LABEL})" if is_broken_download(source) else error


# ---------------------------------------------------------------------------
# Fault 2: judge (one malformed verdict per run)
# ---------------------------------------------------------------------------


def claim_judge_fault(ctx: RunContext) -> bool:
    """True exactly once per run, for the first judge call of the verify stage, when DEMO_FAULTS has judge.

    Decided by code. No await between the check and the claim, so parallel judges can't both get it. The
    events are checked too, so a resumed run (a new process) never plants a second one."""
    if not enabled(ctx.settings, "judge"):
        return False
    run_id = ctx.state.run.run_id
    if run_id in _judge_fault_used or ctx.state.run.stage != JUDGE_STAGE:
        return False
    _judge_fault_used.add(run_id)
    return not any(e.type == "agent.log" and PLANTED_VERDICT in (getattr(e.data, "text", "") or "")
                   for e in ctx.state.events)


def malformed(tool_input: dict) -> dict:
    """The planted malformed verdict: the model's real answer with its verdict removed."""
    out = dict(tool_input or {})
    out.pop("verdict", None)
    return out


def judge_fault_log(agent_id: str) -> str:
    """The judge's warn line for the planted malformed verdict."""
    return JUDGE_LOG.format(agent_id=agent_id)
