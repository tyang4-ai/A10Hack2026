"""B-testpage: Axelrod's own API serves the planted test page (SIMULATED), and S-010 points there when
TEST_PAGE_URL is set, because the GitHub repo stays private until submission.

Offline. The end-to-end tests start a real uvicorn on a free loopback port and fetch the page through the
real Gateway. Only the Guardrails scan is faked (simulated). Every server a test starts is stopped by it.
"""
from __future__ import annotations

import email.message
import http.client
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.axelrod import api, bedrock, orchestrator
from backend.axelrod.api import create_app
from backend.axelrod.config import BACKEND_DIR, Settings, load_settings, set_settings
from backend.axelrod.contract import m
from backend.axelrod.fetch import http as fetch_http
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import plan as plan_stage
from backend.axelrod.store import Store

P = "/api/v1"
PAGE = BACKEND_DIR / "data" / "test_pages" / "injection.html"
ROUTE = "/test-pages/injection.html"
TITLE = "Axelrod test page: planted prompt injection (simulated)"
GITHUB_URL = ("https://raw.githubusercontent.com/tyang4-ai/A10Hack2026/backend/"
              "backend/data/test_pages/injection.html")
APIGW_URL = "https://abc123def4.execute-api.us-east-1.amazonaws.com" + ROUTE
LINK = "https://example.org/axelrod-test/shl-buyer-survey"
FAKE_KEY = "test-key-not-a-real-key"
SHL = {"company": "Siemens Healthineers AG", "ticker": "SHL", "created_via": "api"}
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_check_plan():
    """scripts/check_plan.py as a module. Reuse test_plan.py's copy when it is loaded already."""
    script = BACKEND_DIR / "scripts" / "check_plan.py"
    loaded = sys.modules.get("check_plan")
    if loaded is not None and Path(getattr(loaded, "__file__", "")) == script:
        return loaded
    spec = importlib.util.spec_from_file_location("check_plan", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_plan"] = module  # dataclasses look their module up in sys.modules
    spec.loader.exec_module(module)
    return module


check_plan = _load_check_plan()


def errors(issues) -> list[str]:
    return [f"{i.where}: {i.message}" for i in issues if i.level == "error"]


def warnings(issues) -> list[str]:
    return [f"{i.where}: {i.message}" for i in issues if i.level == "warn"]


def plan_sources() -> list[m.Source]:
    return [plan_stage._fresh_source(s) for s in plan_stage.load_plan()["sources"]]


# ---------------------------------------------------------------------------
# config and .env.example
# ---------------------------------------------------------------------------


def test_settings_read_test_page_url():
    assert load_settings(env_file=None, environ={}).test_page_url is None
    assert load_settings(env_file=None, environ={"TEST_PAGE_URL": "  "}).test_page_url is None
    s = load_settings(env_file=None, environ={"TEST_PAGE_URL": f" {APIGW_URL} "})
    assert s.test_page_url == APIGW_URL


def test_env_example_has_the_key_and_no_value():
    lines = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "TEST_PAGE_URL=" in lines


# ---------------------------------------------------------------------------
# GET /test-pages/{name} (in-process client)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings.model_copy(update={"api_key": FAKE_KEY}))) as c:
        yield c


def test_route_serves_the_page_with_the_simulated_header(client):
    r = client.get(ROUTE)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["x-axelrod-simulated"] == "true"
    assert r.content == PAGE.read_bytes()
    assert f"<title>{TITLE}</title>" in r.text


def test_route_answers_head(client):
    r = client.head(ROUTE)
    assert r.status_code == 200
    assert r.headers["x-axelrod-simulated"] == "true"
    assert r.content == b""


def test_route_needs_no_key_while_the_api_does(client):
    assert client.get(ROUTE).status_code == 200                        # the fetcher sends no key
    assert client.get("/healthz").status_code == 200
    assert client.get(f"{P}/runs").status_code == 401                  # but the API itself still needs it
    assert client.get(f"{P}/runs", headers={"X-Axelrod-Key": FAKE_KEY}).status_code == 200


def test_route_is_outside_api_v1(client):
    assert client.get(f"{P}{ROUTE}").status_code in (401, 404)
    assert client.get(f"{P}{ROUTE}", headers={"X-Axelrod-Key": FAKE_KEY}).status_code == 404


@pytest.mark.parametrize("name", [
    "missing.html", "INJECTION.html", "Injection.HTML", "injection.htm", "injection.html.bak", "injection",
    ".html", "plan.json", "inj ection.html", "inj.ection.html", "~injection.html", "..", "...html",
    "..%5Cshl%5Cplan.json", "..%5C..%5Caxelrod%5Cconfig.py", "injection.html%00", "injection.html%0A",
    "%2e%2e", "c%3A%5Cwindows%5Cwin.ini",
])
def test_route_404s_anything_but_a_plain_existing_name(client, name):
    r = client.get(f"/test-pages/{name}")
    assert r.status_code == 404, (name, r.status_code)
    assert m.ErrorResponse.model_validate(r.json()).error.code == "not_found"
    assert "X-Axelrod-Simulated" not in r.headers


def test_route_serves_only_files_in_its_folder(tmp_path, monkeypatch, client):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "other.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (pages / "folder.html").mkdir()
    (tmp_path / "outside.html").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(api, "TEST_PAGES_DIR", pages)
    assert client.get("/test-pages/other.html").status_code == 200
    assert client.get("/test-pages/folder.html").status_code == 404
    assert client.get("/test-pages/outside.html").status_code == 404
    assert client.get("/test-pages/injection.html").status_code == 404


# ---------------------------------------------------------------------------
# Plan stage: the S-010 URL override
# ---------------------------------------------------------------------------


def test_override_is_off_when_unset():
    sources = plan_sources()
    before = [s.model_copy(deep=True) for s in sources]
    assert plan_stage.serve_test_page(sources, None) is None
    assert plan_stage.serve_test_page(sources, "  ") is None
    assert sources == before
    assert [s.url for s in sources if s.source_id == "S-010"] == [GITHUB_URL]


def test_override_points_s010_at_the_api():
    sources = plan_sources()
    before = {s.source_id: s.model_copy(deep=True) for s in sources}
    note = plan_stage.serve_test_page(sources, APIGW_URL)
    s010 = next(s for s in sources if s.source_id == "S-010")
    assert s010.url == APIGW_URL
    assert s010.domain == "abc123def4.execute-api.us-east-1.amazonaws.com"
    assert s010.rationale.startswith(before["S-010"].rationale)
    assert "SIMULATED" in s010.rationale and "Axelrod's own API" in s010.rationale and APIGW_URL in s010.rationale
    assert s010.title == TITLE and s010.tier == "tier_3" and s010.approval == "pending"
    assert note is not None and "S-010" in note and "simulated" in note and "Axelrod's own API" in note
    assert all(s == before[s.source_id] for s in sources if s.source_id != "S-010")
    m.Source.model_validate(s010.model_dump())


@pytest.mark.parametrize("bad", ["ftp://host/test-pages/injection.html", "not a url", "/test-pages/injection.html"])
def test_override_rejects_a_url_the_gateway_would_never_allow(bad):
    with pytest.raises(ValueError, match="TEST_PAGE_URL"):
        plan_stage.serve_test_page(plan_sources(), bad)


def test_override_leaves_a_plan_without_the_test_page_alone():
    sources = [s for s in plan_sources() if s.source_id != "S-010"]
    before = [s.model_copy(deep=True) for s in sources]
    note = plan_stage.serve_test_page(sources, APIGW_URL)
    assert note is not None and "no planted test page" in note
    assert sources == before


async def _plan_run(settings: Settings, monkeypatch) -> tuple[Store, RunContext]:
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company="Siemens Healthineers AG", ticker="SHL"))
    ctx = RunContext.create(state, store, store.vault, settings)
    await plan_stage.run(ctx)
    return store, ctx


async def test_plan_stage_emits_s010_with_the_served_url(settings, monkeypatch):
    store, ctx = await _plan_run(settings.model_copy(update={"test_page_url": APIGW_URL}), monkeypatch)
    emitted = [e.data.source for e in ctx.state.events if e.type == "plan.source"]
    s010 = next(s for s in emitted if s.source_id == "S-010")
    assert s010.url == APIGW_URL and "Axelrod's own API" in s010.rationale
    assert ctx.state.sources_by_id()["S-010"].url == APIGW_URL
    notes = ctx.state.run.notes or ""
    assert "Frozen demo plan" in notes and "S-010" in notes and "simulated" in notes and APIGW_URL in notes
    manifest = json.loads(store.vault.get_text(ctx.run_id, "00-Meta/_url-manifest.json"))
    assert manifest["S-010"] == APIGW_URL
    assert manifest["S-001"].startswith("https://corporate.webassets.siemens-healthineers.com/")


async def test_plan_stage_keeps_the_github_url_when_unset(settings, monkeypatch):
    _, ctx = await _plan_run(settings, monkeypatch)
    assert ctx.state.sources_by_id()["S-010"].url == GITHUB_URL
    assert "Axelrod's own API" not in (ctx.state.run.notes or "")


async def test_allow_rule_opens_only_the_page_on_the_api_host(settings, monkeypatch):
    """R-allow-S-010's path prefix is the page path, so approving S-010 never opens the rest of the API."""
    _, ctx = await _plan_run(settings.model_copy(update={"test_page_url": APIGW_URL}), monkeypatch)
    for s in ctx.state.sources:
        s.approval = "approved" if s.source_id == "S-010" else "rejected"
    policy = ctx.gateway.build_policy()
    rule = next(r for r in policy.rules if r.rule_id == "R-allow-S-010")
    assert (rule.domain, rule.path_prefix, rule.roles) == (
        "abc123def4.execute-api.us-east-1.amazonaws.com", ROUTE, ["fetcher"])
    fetcher = m.Agent(agent_id="fetcher-01", role="fetcher", status="working", task="t", target_ids=[],
                      started_at="2026-11-07T20:00:00.000Z")
    host = "https://abc123def4.execute-api.us-east-1.amazonaws.com"
    assert ctx.gateway.decide(fetcher, "fetch", APIGW_URL)[:2] == ("allowed", "R-allow-S-010")
    for other in (f"{host}/api/v1/runs", f"{host}/healthz", f"{host}/test-pages/other.html"):
        assert ctx.gateway.decide(fetcher, "fetch", other)[0] == "blocked", other


# ---------------------------------------------------------------------------
# check_plan.py with TEST_PAGE_URL
# ---------------------------------------------------------------------------


def test_override_url_shape_checks():
    assert check_plan.check_test_page_override(APIGW_URL) == []
    assert check_plan.check_test_page_override(f"http://127.0.0.1:8123{ROUTE}") == []
    assert any("path must end with" in e for e in errors(check_plan.check_test_page_override(
        "https://abc.execute-api.us-east-1.amazonaws.com/injection.html")))
    assert errors(check_plan.check_test_page_override("ftp://x/test-pages/injection.html"))
    assert any("query" in e for e in errors(check_plan.check_test_page_override(APIGW_URL + "?x=1")))
    plain = check_plan.check_test_page_override("http://ec2-1-2-3-4.compute-1.amazonaws.com:8000" + ROUTE)
    assert not errors(plain) and any("plain http" in w for w in warnings(plain))


def test_check_urls_probes_the_served_page_instead_of_github():
    sources = [m.Source.model_validate(s) for s in check_plan.load_plan()["sources"]]
    probed: list[str] = []

    def probe(url):
        probed.append(url)
        return check_plan.Probe(True, 200, "HEAD", "text/html")

    issues, lines = check_plan.check_urls(sources, probe, APIGW_URL)
    assert issues == [] and GITHUB_URL not in probed and APIGW_URL in probed
    assert any(APIGW_URL in line and "S-010" in line for line in lines)

    def down(url):
        return check_plan.Probe(url != APIGW_URL, 200 if url != APIGW_URL else None, "GET", "refused")

    issues, _ = check_plan.check_urls(sources, down, APIGW_URL)
    assert [(i.level, i.where) for i in issues] == [("error", "S-010")]
    assert "TEST_PAGE_URL" in issues[0].message


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        self.status = status
        self.headers = email.message.Message()
        for k, v in headers.items():
            self.headers[k] = v
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(resp: _Resp | Exception) -> Callable:
    def open_(req, timeout):
        if isinstance(resp, Exception):
            raise resp
        return resp
    return open_


def test_check_served_page():
    good = {"Content-Type": "text/html; charset=utf-8", "X-Axelrod-Simulated": "true"}
    body = PAGE.read_bytes()
    issues, line = check_plan.check_served_page(APIGW_URL, "UA/1", opener=_opener(_Resp(body, good)))
    assert issues == [] and line.strip().startswith("ok")
    no_header = {"Content-Type": "text/html; charset=utf-8"}
    issues, _ = check_plan.check_served_page(APIGW_URL, "UA/1", opener=_opener(_Resp(body, no_header)))
    assert any("X-Axelrod-Simulated" in e for e in errors(issues))
    issues, _ = check_plan.check_served_page(APIGW_URL, "UA/1", opener=_opener(_Resp(body + b"\n", good)))
    assert not errors(issues) and any("another commit" in w for w in warnings(issues))
    gone = urllib.error.HTTPError(APIGW_URL, 404, "nope", email.message.Message(), None)
    issues, line = check_plan.check_served_page(APIGW_URL, "UA/1", opener=_opener(gone))
    assert any("HTTP 404" in e for e in errors(issues)) and "FAIL" in line
    issues, _ = check_plan.check_served_page(APIGW_URL, "UA/1", opener=_opener(ConnectionRefusedError("no")))
    assert any("Is the Axelrod API running" in e for e in errors(issues))


def test_check_plan_main_offline_with_test_page_url(settings, capsys):
    set_settings(settings.model_copy(update={"test_page_url": APIGW_URL}))
    assert check_plan.main(["--offline"]) == 0
    out = capsys.readouterr().out
    assert "TEST_PAGE_URL is set" in out and APIGW_URL in out and "PASS" in out
    set_settings(settings.model_copy(update={"test_page_url": "ftp://nope/x"}))
    assert check_plan.main(["--offline"]) == 1
    assert "FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# A real uvicorn (loopback, free port)
# ---------------------------------------------------------------------------


@contextmanager
def live_api(make_settings: Callable[[str], Settings]) -> Iterator[tuple[str, FastAPI]]:
    """create_app under a real uvicorn on a socket bound before start, so the URL is known up front."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(make_settings(base))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        yield base, app
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive(), "uvicorn did not stop"


def call(method: str, url: str, body: dict | None = None, key: str | None = None):
    """(status, headers, body) from our own local server, no proxies."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if key:
        headers["X-Axelrod-Key"] = key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with NO_PROXY.open(req, timeout=15) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def raw_get(base: str, path: str, method: str = "GET") -> tuple[int, str | None]:
    """Send the path exactly as written (no client-side normalizing), like `curl --path-as-is`."""
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        resp.read()
        return resp.status, resp.getheader("X-Axelrod-Simulated")
    finally:
        conn.close()


def test_real_server_serves_the_page_and_404s_traversal(settings):
    with live_api(lambda base: settings.model_copy(update={"api_key": FAKE_KEY})) as (base, _):
        status, headers, body = call("GET", base + ROUTE)
        assert status == 200 and body == PAGE.read_bytes()
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["X-Axelrod-Simulated"] == "true"
        assert raw_get(base, ROUTE, "HEAD") == (200, "true")
        for path in ("/test-pages/../shl/plan.json", "/test-pages/..%2Fshl%2Fplan.json",
                     "/test-pages/%2e%2e%2f%2e%2e%2faxelrod%2fconfig.py", "/test-pages/..%5C..%5C.env.example",
                     "/test-pages/..\\..\\.env.example", "/test-pages/injection.html%00", "/test-pages/..",
                     "/test-pages/%2E%2E", "/test-pages//etc/passwd", "/test-pages/INJECTION.HTML"):
            assert raw_get(base, path) == (404, None), path
        assert call("GET", f"{base}{P}/runs")[0] == 401


def test_full_path_fetches_flags_quarantines_and_blocks_the_link(settings, monkeypatch):
    """POST /runs on a real uvicorn whose TEST_PAGE_URL is its own /test-pages route. Approve only S-010.
    The real Gateway fetches it (R-allow-S-010, no key sent), the fake scan (simulated) flags it, the
    document is quarantined and the example.org link is blocked by default deny."""
    monkeypatch.setattr(plan_stage, "PACE_S", 0)
    monkeypatch.setattr(orchestrator, "STAGES", [("plan", "plan"), ("approve", "approve"), ("collect", "collect")])
    real_get_once = fetch_http.get_once
    sent: list[str] = []

    async def recording_get_once(url: str, **kw):
        sent.append(url)
        return await real_get_once(url, **kw)

    monkeypatch.setattr(fetch_http, "get_once", recording_get_once)
    bedrock.register_fake_scan(lambda text: "ignore all previous instructions" in text.lower())
    try:
        with live_api(lambda base: settings.model_copy(
                update={"api_key": FAKE_KEY, "test_page_url": base + ROUTE})) as (base, app):
            page_url = base + ROUTE
            run_id, state = _run_to_done(base)
            served = app.state.rt.store.get(run_id)
            documents = served.documents
            agent_pages = served.agent_pages(documents[0].doc_id) if documents else None
            raw = app.state.rt.vault.get_bytes(run_id, documents[0].vault_paths.raw) if documents else b""
    finally:
        bedrock.register_fake_scan(None)

    plan = state["plan"]
    s010 = next(s for s in plan.sources if s.source_id == "S-010")
    assert s010.url == page_url and s010.domain == "127.0.0.1"
    assert "SIMULATED" in s010.rationale and "Axelrod's own API" in s010.rationale
    assert s010.status == "quarantined"
    assert all(s.status == "skipped" for s in plan.sources if s.source_id != "S-010")
    assert "S-010" in (state["run"].notes or "") and page_url in state["run"].notes

    rule = next(r for r in state["policy"].rules if r.rule_id == "R-allow-S-010")
    assert (rule.domain, rule.path_prefix) == ("127.0.0.1", ROUTE)

    assert sent == [page_url]                      # nothing else left the gateway: example.org was never sent
    reqs = [e.data for e in state["events"] if e.type == "gateway.request"]
    page_reqs = [r for r in reqs if r.target == page_url]
    assert [(r.decision, r.rule_id) for r in page_reqs] == [("allowed", "R-allow-S-010"), ("flagged", "R-allow-S-010")]
    assert page_reqs[0].http_status == 200 and page_reqs[0].source_id == "S-010" and page_reqs[0].role == "fetcher"
    link_reqs = [r for r in reqs if r.target == LINK]
    assert len(link_reqs) == 1
    assert (link_reqs[0].decision, link_reqs[0].rule_id) == ("blocked", None)
    assert link_reqs[0].reason.startswith("link found in page") and "Default deny" in link_reqs[0].reason
    assert len([r for r in reqs if r.action == "fetch"]) == 3
    # the rest are Bobby's gate calls (V3-rework): allowed model calls by the orchestrator, never a fetch
    assert all(r.role == "orchestrator" and r.decision == "allowed" for r in reqs if r.action != "fetch")

    assert len(documents) == 1
    doc = documents[0]
    assert doc.source_id == "S-010" and doc.url == page_url and doc.content_type == "html"
    assert doc.quarantined and doc.scan.verdict == "flagged"
    assert agent_pages == []                       # no agent ever reads it
    assert raw == PAGE.read_bytes()


def _run_to_done(base: str, timeout: float = 60.0) -> tuple[str, dict]:
    """Create the SHL run, approve S-010 only (everything else rejected, so nothing else is fetched), wait."""
    status, _, body = call("POST", f"{base}{P}/runs", SHL, key=FAKE_KEY)
    assert status == 201, body
    run_id = json.loads(body)["run_id"]
    _wait(base, run_id, lambda r: r.status == "awaiting_approval", timeout)
    _, _, body = call("GET", f"{base}{P}/runs/{run_id}/plan", key=FAKE_KEY)
    plan = m.Plan.model_validate_json(body)
    decisions = [{"source_id": s.source_id, "approval": "approved" if s.source_id == "S-010" else "rejected"}
                 for s in plan.sources]
    status, _, body = call("POST", f"{base}{P}/runs/{run_id}/approval", {"decisions": decisions}, key=FAKE_KEY)
    assert status == 200, body
    run = _wait(base, run_id, lambda r: r.status in ("done", "failed", "stopped"), timeout)
    assert run.status == "done", run.error

    def get(path: str) -> bytes:
        code, _, data = call("GET", f"{base}{P}/runs/{run_id}{path}", key=FAKE_KEY)
        assert code == 200, data
        return data

    events = m.EventsPage.model_validate_json(get("/events?limit=2000")).events
    return run_id, {"run": run, "plan": m.Plan.model_validate_json(get("/plan")),
                    "policy": m.Policy.model_validate_json(get("/policy")), "events": events}


def _wait(base: str, run_id: str, pred: Callable[[m.Run], bool], timeout: float) -> m.Run:
    deadline = time.monotonic() + timeout
    run = None
    while time.monotonic() < deadline:
        status, _, body = call("GET", f"{base}{P}/runs/{run_id}", key=FAKE_KEY)
        assert status == 200, body
        run = m.Run.model_validate_json(body)
        if pred(run):
            return run
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting, last run: {run}")
