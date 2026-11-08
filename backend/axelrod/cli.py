"""The axelrod CLI (A10's "launch a CLI agent"): run, tail, approve, revoke, stop, status, runs and the
inject-errors demo control, all against the Axelrod API. It is a thin client: every action is one API call.

Run it from the repo root as `python -m backend.axelrod.cli <command>` (alias it to `axelrod` for the demo).
`--api` defaults to the AXELROD_API environment variable, then http://localhost:8000/api/v1. The API root or the
API Gateway invoke URL both work: /api/v1 is added when missing. When AXELROD_API_KEY is set (environment or
backend/.env), it is sent as X-Axelrod-Key and never printed.

The CLI only talks to Axelrod's own API (the control plane), never to sources or models, so it uses the
standard library's urllib. httpx stays inside the gateway's fetch module, where agent traffic is policed.
typer and rich are imported inside functions, never at import time.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Optional

DEFAULT_API = "http://localhost:8000/api/v1"
API_ENV = "AXELROD_API"
KEY_ENV = "AXELROD_API_KEY"
KEY_HEADER = "X-Axelrod-Key"
TERMINAL = ("done", "stopped", "failed")
PAGE_LIMIT = 500
DECISION_STYLE = {"allowed": "green", "blocked": "red", "flagged": "yellow"}
STATUS_STYLE = {"running": "cyan", "awaiting_approval": "magenta", "done": "green", "stopped": "red",
                "failed": "red"}
APPROVAL_STYLE = {"approved": "green", "rejected": "red", "auto_approved": "yellow", "pending": "dim"}


# ---------------------------------------------------------------------------
# HTTP client for the Axelrod API
# ---------------------------------------------------------------------------


class CliError(Exception):
    """An API error envelope, or status 0 when the API could not be reached."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}

    def describe(self) -> str:
        head = f"{self.status} {self.code}: {self.message}" if self.status else self.message
        return head + (f" {json.dumps(self.details)}" if self.details else "")


def api_base(url: str | None = None) -> str:
    """The API base ending in /api/v1, from --api, then AXELROD_API, then localhost."""
    base = (url or os.environ.get(API_ENV) or DEFAULT_API).strip().rstrip("/")
    return base if base.endswith("/api/v1") else base + "/api/v1"


def _settings() -> Any:
    """backend/.env through config.py, for PUBLIC_UI_URL and the key. None if it can't be read."""
    try:
        from .config import get_settings

        return get_settings()
    except Exception:
        return None


def api_key() -> str | None:
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    s = _settings()
    return getattr(s, "api_key", None) if s is not None else None


def ui_link(run_id: str, ui_url: str | None = None) -> str | None:
    """PUBLIC_UI_URL plus the run: '{run_id}' in the URL is replaced, otherwise ?run=<id> is added."""
    if ui_url is None:
        s = _settings()
        ui_url = getattr(s, "public_ui_url", None) if s is not None else None
    if not ui_url:
        return None
    if "{run_id}" in ui_url:
        return ui_url.replace("{run_id}", run_id)
    return f"{ui_url}{'&' if '?' in ui_url else '?'}run={urllib.parse.quote(run_id)}"


def _error_from(status: int, raw: bytes) -> CliError:
    try:
        err = json.loads(raw.decode("utf-8"))["error"]
        return CliError(status, err.get("code", "error"), err.get("message", ""), err.get("details") or {})
    except (ValueError, KeyError, TypeError, AttributeError):
        return CliError(status, "http_error", raw.decode("utf-8", errors="replace")[:300] or f"HTTP {status}")


class Client:
    """JSON over HTTP to the API. Raises CliError with the API's error envelope."""

    def __init__(self, base: str, key: str | None = None, timeout: float = 30.0) -> None:
        self.base, self.key, self.timeout = base.rstrip("/"), key, timeout

    def request(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "axelrod-cli/0.1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.key:
            headers[KEY_HEADER] = self.key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise _error_from(e.code, e.read()) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise CliError(0, "unreachable", f"Cannot reach the Axelrod API at {self.base} ({reason})") from None
        return json.loads(raw.decode("utf-8")) if raw else None

    def get(self, path: str, query: dict | None = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, body=body if body is not None else {})


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


# ---------------------------------------------------------------------------
# Rendering: plain (text, style) segments, so it is testable without a terminal
# ---------------------------------------------------------------------------


@dataclass
class Line:
    segments: list[tuple[str, str]]
    rule: bool = False                      # a full-width stage divider

    def plain(self) -> str:
        return "".join(t for t, _ in self.segments)


@dataclass
class TailState:
    show_all: bool = False
    cost: float = 0.0
    budget: float | None = None
    status: str | None = None
    stage: str | None = None
    counts: dict[str, int] = field(default_factory=lambda: {"allowed": 0, "blocked": 0, "flagged": 0})


def _clock(e: dict) -> str:
    ts = e.get("ts") or ""
    return ts[11:19] if len(ts) >= 19 else ts


def _trim(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _short_url(url: str, n: int = 72) -> str:
    """Keep the host and the end of the path, which is where URLs differ."""
    if len(url) <= n:
        return url
    keep = n - 3
    return url[: keep // 2] + "..." + url[-(keep - keep // 2):]


def _size(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1000:.1f} KB" if n >= 1000 else f"{n} B"


def _money(x: float | None) -> str:
    return "-" if x is None else f"${x:.4f}"


def _head(e: dict, label: str, style: str) -> list[tuple[str, str]]:
    return [(_clock(e), "dim"), ("  ", ""), (f"{label:<8}", f"bold {style}".strip()), (" ", "")]


def render_gateway(e: dict, st: TailState) -> Line:
    """One traffic line: allowed green, blocked red, flagged yellow. Model calls carry tokens and the running cost."""
    d = e["data"]
    dec = d["decision"]
    style = DECISION_STYLE.get(dec, "")
    st.counts[dec] = st.counts.get(dec, 0) + 1
    who = e.get("agent_id") or d.get("role") or "-"
    seg = _head(e, dec.upper(), style)
    if d["action"] == "model.invoke":
        seg += [("model ", ""), (f"{who:<18}", "cyan"), (" ", ""), (d["target"], style)]
        if d.get("tokens_in") is not None:
            seg.append((f"  {d['tokens_in']:,} in / {d.get('tokens_out') or 0:,} out", ""))
        if d.get("cost_usd") is not None:
            st.cost += d["cost_usd"]
            seg.append((f"  {_money(d['cost_usd'])}", ""))
        if dec != "allowed":
            seg.append((f"  {_trim(d.get('reason'), 120)}", style))
        seg.append((f"  total {_money(st.cost)}", "bold"))
    else:
        seg += [("fetch ", ""), (f"{who:<18}", "cyan"), (" ", ""), (f"{d.get('source_id') or '-':<6}", "magenta"),
                (" ", ""), (_short_url(d["target"]), style)]
        if dec == "allowed" and d.get("http_status") is not None:
            seg.append((f"  HTTP {d['http_status']} {_size(d.get('bytes'))}".rstrip(), "dim"))
        else:
            seg.append((f"  {_trim(d.get('reason'), 140)}", style))
    if d.get("rule_id"):
        seg.append((f"  ({d['rule_id']})", "dim"))
    return Line(seg)


def _stage_line(e: dict, st: TailState) -> Line | None:
    d = e["data"]
    status, stage = d["status"], d["stage"]
    if (status, stage) == (st.status, st.stage):
        return None
    st.status, st.stage = status, stage
    style = STATUS_STYLE.get(status, "cyan")
    seg = [(f" {_clock(e)}  stage {stage} ", f"bold {style}"), (f"| {status} ", style),
           (f"| cost {_money(st.cost)} ", "dim")]
    if status == "awaiting_approval":
        seg.append(("| approve in the UI or with the approve command ", style))
    return Line(seg, rule=True)


def _verified_line(e: dict, st: TailState) -> Line | None:
    d = e["data"]
    status = d["verification"]["status"]
    style = {"fail": "red", "blocked": "yellow", "fixed": "green"}.get(status)
    demo = e.get("message", "").startswith("DEMO")
    if style is None and not demo and not st.show_all:
        return None
    label = "CHECK" if status == "pass" else status.upper()
    return Line(_head(e, label, style or "dim") + [(f"{d['item_id']}  ", "bold"), (_trim(e.get("message"), 160), style or "")])


def _updated_line(e: dict, st: TailState) -> Line | None:
    reason = e["data"]["reason"]
    style = {"demo_injection": "yellow", "repair": "green", "user_edit": "cyan"}.get(reason)
    if style is None and not st.show_all:
        return None
    label = {"demo_injection": "DEMO", "repair": "REPAIR", "user_edit": "EDIT"}.get(reason, "ITEM")
    return Line(_head(e, label, style or "dim") + [(_trim(e.get("message"), 170), style or "")])


def _source_line(e: dict, st: TailState) -> Line | None:
    d = e["data"]
    style = {"revoked": "red", "blocked": "red", "failed": "red", "quarantined": "yellow"}.get(d["status"])
    if style is None and not st.show_all:
        return None
    msg = f"{d['source_id']} {d['status']}" + (f": {_trim(d.get('error'), 140)}" if d.get("error") else "")
    return Line(_head(e, "SOURCE", style or "dim") + [(msg, style or "")])


def _doc_line(e: dict, st: TailState) -> Line | None:
    d = e["data"]
    scan = d.get("scan") or {}
    if scan.get("verdict") in ("flagged", "error"):
        msg = f"{d['doc_id']} from {d['source_id']} quarantined, scan {scan['verdict']}: {_trim(scan.get('detail'), 140)}"
        return Line(_head(e, "QUARANT", "yellow") + [(msg, "yellow")])
    if not st.show_all:
        return None
    return Line(_head(e, "DOC", "dim") + [(f"{d['doc_id']} from {d['source_id']}, {d['pages']} pages", "")])


def _simple(label: str, style: str, text_of) -> Any:
    def render(e: dict, st: TailState) -> Line:
        return Line(_head(e, label, style) + [(text_of(e), style)])
    return render


def _approval_text(e: dict) -> str:
    d = e["data"]
    parts = [f"approved {', '.join(d['approved']) or 'none'}"]
    if d["rejected"]:
        parts.append(f"rejected {', '.join(d['rejected'])}")
    if d["auto_approved"]:
        parts.append(f"auto-approved {', '.join(d['auto_approved'])}")
    return "; ".join(parts)


def _usage(e: dict, st: TailState) -> Line | None:
    u = e["data"]["usage"]
    st.cost = float(u.get("cost_usd") or 0.0)
    st.budget = u.get("budget_usd", st.budget)
    if not st.show_all:
        return None
    of = f" of {_money(st.budget)}" if st.budget is not None else ""
    return Line(_head(e, "COST", "dim") + [(f"{u.get('model_calls', 0)} calls, {_money(st.cost)}{of}", "dim")])


_RENDER = {
    "gateway.request": render_gateway,
    "run.status": _stage_line,
    "item.verified": _verified_line,
    "item.updated": _updated_line,
    "source.status": _source_line,
    "doc.stored": _doc_line,
    "usage.updated": _usage,
    "run.created": _simple("RUN", "bold", lambda e: e.get("message", "")),
    "run.stopped": _simple("STOPPED", "red", lambda e: f"kill switch: {e['data'].get('reason') or 'stopped'}"),
    "run.failed": _simple("FAILED", "red", lambda e: _trim(e["data"].get("error"), 200)),
    "run.completed": _simple("DONE", "green", lambda e: e.get("message", "")),
    "policy.updated": _simple("POLICY", "magenta",
                              lambda e: f"v{e['data']['version']}: {_trim(e['data']['change'], 140)}"),
    "approval.committed": _simple("APPROVAL", "magenta", _approval_text),
    "plan.ready": _simple("PLAN", "magenta",
                          lambda e: f"ready: {e['data']['needs']} needs, {e['data']['sources']} sources"),
    "report.ready": _simple("REPORT", "green", lambda e: (
        f"ready: {e['data']['items']} items, {e['data']['passed']} passed, {e['data']['fixed']} fixed, "
        f"{e['data']['failed']} failed, {e['data']['holes']} holes")),
    "summary.ready": _simple("BRIEF", "green",
                             lambda e: f"ready: {e['data']['sentences']} sentences, {e['data']['words']} words"),
}


def render_event(e: dict, st: TailState) -> Line | None:
    """The line to print for one event, or None to keep the demo view uncluttered (--all shows everything)."""
    fn = _RENDER.get(e.get("type", ""))
    if fn is not None:
        return fn(e, st)
    if not st.show_all:
        return None
    return Line([(_clock(e), "dim"), ("  ", ""), (f"{e.get('type', '?'):<17}", "dim"),
                 (_trim(e.get("message"), 170), "dim")])


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def make_console(**kw: Any) -> Any:
    from rich.console import Console

    return Console(highlight=False, **kw)


def _text(segments: list[tuple[str, str]]) -> Any:
    from rich.text import Text

    t = Text()
    for s, style in segments:
        t.append(s, style=style or None)
    return t


def say(console: Any, *segments: tuple[str, str]) -> None:
    console.print(_text(list(segments)), soft_wrap=True)


def print_line(console: Any, line: Line) -> None:
    if line.rule:
        console.rule(_text(line.segments), style="dim")
    else:
        console.print(_text(line.segments), soft_wrap=True)


def prog() -> str:
    """How the user started us, for the hints we print."""
    name = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "axelrod"
    return "python -m backend.axelrod.cli" if name in ("cli", "__main__", "") else name


def run_header(console: Any, run: dict) -> None:
    ticker = f" ({run['ticker']})" if run.get("ticker") else ""
    say(console, ("Run ", ""), (run["run_id"], "bold"), (f"  {run['company']}{ticker}", ""),
        (f"  {run['status']} at {run['stage']}", STATUS_STYLE.get(run["status"], "")))
    if run.get("mode") and run["mode"] != "live":
        say(console, (f"{run['mode'].upper()}: a recorded run played back, not live traffic", "bold yellow"))


def tail_run(client: Client, run_id: str, console: Any, *, after: int = 0, interval: float = 1.0,
             once: bool = False, show_all: bool = False) -> int:
    """Poll the event log and print it until the run is finished (or once, with --once). Ctrl-C only stops
    the tail: the run keeps going, and STOP is its own command."""
    run = client.get(f"/runs/{_q(run_id)}")
    run_header(console, run)
    st = TailState(show_all=show_all, budget=run["usage"].get("budget_usd"))
    seq, warned = after, False
    try:
        while True:
            try:
                page = client.get(f"/runs/{_q(run_id)}/events", {"after": seq, "limit": PAGE_LIMIT})
            except CliError as e:
                if once or e.status != 0:
                    raise
                if not warned:
                    say(console, (f"{e.message}. Retrying...", "yellow"))
                    warned = True
                time.sleep(max(interval, 1.0))
                continue
            warned = False
            for ev in page["events"]:
                line = render_event(ev, st)
                if line is not None:
                    print_line(console, line)
            seq = page["last_seq"]
            if len(page["events"]) >= PAGE_LIMIT:
                continue
            if once or page["run_status"] in TERMINAL:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        say(console, ("Stopped tailing. The run keeps going: use the stop command for the kill switch.", "yellow"))
        return 0
    summary(console, client.get(f"/runs/{_q(run_id)}"))
    return 0


def summary(console: Any, run: dict) -> None:
    c, u = run["counts"], run["usage"]
    budget = f" of {_money(u.get('budget_usd'))}" if u.get("budget_usd") is not None else ""
    say(console, (f"{run['run_id']} ", "bold"), (f"{run['status']} at {run['stage']}  ", STATUS_STYLE.get(run["status"], "")),
        (f"allowed {c['gateway_allowed']} ", "green"), (f"blocked {c['gateway_blocked']} ", "red"),
        (f"flagged {c['gateway_flagged']}  ", "yellow"),
        (f"{u['model_calls']} model calls, {_money(u['cost_usd'])}{budget}", "bold"))


def wait_for_approval(client: Client, run_id: str, timeout: float, poll: float = 0.5) -> dict:
    """Planning streams in first. Wait until the run awaits approval (or ends, or moves past approve, or the
    timeout passes). A run briefly shows running at approve before it asks for approval."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        run = client.get(f"/runs/{_q(run_id)}")
        early = run["status"] == "running" and run["stage"] in ("plan", "approve")
        if not early or time.monotonic() >= deadline:
            return run
        time.sleep(poll)


def decisions_for(sources: list[dict], *, approve_all: bool, reject: list[str]) -> list[dict]:
    """Rejected ids are rejected. Priority sources (every source with --all) are approved. The rest is left
    out, so the API auto-approves it."""
    out = []
    for s in sources:
        if s["source_id"] in reject:
            out.append({"source_id": s["source_id"], "approval": "rejected"})
        elif approve_all or s["tier"] == "priority":
            out.append({"source_id": s["source_id"], "approval": "approved"})
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@contextmanager
def handled(console: Any) -> Iterator[None]:
    """API errors become one red line and exit code 1 (2 when the API is unreachable)."""
    import typer

    try:
        yield
    except CliError as e:
        say(console, ("error ", "bold red"), (e.describe(), "red"))
        raise typer.Exit(2 if e.status == 0 else 1) from None


def build_app() -> Any:
    import typer

    app = typer.Typer(name="axelrod", add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False,
                      help="Axelrod CLI: launch a research run and watch its gateway traffic live.")

    def api_opt() -> Any:
        return typer.Option(None, "--api", envvar=API_ENV,
                            help=f"API base URL (default {DEFAULT_API}). /api/v1 is added when missing.")

    def client(api: str | None) -> Client:
        return Client(api_base(api), api_key())

    @app.command("run")
    def run_cmd(company: str = typer.Argument(..., help="Company to screen, e.g. 'Siemens Healthineers'"),
                ticker: Optional[str] = typer.Option(None, "--ticker", help="Ticker, e.g. SHL"),
                depth: str = typer.Option("screen", "--depth", help="screen or full"),
                notes: Optional[str] = typer.Option(None, "--notes", help="Analyst notes for the run"),
                budget: Optional[float] = typer.Option(None, "--budget", help="Model spend cap for this run, USD"),
                tail: bool = typer.Option(False, "--tail", help="Follow the run's traffic right away"),
                api: Optional[str] = api_opt()) -> None:
        """Create a run (created_via cli) and print its id and UI link."""
        console = make_console()
        if depth not in ("screen", "full"):
            say(console, ("error ", "bold red"), ("--depth is screen or full", "red"))
            raise typer.Exit(2)
        c = client(api)
        body = {"company": company, "ticker": ticker, "depth": depth, "notes": notes, "budget_usd": budget,
                "created_via": "cli"}
        with handled(console):
            run = c.post("/runs", body)
        ticker_txt = f" ({run['ticker']})" if run.get("ticker") else ""
        say(console, ("Run ", ""), (run["run_id"], "bold green"),
            (f" created for {run['company']}{ticker_txt}, depth {run['depth']}, via {run['created_via']}.", ""))
        link = ui_link(run["run_id"])
        if link:
            say(console, ("UI    ", "dim"), (link, "underline"))
        say(console, ("Next  ", "dim"), (f"{prog()} tail {run['run_id']}", "cyan"),
            ("   then approve in the UI, or ", "dim"), (f"{prog()} approve {run['run_id']} --all", "cyan"))
        if tail:
            with handled(console):
                tail_run(c, run["run_id"], console)

    @app.command("tail")
    def tail_cmd(run_id: str = typer.Argument(..., help="Run id"),
                 after: int = typer.Option(0, "--after", help="Start after this event seq (0 = from the start)"),
                 interval: float = typer.Option(1.0, "--interval", help="Seconds between polls"),
                 once: bool = typer.Option(False, "--once", help="Print what is there now, then exit"),
                 show_all: bool = typer.Option(False, "--all", help="Every event, not only traffic and milestones"),
                 api: Optional[str] = api_opt()) -> None:
        """Print the run's gateway traffic live: allowed green, blocked red, flagged yellow, plus stages and cost."""
        console = make_console()
        with handled(console):
            tail_run(client(api), run_id, console, after=after, interval=interval, once=once, show_all=show_all)

    @app.command("approve")
    def approve_cmd(run_id: str = typer.Argument(..., help="Run id"),
                    approve_all: bool = typer.Option(False, "--all", help="Approve every source in the plan"),
                    reject: Optional[List[str]] = typer.Option(None, "--reject", help="Reject this source (repeat)"),
                    note: Optional[str] = typer.Option(None, "--note", help="Note stored with the approval"),
                    wait: float = typer.Option(60.0, "--wait", help="Seconds to wait for the plan to be ready"),
                    api: Optional[str] = api_opt()) -> None:
        """Commit the approval: --all approves every source, --reject S-00x rejects one (repeatable)."""
        console = make_console()
        rejected = [r.strip().upper() for r in reject or [] if r.strip()]
        if not approve_all and not rejected:
            say(console, ("error ", "bold red"), ("say --all, or name sources with --reject S-00x", "red"))
            raise typer.Exit(2)
        c = client(api)
        with handled(console):
            run = wait_for_approval(c, run_id, wait)
            if run["status"] != "awaiting_approval":
                raise CliError(409, "invalid_state", f"Run is {run['status']} at {run['stage']}, not awaiting approval")
            sources = c.get(f"/runs/{_q(run_id)}/plan")["sources"]
            unknown = sorted(set(rejected) - {s["source_id"] for s in sources})
            if unknown:
                raise CliError(422, "validation_error", f"Not in the plan: {', '.join(unknown)}",
                               {"known": [s["source_id"] for s in sources]})
            decisions = decisions_for(sources, approve_all=approve_all, reject=rejected)
            resp = c.post(f"/runs/{_q(run_id)}/approval", {"decisions": decisions, "note": note})
            plan = c.get(f"/runs/{_q(run_id)}/plan")["sources"]
        _approval_table(console, plan)
        pol = resp["policy"]
        say(console, (f"Gateway policy v{pol['version']}: ", "bold magenta"),
            (f"{len(pol['rules'])} rules, default {pol['default_effect']}. ", "magenta"),
            (f"Run is {resp['run']['status']} at {resp['run']['stage']}.", ""))

    @app.command("revoke")
    def revoke_cmd(run_id: str = typer.Argument(..., help="Run id"),
                   source_id: str = typer.Argument(..., help="Source id, e.g. S-006"),
                   reason: Optional[str] = typer.Option(None, "--reason", help="Why, for the record"),
                   api: Optional[str] = api_opt()) -> None:
        """Revoke an approved source mid-run. The gateway blocks the next request to it."""
        console = make_console()
        sid = source_id.strip().upper()
        with handled(console):
            resp = client(api).post(f"/runs/{_q(run_id)}/sources/{_q(sid)}/revoke", {"reason": reason})
        src, pol = resp["source"], resp["policy"]
        rule = next((r["rule_id"] for r in pol["rules"] if r.get("source_id") == sid and r["effect"] == "deny"),
                    f"R-revoke-{sid}")
        say(console, ("REVOKED ", "bold red"), (f"{sid} ", "bold"), (f"{_trim(src['title'], 70)}: status {src['status']}. ", ""),
            (f"Policy v{pol['version']} adds {rule}: the next request to it is blocked.", "red"))

    @app.command("stop")
    def stop_cmd(run_id: str = typer.Argument(..., help="Run id"),
                 reason: Optional[str] = typer.Option(None, "--reason", help="Why, shown in the UI banner"),
                 api: Optional[str] = api_opt()) -> None:
        """Kill switch: stop the run now. The gateway denies every later fetch and model call."""
        console = make_console()
        with handled(console):
            run = client(api).post(f"/runs/{_q(run_id)}/stop", {"reason": reason})
        say(console, ("STOPPED ", "bold red"), (f"{run['run_id']} at {run['stage']}: {run.get('stop_reason') or 'stopped'}. ", ""),
            ("Kill switch on: the gateway denies every later request.", "red"))

    @app.command("status")
    def status_cmd(run_id: str = typer.Argument(..., help="Run id"),
                   as_json: bool = typer.Option(False, "--json", help="Print the Run object as JSON"),
                   api: Optional[str] = api_opt()) -> None:
        """One run: status, stage, counts, gateway decisions and spend."""
        console = make_console()
        with handled(console):
            run = client(api).get(f"/runs/{_q(run_id)}")
        if as_json:
            console.print_json(json.dumps(run))
            return
        _status_table(console, run)

    @app.command("runs")
    def runs_cmd(api: Optional[str] = api_opt()) -> None:
        """Every run the API knows, newest first."""
        console = make_console()
        with handled(console):
            runs = client(api).get("/runs")["runs"]
        _runs_table(console, runs)

    @app.command("inject-errors")
    def inject_cmd(run_id: str = typer.Argument(..., help="Run id"),
                   count: int = typer.Option(3, "--count", "-n", min=1, max=10, help="How many quotes to corrupt"),
                   api: Optional[str] = api_opt()) -> None:
        """DEMO CONTROL: corrupt one digit in N verified quotes and watch the checker catch them."""
        console = make_console()
        with handled(console):
            resp = client(api).post(f"/runs/{_q(run_id)}/demo/inject-errors", {"count": count})
        say(console, ("DEMO CONTROL ", "bold yellow"), ("(deliberate injection, not a real error)", "yellow"))
        say(console, ("Items ", "dim"), (", ".join(resp["item_ids"]) or "none", "bold"))
        say(console, (resp["note"], ""))

    return app


def _approval_table(console: Any, sources: list[dict]) -> None:
    from rich.table import Table

    t = Table(title="Approval", show_lines=False)
    for col in ("source", "tier", "approval", "title"):
        t.add_column(col)
    for s in sources:
        t.add_row(s["source_id"], s["tier"], _text([(s["approval"], APPROVAL_STYLE.get(s["approval"], ""))]),
                  _trim(s["title"], 60))
    console.print(t)


def _status_table(console: Any, run: dict) -> None:
    from rich.table import Table

    c, u = run["counts"], run["usage"]
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column()
    budget = f" of {_money(u.get('budget_usd'))}" if u.get("budget_usd") is not None else ""
    rows = [
        ("run", run["run_id"]),
        ("company", run["company"] + (f" ({run['ticker']})" if run.get("ticker") else "")),
        ("status", _text([(f"{run['status']} at {run['stage']}", STATUS_STYLE.get(run["status"], ""))])),
        ("mode", run.get("mode", "live") + ("" if run.get("mode", "live") == "live" else " (recorded, not live)")),
        ("via", run.get("created_via", "")),
        ("sources", f"{c['sources_total']} total, {c['sources_approved']} approved, {c['sources_fetched']} fetched, "
                    f"{c['docs']} documents"),
        ("agents", f"{c['agents_total']} spawned, {c['agents_active']} working"),
        ("items", f"{c['items_total']} total: {c['items_pass']} pass, {c['items_fixed']} fixed, "
                  f"{c['items_fail']} fail, {c['items_pending']} pending"),
        ("gateway", _text([(f"allowed {c['gateway_allowed']}  ", "green"), (f"blocked {c['gateway_blocked']}  ", "red"),
                           (f"flagged {c['gateway_flagged']}", "yellow")])),
        ("spend", f"{u['model_calls']} model calls, {u['tokens_in']:,} in / {u['tokens_out']:,} out, "
                  f"{_money(u['cost_usd'])}{budget}"),
        ("policy", f"v{run.get('policy_version', 0)}"),
    ]
    if run.get("stop_reason"):
        rows.append(("stopped", run["stop_reason"]))
    if run.get("error"):
        rows.append(("error", _text([(run["error"], "red")])))
    link = ui_link(run["run_id"])
    if link:
        rows.append(("ui", link))
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)


def _runs_table(console: Any, runs: list[dict]) -> None:
    from rich.table import Table

    t = Table(title="Runs")
    for col in ("run", "company", "status", "stage", "mode", "via", "cost"):
        t.add_column(col)
    for r in runs:
        t.add_row(r["run_id"], r["company"], _text([(r["status"], STATUS_STYLE.get(r["status"], ""))]), r["stage"],
                  r.get("mode", "live"), r.get("created_via", ""), _money(r["usage"]["cost_usd"]))
    console.print(t)


def main() -> None:
    build_app()()


if __name__ == "__main__":
    main()
