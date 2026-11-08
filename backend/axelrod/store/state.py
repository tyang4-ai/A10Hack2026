"""RunState: everything one run knows, as one serializable Pydantic model, plus read helpers.

The API serves snapshots from it, the stages write into it, and the store persists it. Run-level fields
(status, stage, counts, last_seq, usage, policy_version) are kept in sync by RunContext.emit, so stages
change items, sources, agents and documents directly and then emit the matching event.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from ..contract import RUBRIC, m
from ..ids import IdAllocator

if TYPE_CHECKING:
    from ..config import Settings

PageRanges = list[list[int]]        # [[150, 156], [170, 172]]: 1-based PDF page indices, inclusive

ReportStatus = Literal["not_started", "drafting", "verifying", "repairing", "final"]

DISCLAIMER: str = RUBRIC.get("disclaimer") or "Not financial or legal advice."

TIER_ORDER = {"priority": 0, "tier_2": 1, "tier_3": 2}
FETCHED_STATUSES = ("fetched", "extracting", "extracted", "quarantined")
TERMINAL_STATUSES = ("done", "stopped", "failed")

# Report status while report.ready has not fired yet (same mapping as the mock).
_REPORT_STATUS_BY_STAGE: dict[str, ReportStatus] = {
    "plan": "not_started", "approve": "not_started", "collect": "not_started", "extract": "drafting",
    "draft": "drafting", "verify": "verifying", "repair": "repairing",
}
_CATEGORY_ORDER = [c["id"] for c in RUBRIC["categories"]]
_CATEGORY_LABEL = {c["id"]: c["label"] for c in RUBRIC["categories"]}


class RunState(BaseModel):
    run: m.Run
    needs: list[m.DataNeed] = []
    sources: list[m.Source] = []
    extract_pages: dict[str, PageRanges] = {}             # source_id -> pages extractors read
    need_pages: dict[str, dict[str, PageRanges]] = {}     # need_id -> source_id -> pages
    policy: m.Policy | None = None                        # None until approval builds it
    documents: list[m.Document] = []
    pages: dict[str, list[str]] = {}                      # doc_id -> page texts, page n is index n-1
    items: dict[str, m.Item] = {}                         # item id -> DataItem | Conclusion | Extrapolation | Chart
    holes: list[m.Hole] = []
    agents: dict[str, m.Agent] = {}                       # agent_id -> Agent
    comments: list[m.Comment] = []
    summary: m.HumanReport | None = None                  # the brief, set by the summarize stage
    ids: IdAllocator = Field(default_factory=IdAllocator)
    events: list[m.Event] = []

    # Refinements by B-store-api (all optional, so older snapshots still load).
    kill_switch: bool = False                             # POST /stop sets it, check_stop reads it
    plan_ready: bool = False                              # set when plan.ready is emitted
    report_ready: bool = False                            # set when report.ready is emitted
    summary_ready: bool = False                           # set when summary.ready is emitted
    as_of: str | None = None                              # plan.json as_of (YYYY-MM-DD)
    fiscal_note: str | None = None                        # plan.json fiscal_note
    approval_request: m.ApprovalRequest | None = None     # the committed decisions, for the record
    sections: list[m.ReportSection] = []                  # empty: report() groups items by category
    rollup: m.Rollup | None = None                        # set by finalize_report
    report_certainty: m.ReportCertainty | None = None     # set by finalize_report
    report_generated_at: str | None = None
    report_markdown_path: str | None = None
    disclaimer: str = DISCLAIMER

    # -- item views -------------------------------------------------------------------------

    def data_items(self) -> list[m.DataItem]:
        return [i for i in self.items.values() if isinstance(i, m.DataItem)]

    def conclusions(self) -> list[m.Conclusion]:
        return [i for i in self.items.values() if isinstance(i, m.Conclusion)]

    def extrapolations(self) -> list[m.Extrapolation]:
        return [i for i in self.items.values() if isinstance(i, m.Extrapolation)]

    def charts(self) -> list[m.Chart]:
        return [i for i in self.items.values() if isinstance(i, m.Chart)]

    # -- lookups ----------------------------------------------------------------------------

    def sources_by_id(self) -> dict[str, m.Source]:
        return {s.source_id: s for s in self.sources}

    def documents_by_id(self) -> dict[str, m.Document]:
        return {d.doc_id: d for d in self.documents}

    def sorted_sources(self) -> list[m.Source]:
        """Tier, then rank, then id: the order the Sources panel and the fetchers use."""
        return sorted(self.sources, key=lambda s: (TIER_ORDER[s.tier], s.rank, s.source_id))

    def agent_pages(self, doc_id: str) -> list[str]:
        """Page texts an agent may read: empty for quarantined or unknown documents (fail closed)."""
        doc = self.documents_by_id().get(doc_id)
        if doc is None or doc.quarantined:
            return []
        return list(self.pages.get(doc_id, []))

    def is_finished(self) -> bool:
        return self.run.status in TERMINAL_STATUSES

    # -- snapshots for the API ------------------------------------------------------------

    def plan(self) -> m.Plan:
        return m.Plan(run_id=self.run.run_id, needs=list(self.needs), sources=self.sorted_sources(),
                      ready=self.plan_ready, as_of_seq=self.run.last_seq)

    def report_status(self) -> ReportStatus:
        if self.report_ready:
            return "final"
        stage = self.run.stage
        if stage in _REPORT_STATUS_BY_STAGE:
            return _REPORT_STATUS_BY_STAGE[stage]
        return "repairing" if self.run.status != "done" else "final"

    def report_sections(self) -> list[m.ReportSection]:
        """Explicit sections when a stage set them (filtered to existing items), else items grouped by category."""
        if self.sections:
            out = []
            for s in self.sections:
                ids = [i for i in s.item_ids if i in self.items]
                if ids:
                    out.append(m.ReportSection(category=s.category, title=s.title, item_ids=ids))
            return out
        groups: dict[str, list[str]] = {}
        for item in self.items.values():
            groups.setdefault(item.category, []).append(item.id)
        return [m.ReportSection(category=c, title=_CATEGORY_LABEL[c], item_ids=groups[c])
                for c in _CATEGORY_ORDER if c in groups]

    def report(self) -> m.AgentReport:
        """The agent report as it stands. Rollup, certainty and holes appear once it is final."""
        final = self.report_status() == "final"
        return m.AgentReport(
            run_id=self.run.run_id, company=self.run.company, ticker=self.run.ticker, depth=self.run.depth,
            status=self.report_status(), generated_at=self.report_generated_at if final else None,
            sections=self.report_sections(), items=list(self.items.values()),
            rollup=self.rollup if final else None, certainty=self.report_certainty if final else None,
            holes=list(self.holes) if final else [], disclaimer=self.disclaimer,
            markdown_path=self.report_markdown_path, as_of_seq=self.run.last_seq,
        )

    def human_report(self) -> m.HumanReport:
        """The brief if the summarize stage made one, else a not_started placeholder. as_of_seq is current."""
        if self.summary is not None:
            return self.summary.model_copy(update={"as_of_seq": self.run.last_seq})
        return m.HumanReport(
            run_id=self.run.run_id, title=f"{self.run.company}: screening brief", company=self.run.company,
            ticker=self.run.ticker, as_of=self.as_of or self.run.created_at[:10], status="not_started",
            sections=[], holes=[], disclaimer=self.disclaimer,
            stats=m.SummaryStats(sentences=0, passed=0, fixed=0, failed=0, words=0, est_pages=0.0),
            certainty=None, markdown_path=None, as_of_seq=self.run.last_seq,
        )

    def policy_view(self, settings: Settings | None = None) -> m.Policy:
        """What the gateway enforces right now. Before approval it is the gateway's own version 0 base:
        R-sys-models and the role scopes from settings (default: get_settings()), every fetch default deny."""
        if self.policy is not None:
            return self.policy.model_copy(update={"as_of_seq": self.run.last_seq,
                                                  "kill_switch": self.policy.kill_switch or self.kill_switch})
        from ..config import get_settings
        from ..gateway import base_policy

        settings = settings or get_settings()
        budget = self.run.usage.budget_usd if self.run.usage.budget_usd is not None else settings.budget_usd
        return base_policy(self.run.run_id, settings, budget_usd=budget, at=self.run.created_at,
                           as_of_seq=self.run.last_seq, kill_switch=self.kill_switch)

    def agent_list(self) -> m.AgentList:
        return m.AgentList(agents=list(self.agents.values()), as_of_seq=self.run.last_seq)

    # -- counts ---------------------------------------------------------------------------

    def recount(self) -> None:
        """Recompute Run.counts from the state. Gateway counts are kept by the event fold, not here."""
        c = self.run.counts
        c.sources_total = len(self.sources)
        c.sources_approved = sum(s.approval in ("approved", "auto_approved") for s in self.sources)
        c.sources_fetched = sum(s.status in FETCHED_STATUSES for s in self.sources)
        c.docs = len(self.documents)
        c.agents_total = len(self.agents)
        c.agents_active = sum(a.status == "working" for a in self.agents.values())
        statuses = [i.verification.status for i in self.items.values()]
        c.items_total = len(statuses)
        c.items_pass = statuses.count("pass")
        c.items_fixed = statuses.count("fixed")
        c.items_fail = statuses.count("fail")
        c.items_pending = statuses.count("pending")

    def recount_gateway(self) -> None:
        """Gateway counts from the event log (used after a load)."""
        c = self.run.counts
        c.gateway_allowed = c.gateway_blocked = c.gateway_flagged = 0
        for e in self.events:
            if e.type == "gateway.request":
                setattr(c, f"gateway_{e.data.decision}", getattr(c, f"gateway_{e.data.decision}") + 1)
