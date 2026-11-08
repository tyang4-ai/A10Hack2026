"""FastAPI app: every route the mock answers, with the same paths, shapes, status codes and error envelope,
backed by RunState instead of fixtures.

Run with `uvicorn backend.axelrod.api:app` from the repo root. `create_app(settings)` builds a fresh app
(tests use it with their own DATA_DIR). Settings are read at startup, not at import.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import BACKEND_DIR, Settings, get_settings
from .contract import CONTRACT_VERSION, m
from .ids import now_iso
from .orchestrator import start_run
from .runtime import RunContext
from .stages import approve
from .store import Store, Vault
from .store.events import events_page
from .store.state import RunState

PREFIX = "/api/v1"
KEY_HEADER = "X-Axelrod-Key"
TEST_PAGES_DIR = BACKEND_DIR / "data" / "test_pages"
TEST_PAGE_NAME = re.compile(r"[a-z0-9_-]+\.html")      # fullmatch: a plain file name, no dots, slashes or newline
SIMULATED_HEADER = "X-Axelrod-Simulated"
log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def _envelope(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message, "details": details or {}}})


class Runtime:
    """What the routes share: settings, the store, the vault, and one RunContext per run touched."""

    def __init__(self, settings: Settings, store: Store | None = None, vault: Vault | None = None) -> None:
        self.settings = settings
        self.store = store or Store(settings)
        self.vault = vault or self.store.vault
        self.contexts: dict[str, RunContext] = {}

    def state(self, run_id: str) -> RunState:
        try:
            return self.store.get(run_id)
        except KeyError:
            raise ApiError(404, "not_found", f"No run {run_id}") from None

    def ctx(self, run_id: str) -> RunContext:
        """The run's context, built on first use for runs loaded from storage."""
        state = self.state(run_id)
        ctx = self.contexts.get(run_id)
        if ctx is None or ctx.state is not state:
            ctx = RunContext.create(state, self.store, self.vault, self.settings)
            self.contexts[run_id] = ctx
        return ctx

    async def offload(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Vault calls go to S3 in aws mode: run them off the event loop."""
        if self.settings.store == "aws":
            return await asyncio.to_thread(fn, *args)
        return fn(*args)


def _runtime(request: Request) -> Runtime:
    return request.app.state.rt


RT = Annotated[Runtime, Depends(_runtime)]


def create_app(settings: Settings | None = None, *, store: Store | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(settings or get_settings(), store=store)
        rt.store.load_all()
        app.state.rt = rt
        yield
        tasks = [c.task for c in rt.contexts.values() if c.task is not None and not c.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rt.store.close()

    app = FastAPI(title="Axelrod API", version=CONTRACT_VERSION, lifespan=lifespan,
                  description="Axelrod backend: research harness API (contract/models.py shapes).")
    _install_errors(app)
    _install_key_check(app)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.get("/healthz", response_model=m.Health)(healthz)
    app.api_route("/test-pages/{name}", methods=["GET", "HEAD"], response_class=HTMLResponse)(serve_test_page)
    _install_routes(app)
    return app


# ---------------------------------------------------------------------------
# Errors and the optional shared key
# ---------------------------------------------------------------------------


def _install_errors(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return _envelope(e.status, e.code, e.message, e.details)

    @app.exception_handler(approve.ApprovalError)
    async def _approval_error(_: Request, e: approve.ApprovalError):
        return _envelope(e.status, e.code, e.message, e.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        errors = [{"loc": list(err.get("loc", [])), "msg": str(err.get("msg", "")), "type": str(err.get("type", ""))}
                  for err in e.errors()]
        return _envelope(422, "validation_error", "Request body or query is invalid", {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, e: StarletteHTTPException):
        code = {404: "not_found", 409: "conflict", 400: "validation_error", 422: "validation_error"}.get(
            e.status_code, "invalid_state" if e.status_code == 405 else "internal")
        return _envelope(e.status_code, code, str(e.detail))

    @app.exception_handler(Exception)
    async def _internal(_: Request, e: Exception):
        log.exception("unhandled error")
        return _envelope(500, "internal", f"{type(e).__name__}: {e}")


def _install_key_check(app: FastAPI) -> None:
    """When AXELROD_API_KEY is set, every /api/v1 request needs X-Axelrod-Key. Preflights and /healthz pass."""

    @app.middleware("http")
    async def _key_check(request: Request, call_next):
        rt = getattr(request.app.state, "rt", None)
        key = rt.settings.api_key if rt is not None else None
        if key and request.method != "OPTIONS" and request.url.path.startswith(PREFIX):
            given = request.headers.get(KEY_HEADER, "")
            if not hmac.compare_digest(given.encode("utf-8"), key.encode("utf-8")):
                return _envelope(401, "policy_denied", f"Missing or wrong {KEY_HEADER} header")
        return await call_next(request)


async def healthz():
    return m.Health(ok=True, version=CONTRACT_VERSION, mode="live")


async def serve_test_page(name: str):
    """SIMULATED planted pages from backend/data/test_pages/, so S-010 works while the GitHub repo is private.

    Outside /api/v1, so the X-Axelrod-Key check never applies (the gateway's fetcher sends no key). Only a plain
    lowercase file name in that one folder is served. Anything else is a 404, which rules out path traversal.
    """
    if not TEST_PAGE_NAME.fullmatch(name):
        raise ApiError(404, "not_found", "No such test page")
    path = TEST_PAGES_DIR / name
    if path.resolve().parent != TEST_PAGES_DIR.resolve() or not path.is_file():
        raise ApiError(404, "not_found", "No such test page")
    return Response(path.read_bytes(), media_type="text/html; charset=utf-8",
                    headers={SIMULATED_HEADER: "true", "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _install_routes(app: FastAPI) -> None:
    p = PREFIX
    app.post(f"{p}/runs", response_model=m.Run, status_code=201)(create_run)
    app.get(f"{p}/runs", response_model=m.RunList)(list_runs)
    app.get(f"{p}/runs/{{run_id}}", response_model=m.Run)(read_run)
    app.get(f"{p}/runs/{{run_id}}/events", response_model=m.EventsPage)(read_events)
    app.post(f"{p}/runs/{{run_id}}/stop", response_model=m.Run)(stop_run)
    app.get(f"{p}/runs/{{run_id}}/plan", response_model=m.Plan)(read_plan)
    app.post(f"{p}/runs/{{run_id}}/sources", response_model=m.Source, status_code=201)(add_source)
    app.post(f"{p}/runs/{{run_id}}/approval", response_model=m.ApprovalResponse)(approve_run)
    app.post(f"{p}/runs/{{run_id}}/sources/{{source_id}}/revoke", response_model=m.RevokeResponse)(revoke)
    app.get(f"{p}/runs/{{run_id}}/policy", response_model=m.Policy)(read_policy)
    app.get(f"{p}/runs/{{run_id}}/report", response_model=m.AgentReport)(read_report)
    app.get(f"{p}/runs/{{run_id}}/summary", response_model=m.HumanReport)(read_summary)
    app.patch(f"{p}/runs/{{run_id}}/items/{{item_id}}", response_model=m.Item)(patch_item)
    app.get(f"{p}/runs/{{run_id}}/agents", response_model=m.AgentList)(read_agents)
    app.get(f"{p}/runs/{{run_id}}/documents", response_model=m.DocumentList)(list_documents)
    app.get(f"{p}/runs/{{run_id}}/documents/{{doc_id}}", response_model=m.Document)(read_document)
    app.get(f"{p}/runs/{{run_id}}/documents/{{doc_id}}/pages/{{page}}", response_model=m.DocumentPage)(read_page)
    app.get(f"{p}/runs/{{run_id}}/vault/tree", response_model=m.VaultNode)(vault_tree)
    app.get(f"{p}/runs/{{run_id}}/vault/file", response_model=m.VaultFile)(vault_file)
    app.get(f"{p}/runs/{{run_id}}/comments", response_model=m.CommentList)(list_comments)
    app.post(f"{p}/runs/{{run_id}}/comments", response_model=m.Comment, status_code=201)(add_comment)
    app.post(f"{p}/runs/{{run_id}}/demo/inject-errors", response_model=m.InjectErrorsResponse)(inject_errors)


# -- runs ---------------------------------------------------------------------------------


async def create_run(req: m.CreateRunRequest, rt: RT):
    ctx = await start_run(rt.store, rt.vault, rt.settings, req)
    rt.contexts[ctx.run_id] = ctx
    return ctx.state.run


async def list_runs(rt: RT):
    return m.RunList(runs=rt.store.list_runs())


async def read_run(run_id: str, rt: RT):
    return rt.state(run_id).run


async def read_events(run_id: str, rt: RT, after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)):
    return events_page(rt.state(run_id), after=after, limit=limit)


async def stop_run(run_id: str, rt: RT, req: Optional[m.StopRequest] = Body(None)):
    """Kill switch: the run is stopped now, the gateway denies every later request, waiting stages wake up."""
    ctx = rt.ctx(run_id)
    state = ctx.state
    if state.is_finished():
        raise ApiError(409, "invalid_state", f"Run is already {state.run.status}")
    reason = ((req.reason if req else None) or "").strip() or "Stopped by user"
    state.kill_switch = True
    state.run.stop_reason = reason
    stage = state.run.stage
    ctx.emit("run.status", {"status": "stopped", "stage": stage}, stage=stage, level="warn", message="Kill switch on")
    ctx.emit("run.stopped", {"reason": reason}, stage=stage, level="warn", message=f"Run stopped: {reason}")
    _kill_policy(ctx, reason)
    _reject_open_comments(ctx)
    ctx.approval.set()
    await rt.store.save_async(state)
    return state.run


def _kill_policy(ctx: RunContext, reason: str) -> None:
    """Gateway.set_kill_switch flips the policy. If it isn't built (or fails), flip it here: fail closed."""
    try:
        ctx.gateway.set_kill_switch(reason)
        return
    except NotImplementedError:
        pass
    except Exception:
        log.exception("set_kill_switch failed for %s, flipping the policy directly", ctx.run_id)
    policy = ctx.state.policy
    if policy is None or policy.kill_switch:
        return
    policy.kill_switch = True
    policy.version += 1
    policy.updated_at = now_iso()
    ctx.emit("policy.updated", {"version": policy.version, "change": "Kill switch on", "rule_ids": []},
             level="warn", message=f"Gateway policy v{policy.version}: kill switch, every request denied")


def _reject_open_comments(ctx: RunContext) -> None:
    for c in ctx.state.comments:
        if c.status in ("queued", "working"):
            c.status, c.resolved_at = "rejected", now_iso()
            c.response = "Run stopped before this comment was applied."
            ctx.emit("comment.resolved", {"comment": c}, level="warn",
                     message=f"Comment {c.comment_id} rejected: run stopped")


# -- plan, approval, sources, policy ----------------------------------------------------


async def read_plan(run_id: str, rt: RT):
    return rt.state(run_id).plan()


async def add_source(run_id: str, req: m.AddSourceRequest, rt: RT):
    ctx = rt.ctx(run_id)
    source, ref = approve.add_source(ctx, req)
    if ref and req.api_key:
        rt.store.put_secret(ref, req.api_key)       # memory only, never echoed or written
    await rt.store.save_async(ctx.state)
    return source


async def approve_run(run_id: str, req: m.ApprovalRequest, rt: RT):
    ctx = rt.ctx(run_id)
    resp = approve.commit(ctx, req)
    await rt.store.save_async(ctx.state)
    return resp


async def revoke(run_id: str, source_id: str, rt: RT, req: Optional[m.RevokeRequest] = Body(None)):
    """After approval only. The gateway adds R-revoke-<S>, so the next request to that source is blocked."""
    ctx = rt.ctx(run_id)
    state = ctx.state
    source = state.sources_by_id().get(source_id)
    if source is None:
        raise ApiError(404, "not_found", f"No source {source_id}")
    if state.approval_request is None or state.policy is None:     # a stop before approval also makes a policy
        raise ApiError(409, "invalid_state",
                       "Revoke applies after approval. Before that, reject the source in the approval request.")
    if source.status == "revoked" or any(r.rule_id == f"R-revoke-{source_id}" for r in state.policy.rules):
        raise ApiError(409, "conflict", f"{source_id} is already revoked")
    reason = ((req.reason if req else None) or "").strip() or None
    try:
        # Sets source.status and error, emits policy.updated and source.status itself.
        state.policy = ctx.gateway.revoke(source_id, reason)
    except NotImplementedError:
        raise ApiError(409, "invalid_state", "not built yet: the gateway cannot revoke in this build") from None
    except KeyError:
        raise ApiError(404, "not_found", f"No source {source_id}") from None
    except ValueError as e:
        raise ApiError(409, "invalid_state", str(e)) from None
    await rt.store.save_async(state)
    return m.RevokeResponse(source=source, policy=state.policy_view(rt.settings))


async def read_policy(run_id: str, rt: RT):
    return rt.state(run_id).policy_view(rt.settings)


# -- reports ------------------------------------------------------------------------------


async def read_report(run_id: str, rt: RT):
    return rt.state(run_id).report()


async def read_summary(run_id: str, rt: RT):
    return rt.state(run_id).human_report()


async def patch_item(run_id: str, item_id: str, req: m.ItemPatch, rt: RT):
    ctx = rt.ctx(run_id)
    if ctx.state.run.status == "stopped":
        raise ApiError(409, "invalid_state", "Run is stopped (kill switch)")
    item = ctx.state.items.get(item_id)
    if item is None:
        raise ApiError(404, "not_found", f"No item {item_id}")
    if item.kind == "chart":
        raise ApiError(422, "validation_error", "Charts are built by code and cannot be edited")
    from . import demo

    try:
        return await demo.patch_item(ctx, item_id, req)
    except NotImplementedError:
        raise ApiError(409, "invalid_state", "not built yet: analyst edits are not available in this build") from None


async def inject_errors(run_id: str, req: m.InjectErrorsRequest, rt: RT):
    """Demo control (label it in the UI): corrupt digits in verified quotes so the checker catches them."""
    ctx = rt.ctx(run_id)
    if ctx.state.run.status == "stopped":
        raise ApiError(409, "invalid_state", "Run is stopped (kill switch)")
    from . import demo

    try:
        return await demo.inject_errors(ctx, req)
    except NotImplementedError:
        raise ApiError(409, "invalid_state", "not built yet: demo error injection is not available in this build") \
            from None


# -- agents, documents, vault -------------------------------------------------------------


async def read_agents(run_id: str, rt: RT):
    return rt.state(run_id).agent_list()


async def list_documents(run_id: str, rt: RT):
    return m.DocumentList(documents=list(rt.state(run_id).documents))


def _document(state: RunState, doc_id: str) -> m.Document:
    doc = state.documents_by_id().get(doc_id)
    if doc is None:
        raise ApiError(404, "not_found", f"{doc_id} has not been fetched in this run")
    return doc


async def read_document(run_id: str, doc_id: str, rt: RT):
    return _document(rt.state(run_id), doc_id)


async def read_page(run_id: str, doc_id: str, page: int, rt: RT):
    state = rt.state(run_id)
    doc = _document(state, doc_id)
    if not 1 <= page <= doc.pages:
        raise ApiError(404, "not_found", f"{doc_id} has pages 1 to {doc.pages}")
    from .fetch import ingest

    try:
        return ingest.document_page(state, doc_id, page)
    except NotImplementedError:
        raise ApiError(409, "invalid_state", "not built yet: the page viewer is not available in this build") from None
    except (KeyError, IndexError):
        raise ApiError(404, "not_found", f"{doc_id} page {page} has no stored text") from None


async def vault_tree(run_id: str, rt: RT):
    rt.state(run_id)
    return await rt.offload(rt.vault.tree, run_id)


async def vault_file(run_id: str, rt: RT, path: str = Query(...)):
    rt.state(run_id)
    try:
        return await rt.offload(rt.vault.file, run_id, path)
    except (KeyError, ValueError):
        raise ApiError(404, "not_found", f"No vault file {path}") from None


# -- comments -----------------------------------------------------------------------------


async def list_comments(run_id: str, rt: RT):
    return m.CommentList(comments=list(rt.state(run_id).comments))


async def add_comment(run_id: str, req: m.CommentRequest, rt: RT):
    """Stored and announced. The reviser that acts on item comments comes later. A stopped run rejects them."""
    ctx = rt.ctx(run_id)
    state = ctx.state
    if req.target_type in ("item", "summary_sentence") and not req.target_id:
        raise ApiError(422, "validation_error", "target_id is required for item and summary_sentence comments")
    if req.target_type == "item" and req.target_id not in state.items:
        raise ApiError(404, "not_found", f"No item {req.target_id}")
    cid = ctx.next_id("CM")
    comment = m.Comment(comment_id=cid, target_type=req.target_type, target_id=req.target_id, text=req.text,
                        status="queued", response=None, changed_item_ids=[], created_at=now_iso(), resolved_at=None)
    state.comments.append(comment)
    ctx.emit("comment.created", {"comment": comment}, message=f"Comment {cid} on {req.target_id or req.target_type}")
    if state.run.status == "stopped":
        comment.status, comment.resolved_at = "rejected", now_iso()
        comment.response = "Run is stopped (kill switch). Start a new run to apply comments."
        ctx.emit("comment.resolved", {"comment": comment}, level="warn", message=f"Comment {cid} rejected: run stopped")
    await rt.store.save_async(state)
    return comment


app = create_app()
