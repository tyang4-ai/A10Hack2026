"""Stage collect: fetch approved sources through the gateway in tier then rank order, ingest, scan untrusted ones.

One fetcher agent does the requests, one source at a time, so the order on screen is the plan's order and a
revoke mid-collect blocks the next request to that source. Every failure is recorded on its source and the
stage carries on with the next one. Only a stop (kill switch) ends the stage early.

tier_3 HTML pages also get their off-page links requested through the gateway (why="link found in page").
Nothing approves those domains, so default deny blocks them: that is the demo's disallowed action. A link
the policy happens to allow is fetched and dropped, never ingested, because links are not planned sources.

Live demo: before every request (source or link) the fetcher logs "next: S-006 ...", and the gateway waits
DEMO_PACING seconds before deciding it. So the presenter sees what is coming and has time to revoke it, and a
revoke that lands during the wait blocks that very request. The plan puts S-006 (the revoke target) 8th.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urlsplit

from ..contract import m
from ..fetch.html import html_links
from ..fetch.ingest import INDEX_RELPATH, document_index, ingest, state_source
from ..gateway import Blocked
from ..ids import now_iso
from ..runtime import StopRequested

if TYPE_CHECKING:
    from ..gateway import FetchResult
    from ..runtime import RunContext

STAGE = "collect"
TASK = "Fetch approved sources"
TIER_ORDER = {"priority": 0, "tier_2": 1, "tier_3": 2}
APPROVED = ("approved", "auto_approved")
ALREADY_STORED = ("fetched", "quarantined", "extracting", "extracted")
MAX_LINKS = 3
LINK_WHY = "link found in page"
NEXT_TITLE_CHARS = 60


async def run(ctx: RunContext) -> None:
    sources = collect_order(ctx.state.sources)
    agent = spawn_fetcher(ctx, [s.source_id for s in sources])
    status = "failed"
    try:
        for source in sources:
            ctx.check_stop()
            await collect_source(ctx, agent, source)
        status = "done"
    except StopRequested:
        status = "stopped"
        raise
    finally:
        finish_fetcher(ctx, agent, status)


def collect_order(sources: list[m.Source]) -> list[m.Source]:
    """Approved and auto-approved sources, priority first, then tier_2, then tier_3, each by rank."""
    picked = [s for s in sources if s.approval in APPROVED]
    return sorted(picked, key=lambda s: (TIER_ORDER.get(s.tier, 9), s.rank, s.source_id))


async def collect_source(ctx: RunContext, agent: m.Agent, source: m.Source) -> m.Document | None:
    """Fetch, ingest and (for untrusted kinds) scan one source. Returns its Document, or None when it failed."""
    source = state_source(ctx.state, source.source_id) or source
    if source.doc_ids and source.status in ALREADY_STORED:
        return None  # stored before a restart: never fetch twice
    log(ctx, agent, next_source_text(source))
    if source.status != "revoked":
        set_status(ctx, agent, source, "fetching")
    try:
        result = await ctx.gateway.fetch(agent, source.url, source_id=source.source_id)
    except Blocked as exc:
        set_status(ctx, agent, source, blocked_status(exc), error=exc.reason, level="warn")
        ctx.check_stop()  # a kill-switch block ends the stage
        return None
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001  record it and move on to the next source
        set_status(ctx, agent, source, "failed", error=f"fetch failed: {exc}", level="warn")
        ctx.check_stop()
        return None
    if not 200 <= result.http_status < 300:
        set_status(ctx, agent, source, "failed", error=f"HTTP {result.http_status}", level="warn")
        return None
    try:
        doc = await ingest(ctx, source, result, agent=agent)
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001
        set_status(ctx, agent, source, "failed", error=f"could not read the {result.content_type or 'body'}: {exc}",
                   level="warn")
        return None
    await _write_index(ctx, agent)
    emit_stored(ctx, agent, doc)
    if source.status != "revoked":  # a revoke that landed while it was being stored stays on screen
        if doc.quarantined:
            set_status(ctx, agent, source, "quarantined", error=_quarantine_reason(doc), level="warn")
        else:
            set_status(ctx, agent, source, "fetched")
    if source.tier == "tier_3" and doc.content_type == "html":
        await follow_links(ctx, agent, source, result)
    return doc


async def follow_links(ctx: RunContext, agent: m.Agent, source: m.Source, result: FetchResult) -> list[str]:
    """Request up to MAX_LINKS off-page links through the gateway. Returns the ones it blocked."""
    links = off_page_links(result, [s.url for s in ctx.state.sources])
    blocked: list[str] = []
    for url in links:
        ctx.check_stop()
        log(ctx, agent, f"next: link found in {source.source_id}, not a planned source: {url}")
        try:
            await ctx.gateway.fetch(agent, url, source_id=None, why=LINK_WHY)
        except Blocked:
            blocked.append(url)  # the gateway already emitted its gateway.request
            ctx.check_stop()
            continue
        except StopRequested:
            raise
        except Exception as exc:  # noqa: BLE001
            log(ctx, agent, f"Link {url} from {source.source_id} failed: {exc}", level="warn")
            ctx.check_stop()
            continue
        log(ctx, agent, f"Link {url} from {source.source_id} was allowed by policy. Dropped: links are never ingested.")
    if links:
        log(ctx, agent, f"{source.source_id}: requested {len(links)} link(s) found in the page, "
                        f"{len(blocked)} blocked by the gateway", level="warn" if blocked else "info")
    return blocked


def next_source_text(source: m.Source) -> str:
    """The line logged before a source's request, e.g. next: S-006 Reuters via Euronext: ... (live.euronext.com)."""
    title = " ".join(source.title.split())
    if len(title) > NEXT_TITLE_CHARS:
        title = title[: NEXT_TITLE_CHARS - 3].rstrip() + "..."
    host = urlsplit(source.url).hostname or source.domain
    revoked = ", revoked: the gateway will block it" if source.status == "revoked" else ""
    return f"next: {source.source_id} {title} ({host}{revoked})"


def blocked_status(exc: Blocked) -> str:
    """revoked for a revoke rule, failed when the gateway failed closed on a network error, else blocked."""
    if (exc.rule_id or "").startswith("R-revoke-"):
        return "revoked"
    if exc.rule_id is None and exc.reason.startswith("gateway error"):
        return "failed"
    return "blocked"


def off_page_links(result: FetchResult, known_urls: list[str], limit: int = MAX_LINKS) -> list[str]:
    """Links that leave the page, other domains first, skipping the page itself and every planned source."""
    page = result.final_url or result.url
    skip = {_canon(result.url), _canon(page), *(_canon(u) for u in known_urls)}
    links = [u for u in html_links(result.body, page) if _canon(u) not in skip]
    host = urlsplit(page).hostname
    links.sort(key=lambda u: urlsplit(u).hostname == host)  # stable: page order within each group
    return links[:limit]


def _canon(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


# ---------------------------------------------------------------------------
# Agent and events
# ---------------------------------------------------------------------------


def spawn_fetcher(ctx: RunContext, target_ids: list[str]) -> m.Agent:
    """Register a fetcher agent (no model) in RunState, named there, and emit agent.spawned."""
    agent = m.Agent(agent_id=ctx.next_id("fetcher"), role="fetcher", status="working", task=TASK,
                    target_ids=target_ids, started_at=now_iso())
    ctx.state.register_agent(agent)
    ctx.emit("agent.spawned", m.AgentSpawnedData(agent=agent.model_copy(deep=True)),
             message=f"{agent.agent_id} spawned: {TASK}", stage=STAGE, agent_id=agent.agent_id)
    return agent


def finish_fetcher(ctx: RunContext, agent: m.Agent, status: str) -> None:
    held = ctx.state.agents.get(agent.agent_id, agent)
    held.status = status
    held.finished_at = now_iso()
    ctx.emit("agent.status", m.AgentStatusData(agent_id=agent.agent_id, status=status, task=agent.task,
                                               usage=held.usage.model_copy()),
             message=f"{agent.agent_id} {status}", stage=STAGE, agent_id=agent.agent_id)


def set_status(ctx: RunContext, agent: m.Agent, source: m.Source, status: str, *, error: str | None = None,
               level: str = "info") -> None:
    held = state_source(ctx.state, source.source_id) or source
    held.status = status
    held.error = error
    text = f"{held.source_id} {status}" + (f": {error}" if error else _size_note(held))
    ctx.emit("source.status", m.SourceStatusData(source_id=held.source_id, status=status,
                                                 stats=held.stats.model_copy(), error=error),
             message=text, stage=STAGE, agent_id=agent.agent_id, level=level)


def emit_stored(ctx: RunContext, agent: m.Agent, doc: m.Document) -> None:
    note = " (quarantined: no agent will read it)" if doc.quarantined else ""
    ctx.emit("doc.stored", m.DocStoredData(doc_id=doc.doc_id, source_id=doc.source_id, pages=doc.pages,
                                           vault_path=doc.vault_paths.raw, scan=doc.scan.model_copy()),
             message=f"{doc.doc_id} stored, {doc.pages} page(s){note}", stage=STAGE, agent_id=agent.agent_id,
             level="warn" if doc.quarantined else "info")


def log(ctx: RunContext, agent: m.Agent, text: str, *, level: str = "info") -> None:
    ctx.emit("agent.log", m.AgentLogData(text=text, transcript_path=None), message=text, stage=STAGE,
             agent_id=agent.agent_id, level=level)


def _quarantine_reason(doc: m.Document) -> str:
    if doc.scan.verdict == "flagged":
        return f"Prompt attack detected in content ({doc.scan.detail}). Stored, never shown to an agent."
    return f"Quarantined until a clean scan ({doc.scan.detail or doc.scan.verdict}). Never shown to an agent."


def _size_note(source: m.Source) -> str:
    if source.status != "fetched" or not source.stats.bytes:
        return ""
    return f": {source.stats.pages} page(s), {source.stats.bytes:,} bytes"


async def _write_index(ctx: RunContext, agent: m.Agent) -> None:
    """Refresh 00-Meta/_document-index.json after every stored document. A failure here is only logged."""
    try:
        await asyncio.to_thread(ctx.vault.put_text, ctx.state.run.run_id, INDEX_RELPATH, document_index(ctx.state))
    except Exception as exc:  # noqa: BLE001
        log(ctx, agent, f"Could not write the document index: {exc}", level="warn")
