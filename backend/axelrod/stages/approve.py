"""Stage approve: wait for POST /approval, apply the ApprovalRequest rules, build the policy.

The rules run inside the request (`commit`), so a bad request gets its 422 right away and the response
carries the new policy. The stage itself only announces `awaiting_approval` and waits for the commit.
Rules (CONTRACT.md section 5): every priority source needs a decision; tier and rank moves are applied;
non-priority sources left out become auto_approved; analyst-added sources are approved unless rejected.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..contract import m

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

FOLDER_BY_KIND: dict[str, str] = {
    "sec_filing": "01-Annual-Reports", "xbrl_facts": "01-Annual-Reports", "annual_report": "01-Annual-Reports",
    "company_ir": "03-Other-Company-Reports", "company_site": "11-Technical-Explainers",
    "product_page": "11-Technical-Explainers", "news": "04-News-Coverage", "gov": "05-Regulatory-and-Legal",
    "market_report": "10-Demand-Evidence", "forum": "10-Demand-Evidence", "code_repo": "11-Technical-Explainers",
    "data_api": "00-Meta", "other": "00-Meta",
}
DATA_TYPE_BY_KIND: dict[str, str] = {
    "sec_filing": "financial_filing", "xbrl_facts": "structured_financials", "annual_report": "financial_filing",
    "company_ir": "investor_presentation", "company_site": "company_page", "product_page": "product_page",
    "news": "news_article", "gov": "government_regulation", "market_report": "market_report",
    "forum": "forum_post", "code_repo": "code_repository", "data_api": "dataset", "other": "other",
}
TIERS = ("priority", "tier_2", "tier_3")


class ApprovalError(Exception):
    """A rule broke. The API sends it as the contract error envelope."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


async def run(ctx: RunContext) -> None:
    ctx.emit("run.status", {"status": "awaiting_approval", "stage": "approve"}, stage="approve",
             message="Waiting for the analyst to decide the priority sources")
    await ctx.store.save_async(ctx.state)
    await ctx.approval.wait()          # set by commit(), or by POST /stop to wake us up
    ctx.check_stop()


# ---------------------------------------------------------------------------
# POST /approval
# ---------------------------------------------------------------------------


def validate(state: RunState, req: m.ApprovalRequest) -> None:
    """409 unless awaiting approval, 422 with details.unknown, details.duplicate or details.undecided."""
    if state.run.status != "awaiting_approval":
        raise ApprovalError(409, "invalid_state",
                            f"Run is {state.run.status} at stage {state.run.stage}, not awaiting approval")
    ids = [d.source_id for d in req.decisions]
    known = state.sources_by_id()
    unknown = sorted(set(ids) - set(known))
    if unknown:
        raise ApprovalError(422, "validation_error", "Unknown source ids", {"unknown": unknown})
    duplicate = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate:
        raise ApprovalError(422, "validation_error", "Each source can be decided once", {"duplicate": duplicate})
    undecided = sorted(s.source_id for s in state.sources
                       if s.tier == "priority" and s.proposed_by != "user" and s.source_id not in ids)
    if undecided:
        raise ApprovalError(422, "validation_error", "Every priority source needs a decision",
                            {"undecided": undecided})


def apply_decisions(sources: list[m.Source], decisions: list[m.SourceDecision]) -> tuple[list[str], list[str], list[str]]:
    """Mutates sources. Returns (approved, rejected, auto_approved), each in source order."""
    by_id = {s.source_id: s for s in sources}
    decided = {d.source_id: d for d in decisions}
    explicit_rank: set[str] = set()
    for d in decisions:
        s = by_id[d.source_id]
        s.approval = d.approval
        if d.tier:
            s.tier = d.tier
        if d.rank:
            s.rank = d.rank
            explicit_rank.add(s.source_id)
    approved, rejected, auto = [], [], []
    for s in sources:
        if s.source_id in decided:
            (approved if s.approval == "approved" else rejected).append(s.source_id)
        elif s.proposed_by == "user" and s.approval != "rejected":
            s.approval = "approved"
            approved.append(s.source_id)
        elif s.approval == "pending" and s.tier != "priority":
            s.approval = "auto_approved"
            auto.append(s.source_id)
        s.status = "queued" if s.approval in ("approved", "auto_approved") else "skipped"
    _rerank(sources, explicit_rank)
    return approved, rejected, auto


def _rerank(sources: list[m.Source], explicit: set[str]) -> None:
    """Ranks become 1..n inside each tier. On a tie the source the analyst just placed goes first."""
    order = {s.source_id: i for i, s in enumerate(sources)}
    for tier in TIERS:
        members = sorted((s for s in sources if s.tier == tier),
                         key=lambda s: (s.rank, s.source_id not in explicit, order[s.source_id]))
        for i, s in enumerate(members, start=1):
            s.rank = i


def commit(ctx: RunContext, req: m.ApprovalRequest) -> m.ApprovalResponse:
    """Validate, apply the decisions, build the gateway policy, emit approval.committed and policy.updated,
    then release the waiting stage. Nothing changes when the policy can't be built."""
    state = ctx.state
    validate(state, req)
    backup = [s.model_copy(deep=True) for s in state.sources]
    approved, rejected, auto = apply_decisions(state.sources, req.decisions)
    old_policy = state.policy
    try:
        policy = ctx.gateway.build_policy(emit=False)      # we emit, so approval.committed comes first
    except NotImplementedError:
        state.sources[:], state.policy = backup, old_policy
        raise ApprovalError(409, "invalid_state", "not built yet: the gateway cannot build a policy in this build")
    except Exception:
        state.sources[:], state.policy = backup, old_policy
        raise
    state.policy = policy
    state.approval_request = req
    ctx.emit("approval.committed", {"approved": approved, "rejected": rejected, "auto_approved": auto},
             stage="approve", message=f"Approved {len(approved)}, rejected {len(rejected)}, "
                                      f"auto-approved {len(auto)}")
    ctx.emit("policy.updated", {"version": policy.version, "change": "Approval committed",
                                "rule_ids": [r.rule_id for r in policy.rules]}, stage="approve",
             message=f"Gateway policy v{policy.version}: allowlist built from approved sources, default deny")
    ctx.emit("run.status", {"status": "running", "stage": "approve"}, stage="approve",
             message="Approval committed, collecting next")
    ctx.approval.set()
    return m.ApprovalResponse(run=state.run, policy=state.policy_view())


# ---------------------------------------------------------------------------
# POST /sources (analyst adds a source while the run waits for approval)
# ---------------------------------------------------------------------------


def add_source(ctx: RunContext, req: m.AddSourceRequest) -> tuple[m.Source, str | None]:
    """Returns the new source and, when an api_key came with it, its secret_ref (the caller keeps the key
    in memory only). The source is approved unless the approval rejects it."""
    state = ctx.state
    if state.run.status != "awaiting_approval":
        raise ApprovalError(409, "invalid_state", "Sources can only be added while the run waits for approval")
    url = req.url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApprovalError(422, "validation_error", "url must be an absolute http(s) URL", {"url": req.url})
    if "@" in parsed.netloc:
        raise ApprovalError(422, "validation_error", "url must not carry credentials", {"url": "(redacted)"})
    sid = ctx.next_id("S")
    while sid in state.sources_by_id():
        sid = ctx.next_id("S")
    kind = req.kind or "other"
    ref = f"secret://{ctx.run_id}/{sid}" if req.api_key else None
    rank = 1 + max([s.rank for s in state.sources if s.tier == req.tier] or [0])
    source = m.Source(
        source_id=sid, title=(req.title or "").strip() or (parsed.netloc + parsed.path), url=url,
        domain=parsed.netloc.lower(), kind=kind, data_types=[DATA_TYPE_BY_KIND[kind]],
        access=m.SourceAccess(method="api" if req.api_key else "http_get",
                              auth="api_key" if req.api_key else "none", secret_ref=ref),
        tier=req.tier, rank=rank, scores=m.SourceScores(reliability=0.5, relevance=0.5),
        rationale="Added by the analyst" + (f": {req.note.strip()}" if (req.note or "").strip() else ""),
        need_ids=[], criteria=[], vault_folder=FOLDER_BY_KIND[kind], proposed_by="user", approval="approved",
        status="proposed",
    )
    state.sources.append(source)
    ctx.emit("plan.source", {"source": source}, stage="approve", message=f"Analyst added {sid}: {source.title}")
    return source, ref
