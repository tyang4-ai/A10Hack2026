"""
Axelrod API contract: Pydantic v2 models.

This file is the single source of truth for every JSON shape that crosses the
backend/frontend boundary. contract/types.ts is GENERATED from it
(python contract/gen_types.py). Change shapes here first, regenerate, update
contract/CHANGELOG.md, then tell the other person.

Conventions
- IDs: run_YYYYMMDD_xxxxxx, N-01 (need), S-001 (source), DOC-001 (document),
  D-001 (data), C-001 (conclusion), E-001 (extrapolation = BOFFIN criterion
  score), G-001 (chart), H-001 (human-report sentence), R-... (policy rule),
  REQ-... (gateway request), CM-001 (comment).
- Timestamps: ISO 8601 UTC with a trailing Z.
- Money: USD as float. Scores: -1, 0, 1, or null for N/A.
- Every model forbids unknown fields, so a typo fails loudly on the backend.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

RunStatus = Literal["running", "awaiting_approval", "done", "stopped", "failed"]

# Pipeline stages, in order. The UI stepper and panel routing use this.
# Sources panel: plan, approve, collect. Agent report panel: extract, draft,
# verify, repair. Human report panel: summarize, verify_summary.
Stage = Literal[
    "plan",
    "approve",
    "collect",
    "extract",
    "draft",
    "verify",
    "repair",
    "summarize",
    "verify_summary",
    "complete",
]

Depth = Literal["screen", "full"]

Dimension = Literal[
    "company",
    "financials",
    "upstream",
    "downstream",
    "market_share",
    "competition",
    "regulation",
    "public_sentiment",
    "management",
    "technology",
    "macro",
]

DataType = Literal[
    "financial_filing",
    "structured_financials",
    "proxy_statement",
    "earnings_call",
    "investor_presentation",
    "press_release",
    "news_article",
    "market_report",
    "government_regulation",
    "legal_filing",
    "product_page",
    "company_page",
    "forum_post",
    "code_repository",
    "patent",
    "dataset",
    "other",
]

SourceKind = Literal[
    "sec_filing",
    "xbrl_facts",       # SEC companyfacts JSON or ESEF xBRL-JSON (EU issuers)
    "annual_report",    # issuer's own annual report PDF (e.g. Siemens Healthineers)
    "company_ir",
    "company_site",
    "product_page",
    "news",
    "gov",
    "market_report",
    "forum",
    "code_repo",
    "data_api",
    "other",
]

Tier = Literal["priority", "tier_2", "tier_3"]

Approval = Literal["pending", "approved", "rejected", "auto_approved"]

SourceStatus = Literal[
    "proposed",
    "queued",
    "fetching",
    "fetched",
    "extracting",
    "extracted",
    "blocked",
    "revoked",
    "quarantined",
    "failed",
    "skipped",
]

# Mirrors the per-company layout of Gordon's Notes/Finance vault.
VaultFolder = Literal[
    "00-Meta",
    "01-Annual-Reports",
    "02-Quarterly-Reports",
    "03-Other-Company-Reports",
    "04-News-Coverage",
    "05-Regulatory-and-Legal",
    "06-Macro-Political-Social",
    "07-Ecosystem-Crosslinks",
    "08-Competitors",
    "09-Analysis",
    "10-Demand-Evidence",
    "11-Technical-Explainers",
    "12-Verification",
]

# BOFFIN: 8 categories, 27 criteria. Labels, weights and thresholds live in
# contract/boffin_rubric.json. These literals must match that file.
Category = Literal[
    "dominant_business",
    "compounder",
    "management",
    "control_of_destiny",
    "financial_strength",
    "shared_value",
    "research_stage",
    "thesis_stability",
]

Criterion = Literal[
    # dominant_business (x2)
    "strategy",
    "demand_side",
    "supply_side",
    "flywheel",
    "rotic_vs_peers",
    # compounder (x2)
    "secular_growth",
    "rotic_level",
    "capital_allocation",
    "share_repo_record",
    # management (x2)
    "mgmt_fit",
    "mgmt_stability",
    "ceo_ownership",
    "ceo_pay",
    "incentive_comp_metrics",
    "incentive_comp_payout",
    "governance",
    # control_of_destiny (x1)
    "competitor_behavior",
    "industry_cyclicality",
    # financial_strength (x1)
    "leverage",
    "liquidity",
    "hidden_risk_value",
    # shared_value (x1)
    "shared_value",
    # research_stage (x1)
    "research_stage",
    # thesis_stability (x1)
    "fundamental_thesis",
    "non_gaap_adjustments",
    "rotic_trend",
    "rotic_estimate_trend",
]

VerificationStatus = Literal["pending", "pass", "fixed", "fail", "blocked"]
CheckMethod = Literal["code", "llm_judge", "user"]
CheckName = Literal[
    "quote_exact",       # quote appears in the stored source text (code)
    "value_in_quote",    # value.as_reported appears inside the quote (code)
    "unit_context",      # a unit/scale citation exists and matches value.scale (code)
    "period_match",      # value.period matches the cited period context (code)
    "json_value",        # JSON-pointer value equals value.number (code)
    "computation",       # computations re-evaluated from data values (code)
    "numbers_traced",    # every number in the text traces to a cited value (code)
    "threshold_rule",    # score recomputed from the rubric threshold (code)
    "tier_rule",         # tier_3 never the only support for a number; tier_2 must not contradict tier 1 (code)
    "context",           # statement keeps the meaning of the quote in its paragraph: no dropped "except" or "not" (llm_judge)
    "support",           # claim is directly supported by cited items (llm_judge)
    "faithfulness",      # summary sentence says no more than its items (llm_judge)
    "copy_exact",        # copied sentence equals the item text (code)
    "chart_values",      # every chart point equals its data item value (code)
    "citations_resolve", # every cited id exists and passed (code)
]
CheckResultValue = Literal["pass", "fail", "blocked"]

AgentRole = Literal[
    "orchestrator",  # v0.3: Bobby, the one agent that makes the big calls. Every other agent is one of his goons
    "planner",
    "fetcher",
    "extractor",
    "drafter",
    "verifier",
    "repairer",
    "summarizer",
    "reviser",
]
AgentStatus = Literal["queued", "working", "done", "failed", "stopped"]

GatewayAction = Literal["fetch", "model.invoke"]
GatewayDecision = Literal["allowed", "blocked", "flagged"]
# v0.2: where a stored document's bytes came from. fetched = fetched live by this run,
# cache = fetched by an earlier Axelrod run and served from the S3 cache,
# archive = collected before the event (Gordon's summer 2026 archive) and served from S3.
DocOrigin = Literal["fetched", "cache", "archive"]
# v0.2: demo cache. documents = fetch bodies served from the S3 cache.
# documents_and_models = model answers replayed from recorded calls too. Label anything but "off".
CacheMode = Literal["off", "documents", "documents_and_models"]

RepairAction = Literal[
    "recite",            # replace the quote with the real source span
    "recompute",         # redo a computation
    "downgrade",         # weaken the claim toward its evidence
    "delete",            # drop the claim
    "not_determinable",  # turn it into a declared hole
    "reword",            # wording fix with the same evidence
    "user_edit",         # a human changed it
    "rework",            # v0.3: a rerun of the agent that produced it, with instructions
]


# ---------------------------------------------------------------------------
# Shared small objects
# ---------------------------------------------------------------------------


class Usage(Model):
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    budget_usd: Optional[float] = None


class RunCounts(Model):
    sources_total: int = 0
    sources_approved: int = 0
    sources_fetched: int = 0
    docs: int = 0
    agents_total: int = 0
    agents_active: int = 0
    items_total: int = 0
    items_pass: int = 0
    items_fixed: int = 0
    items_fail: int = 0
    items_pending: int = 0
    gateway_allowed: int = 0
    gateway_blocked: int = 0
    gateway_flagged: int = 0


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class Run(Model):
    run_id: str
    company: str
    ticker: Optional[str] = None
    depth: Depth = "screen"
    status: RunStatus
    stage: Stage
    created_at: str
    updated_at: str
    created_via: Literal["ui", "cli", "api"] = "ui"
    notes: Optional[str] = None
    policy_version: int = 0
    counts: RunCounts
    usage: Usage
    last_seq: int = 0
    error: Optional[str] = None
    stop_reason: Optional[str] = None
    # live = running now; replay = a recorded real run played back (label it in
    # the UI); fixture = mock data from mock/build_fixtures.py
    mode: Literal["live", "replay", "fixture"] = "live"
    # v0.2: rerun from a step. A rerun is a fork: a new run that reuses the
    # parent's work before rerun_from and redoes the rest with the instructions.
    # The parent run is never changed. All three are null for normal runs.
    parent_run_id: Optional[str] = None
    rerun_from: Optional[Stage] = None
    instructions: Optional[str] = None
    # v0.2: demo cache in use for this run (see CacheMode). Label it in the UI.
    cache_mode: CacheMode = "off"


class CreateRunRequest(Model):
    company: str = Field(min_length=1)
    ticker: Optional[str] = None
    depth: Depth = "screen"
    notes: Optional[str] = None
    budget_usd: Optional[float] = None
    created_via: Literal["ui", "cli", "api"] = "ui"


class RunList(Model):
    runs: list[Run]


class StopRequest(Model):
    reason: Optional[str] = None


class ResumeRequest(Model):  # v0.2
    reason: Optional[str] = None


class RerunRequest(Model):  # v0.2
    from_stage: Stage
    instructions: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Plan: data needs and sources (Sources panel)
# ---------------------------------------------------------------------------


class DataNeed(Model):
    need_id: str
    label: str
    dimension: Dimension
    question: str
    data_types: list[DataType]
    criteria: list[Criterion]
    rationale: str


class SourceAccess(Model):
    method: Literal["http_get", "api", "browser"] = "http_get"
    auth: Literal["none", "api_key"] = "none"
    secret_ref: Optional[str] = None  # a reference only, never the key


class SourceScores(Model):
    reliability: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)


class SourceStats(Model):
    bytes: int = 0
    pages: int = 0
    data_items: int = 0
    data_items_verified: int = 0


class Source(Model):
    source_id: str
    title: str
    url: str
    domain: str
    kind: SourceKind
    data_types: list[DataType]
    access: SourceAccess
    tier: Tier
    rank: int = Field(ge=1)  # order inside its tier, 1 = first
    scores: SourceScores
    rationale: str
    need_ids: list[str]
    criteria: list[Criterion]
    vault_folder: VaultFolder
    proposed_by: Literal["planner", "user"]
    approval: Approval
    status: SourceStatus
    doc_ids: list[str] = []
    stats: SourceStats = SourceStats()
    error: Optional[str] = None


class Plan(Model):
    run_id: str
    needs: list[DataNeed]
    sources: list[Source]
    ready: bool  # True once the planner finished (plan.ready emitted)
    as_of_seq: int


class AddSourceRequest(Model):
    url: str
    title: Optional[str] = None
    kind: Optional[SourceKind] = None
    tier: Tier = "priority"
    note: Optional[str] = None
    api_key: Optional[str] = None  # write-only; stored server-side, never echoed


class SourceDecision(Model):
    source_id: str
    approval: Literal["approved", "rejected"]
    tier: Optional[Tier] = None
    rank: Optional[int] = Field(default=None, ge=1)


class ApprovalRequest(Model):
    # Every priority-tier source must appear here. Non-priority sources that
    # are left out become auto_approved.
    decisions: list[SourceDecision]
    note: Optional[str] = None


class RevokeRequest(Model):
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Gateway policy
# ---------------------------------------------------------------------------


class PolicyRule(Model):
    rule_id: str
    effect: Literal["allow", "deny"]
    action: GatewayAction
    domain: Optional[str] = None
    path_prefix: Optional[str] = None
    model_id: Optional[str] = None
    roles: list[AgentRole] = []  # empty = every role
    source_id: Optional[str] = None
    agent_id: Optional[str] = None  # v0.2: the rule applies to this one agent only (R-stop-<agent_id>)
    origin: Literal["user_approval", "user_rejection", "user_revoke", "user_agent_stop", "auto_tier", "system"]
    note: Optional[str] = None
    created_at: str


class RoleScope(Model):
    role: AgentRole
    actions: list[GatewayAction]
    models: list[str] = []  # model ids this role may call


class Policy(Model):
    run_id: str
    version: int
    default_effect: Literal["deny"] = "deny"
    kill_switch: bool = False
    rules: list[PolicyRule]
    role_scopes: list[RoleScope]
    budget_usd: Optional[float] = None
    updated_at: str
    as_of_seq: int


class ApprovalResponse(Model):
    run: Run
    policy: Policy


class RevokeResponse(Model):
    source: Source
    policy: Policy


# ---------------------------------------------------------------------------
# Documents (stored copies of fetched sources)
# ---------------------------------------------------------------------------


class ScanResult(Model):
    scanner: Literal["bedrock_guardrails", "none"]
    verdict: Literal["clean", "flagged", "error", "skipped"]
    detail: Optional[str] = None


class DocVaultPaths(Model):
    raw: str
    text: str
    meta: str


class Document(Model):
    doc_id: str
    source_id: str
    title: str
    url: str
    content_type: Literal["html", "pdf", "json", "text"]
    paginated: bool  # False = backend split the text into pseudo-pages
    pages: int
    sha256: str
    bytes: int
    fetched_at: str
    vault_paths: DocVaultPaths
    quarantined: bool = False
    scan: ScanResult
    origin: DocOrigin = "fetched"  # v0.2: see DocOrigin. archive = collected before the event
    collected_at: Optional[str] = None  # v0.2: when the bytes were first collected (differs from fetched_at for cache and archive)


class DocumentList(Model):
    documents: list[Document]


class Highlight(Model):
    item_id: str
    char_start: int
    char_end: int
    status: VerificationStatus
    role: Literal["evidence", "unit_context", "period_context"] = "evidence"


class DocumentPage(Model):
    doc_id: str
    page: int
    pages: int
    content_type: Literal["html", "pdf", "json", "text"]
    text: str  # exactly the text the checker matched against
    highlights: list[Highlight]


# ---------------------------------------------------------------------------
# Citations and verification
# ---------------------------------------------------------------------------


class TextLocator(Model):
    type: Literal["text"] = "text"
    page: int = Field(ge=1)  # 1-based page index in the stored file, not the printed page label
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)  # exclusive


class JsonLocator(Model):
    type: Literal["json"] = "json"
    pointer: str  # RFC 6901 JSON pointer into the stored JSON document


Locator = Annotated[Union[TextLocator, JsonLocator], Field(discriminator="type")]


class Citation(Model):
    doc_id: str
    locator: Locator
    quote: str  # verbatim text (text locator) or str(value) at the pointer (json)
    role: Literal["evidence", "unit_context", "period_context"] = "evidence"


class DiffSegment(Model):
    op: Literal["equal", "insert", "delete", "replace"]
    agent: str  # characters in the agent's quote ("" for insert)
    source: str  # characters in the source ("" for delete)


class CheckResult(Model):
    check: CheckName
    method: CheckMethod
    result: CheckResultValue
    detail: str
    model_id: Optional[str] = None
    agent_id: Optional[str] = None
    diff: Optional[list[DiffSegment]] = None
    closest_locator: Optional[TextLocator] = None
    unsupported_spans: list[str] = []
    votes_for: Optional[int] = None    # llm_judge only: judges that said supported
    votes_total: Optional[int] = None  # llm_judge only: judges asked (default 3)


class Attempt(Model):
    attempt: int = Field(ge=1)
    status: VerificationStatus
    checks: list[CheckResult] = []  # the checks as they stood on this attempt
    action: Optional[RepairAction] = None  # what the repair did after this attempt
    before: Optional[str] = None
    after: Optional[str] = None
    note: Optional[str] = None
    at: str


class Verification(Model):
    status: VerificationStatus
    checks: list[CheckResult] = []
    attempts: list[Attempt] = []
    verified_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Certainty (per item, per brief sentence, and per report)
# ---------------------------------------------------------------------------
# Computed by code from verification results, never asked of a model.
# Reference implementation: contract/certainty.py
#   strength    = support x inherited
#   certainty   = strength x completeness, shown as 0-100
#   support     = 1.0 when code proves the item (exact quote, computation,
#                 chart built from refs); judge agreement (votes_for /
#                 votes_total) for semantic checks; 0 when failed
#   inherited   = source reliability for data items; for everything else
#                 the weakest strength among the cited inputs
#   completeness= BOFFIN data coverage, scores only (high 1.0, medium 0.85,
#                 low 0.6); 1.0 elsewhere and never inherited
# Bands: high >= 85, medium 60-84, low < 60, none = failed, blocked,
# pending or not scored.

CertaintyBand = Literal["high", "medium", "low", "none"]


class Certainty(Model):
    score: Optional[int] = Field(default=None, ge=0, le=100)  # None when band is "none"
    band: CertaintyBand
    strength: float = Field(ge=0, le=1)  # support x inherited, what dependents inherit
    support: float = Field(ge=0, le=1)
    inherited: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)
    weakest_ref: Optional[str] = None  # the source or item that limits inherited
    basis: str  # one readable line for the tooltip


class ReportCertainty(Model):
    mean: Optional[int] = None  # word-weighted (brief) or plain (agent report) mean of scored entries
    high_pct: int  # share of words (brief) or items (agent report) in the high band
    high: int
    medium: int
    low: int
    none: int
    coverage_scored: int  # BOFFIN criteria scored; shown next to certainty, never merged into it
    coverage_in_scope: int
    method: str


# ---------------------------------------------------------------------------
# Agent report items (Agent report panel)
# ---------------------------------------------------------------------------


class Value(Model):
    number: float  # as reported, before scale (130497 with scale "million")
    as_reported: str  # exact characters in the quote, e.g. "130,497" or "(1,204)"
    unit: str  # "EUR", "USD", "%", "count", "x", "years"
    scale: Literal["one", "thousand", "million", "billion"] = "one"
    period: Optional[str] = None  # "FY2025", "Q3 FY2026", "2025-12-31"


class DataItem(Model):
    id: str  # D-###
    kind: Literal["data"] = "data"
    category: Category
    criteria: list[Criterion]
    need_id: Optional[str] = None
    statement: str
    value: Optional[Value] = None
    citations: list[Citation] = Field(min_length=1)
    source_id: str
    source_tier: Tier
    extracted_by: Literal["code", "agent"]
    agent_id: Optional[str] = None
    verification: Verification
    certainty: Optional[Certainty] = None  # None until verified


class Computation(Model):
    name: str
    expr: str  # e.g. "D-020 / ((D-021 + D-022) / 2)", over base-unit values
    result: float
    display: str  # how the number appears in the text, e.g. "38.2%"


class Conclusion(Model):
    id: str  # C-###
    kind: Literal["conclusion"] = "conclusion"
    category: Category
    criteria: list[Criterion]
    text: str
    data_ids: list[str] = Field(min_length=1)
    computations: list[Computation] = []
    agent_id: Optional[str] = None
    verification: Verification
    certainty: Optional[Certainty] = None  # None until verified


class ScoreRule(Model):
    type: Literal["threshold", "judgment"]
    plus_one: Optional[str] = None
    minus_one: Optional[str] = None
    input: Optional[str] = None  # "C-007:rotic_avg_5y" for threshold rules


class Extrapolation(Model):
    id: str  # E-###
    kind: Literal["extrapolation"] = "extrapolation"
    category: Category
    criterion: Criterion
    score: Optional[Literal[-1, 0, 1]] = None  # None = N/A
    na_reason: Optional[str] = None
    text: str
    conclusion_ids: list[str]
    rule: ScoreRule
    confidence: Literal["high", "medium", "low"]
    agent_id: Optional[str] = None
    verification: Verification
    certainty: Optional[Certainty] = None  # None until verified


class ChartPoint(Model):
    x: str
    y: float  # in the chart's unit and scale, rounded for display
    ref: str  # "D-001" or "C-001:rotic_fy2025" (conclusion computation)


class ChartSeries(Model):
    name: str
    points: list[ChartPoint]


class Chart(Model):
    # Built by code from refs, never drawn by a model. The frontend renders it.
    id: str  # G-###
    kind: Literal["chart"] = "chart"
    category: Category
    title: str
    chart_type: Literal["bar", "line", "table"]
    unit: str  # "USD", "%"
    scale: Literal["one", "thousand", "million", "billion"] = "one"
    series: list[ChartSeries]
    refs: list[str]  # every D or C id the chart reads
    verification: Verification
    certainty: Optional[Certainty] = None  # None until verified


Item = Annotated[
    Union[DataItem, Conclusion, Extrapolation, Chart], Field(discriminator="kind")
]


class CategoryScore(Model):
    category: Category
    weight: int
    score: Optional[float] = None  # mean of scored criteria, 1 decimal
    scored: int
    in_scope: int
    na_criteria: list[Criterion]


class Rollup(Model):
    categories: list[CategoryScore]
    weighted_average: Optional[float] = None  # 2 decimals, range -1.00 to +1.00
    normalized_pct: Optional[int] = None  # ((avg + 1) / 2) * 100, an inference
    percent_of_max_pct: Optional[int] = None  # max(avg, 0) * 100, an inference
    scored: int
    in_scope: int
    framework_total: int = 27
    na_criteria: list[Criterion]
    note: str


class Hole(Model):
    hole_id: str
    criterion: Optional[Criterion] = None
    item_id: Optional[str] = None
    reason: str


class ReportSection(Model):
    category: Category
    title: str
    item_ids: list[str]


class AgentReport(Model):
    run_id: str
    company: str
    ticker: Optional[str] = None
    depth: Depth
    status: Literal["not_started", "drafting", "verifying", "repairing", "final"]
    generated_at: Optional[str] = None
    sections: list[ReportSection]
    items: list[Item]
    rollup: Optional[Rollup] = None
    certainty: Optional[ReportCertainty] = None  # set when status is final
    holes: list[Hole]
    disclaimer: str
    markdown_path: Optional[str] = None
    as_of_seq: int


class ItemPatch(Model):
    text: str  # new statement/text; the backend re-verifies it


# ---------------------------------------------------------------------------
# Human report (3-page brief panel)
# ---------------------------------------------------------------------------


class SummarySentence(Model):
    id: str  # H-###
    text: str
    mode: Literal["copied", "derived"]
    # Item ids (D/C/E/G), "rollup" for the scorecard numbers, or a HOLE-## id.
    # A copied sentence has exactly one entry and must equal that item's text.
    derived_from: list[str] = Field(min_length=1)
    verification: Verification
    certainty: Optional[Certainty] = None


class ParagraphBlock(Model):
    type: Literal["paragraph"] = "paragraph"
    sentences: list[SummarySentence]


class BulletsBlock(Model):
    type: Literal["bullets"] = "bullets"
    sentences: list[SummarySentence]


class ChartBlock(Model):
    type: Literal["chart"] = "chart"
    chart_id: str
    caption: SummarySentence


class ScorecardBlock(Model):
    type: Literal["scorecard"] = "scorecard"  # render AgentReport.rollup


Block = Annotated[
    Union[ParagraphBlock, BulletsBlock, ChartBlock, ScorecardBlock],
    Field(discriminator="type"),
]


class SummarySection(Model):
    heading: str
    blocks: list[Block]


class SummaryStats(Model):
    sentences: int
    passed: int
    fixed: int
    failed: int
    words: int
    est_pages: float


class HumanReport(Model):
    run_id: str
    title: str
    company: str
    ticker: Optional[str] = None
    as_of: str
    status: Literal["not_started", "drafting", "verifying", "final"]
    sections: list[SummarySection]
    holes: list[Hole]
    disclaimer: str
    stats: SummaryStats
    certainty: Optional[ReportCertainty] = None  # set when status is final
    markdown_path: Optional[str] = None
    as_of_seq: int


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class Agent(Model):
    agent_id: str  # "bobby" for the orchestrator, "<role>-<nn>" for goons, "<agent_id>.<k>" for a rerun
    role: AgentRole
    model_id: Optional[str] = None
    status: AgentStatus
    task: str
    target_ids: list[str] = []
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    usage: Usage = Usage()
    transcript_path: Optional[str] = None
    # v0.3: display name. "Bobby" for the orchestrator, "Bobby's goon #<n>" for every other agent
    name: Optional[str] = None
    # v0.3: rerun one agent. The new agent names the agent it reran and carries the instructions
    parent_agent_id: Optional[str] = None
    instructions: Optional[str] = None
    rerun_by: Optional[Literal["analyst", "orchestrator"]] = None


class AgentRerunRequest(Model):  # v0.3: rerun one agent in the same run
    instructions: str = Field(min_length=1, max_length=4000)
    recheck: bool = True  # also re-check everything that cites the changed items


class AgentList(Model):
    agents: list[Agent]
    as_of_seq: int


# v0.2: talk to one agent. Replies come from the agent's own task and context.
# They are not checked and never enter the report or the brief: label them.
class AgentMessage(Model):
    message_id: str  # "AM-001"
    agent_id: str
    author: Literal["analyst", "agent"]
    text: str
    status: Literal["sent", "answered", "failed"]  # analyst messages go sent -> answered (or failed)
    created_at: str
    reply_to: Optional[str] = None  # an agent reply names the analyst message it answers


class AgentMessageRequest(Model):
    text: str = Field(min_length=1, max_length=2000)


class AgentMessageList(Model):
    messages: list[AgentMessage]  # oldest first


# ---------------------------------------------------------------------------
# Comments ("butt in")
# ---------------------------------------------------------------------------


class CommentRequest(Model):
    target_type: Literal["item", "source", "run", "summary_sentence"]
    target_id: Optional[str] = None
    text: str = Field(min_length=1)


class Comment(Model):
    comment_id: str
    target_type: Literal["item", "source", "run", "summary_sentence"]
    target_id: Optional[str] = None
    text: str
    status: Literal["queued", "working", "applied", "rejected"]
    response: Optional[str] = None
    changed_item_ids: list[str] = []
    created_at: str
    resolved_at: Optional[str] = None


class CommentList(Model):
    comments: list[Comment]


# ---------------------------------------------------------------------------
# Vault browser
# ---------------------------------------------------------------------------


class VaultNode(Model):
    name: str
    path: str
    type: Literal["dir", "file"]
    size: Optional[int] = None
    children: Optional[list["VaultNode"]] = None


class VaultFile(Model):
    path: str
    content_type: str
    size: int
    text: Optional[str] = None  # set for text files up to 1 MB
    truncated: bool = False
    download_url: Optional[str] = None  # presigned S3 URL for binaries


# ---------------------------------------------------------------------------
# Demo controls (label these "demo" in the UI)
# ---------------------------------------------------------------------------


class InjectErrorsRequest(Model):
    count: int = Field(default=3, ge=1, le=10)


class InjectErrorsResponse(Model):
    item_ids: list[str]
    note: str


# ---------------------------------------------------------------------------
# Events (polled from GET /runs/{run_id}/events)
# ---------------------------------------------------------------------------


class _EventBase(Model):
    seq: int
    ts: str
    run_id: str
    stage: Stage
    agent_id: Optional[str] = None
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str  # one readable line for log views


class RunCreatedData(Model):
    run: Run


class RunStatusData(Model):
    status: RunStatus
    stage: Stage


class RunStoppedData(Model):
    reason: Optional[str] = None


class RunFailedData(Model):
    error: str


class EmptyData(Model):
    pass


class PlanNeedData(Model):
    need: DataNeed


class PlanSourceData(Model):
    source: Source


class PlanReadyData(Model):
    needs: int
    sources: int


class ApprovalCommittedData(Model):
    approved: list[str]
    rejected: list[str]
    auto_approved: list[str]


class PolicyUpdatedData(Model):
    version: int
    change: str
    rule_ids: list[str]


class SourceStatusData(Model):
    source_id: str
    status: SourceStatus
    stats: SourceStats
    error: Optional[str] = None


class DocStoredData(Model):
    doc_id: str
    source_id: str
    pages: int
    vault_path: str
    scan: ScanResult


class AgentSpawnedData(Model):
    agent: Agent


class AgentStatusData(Model):
    agent_id: str
    status: AgentStatus
    task: str
    usage: Usage
    reason: Optional[str] = None  # v0.2: why, when the analyst stopped this agent


class AgentLogData(Model):
    text: str
    transcript_path: Optional[str] = None


class GatewayRequestData(Model):
    request_id: str
    role: AgentRole
    action: GatewayAction
    target: str  # URL for fetch, model id for model.invoke
    decision: GatewayDecision
    rule_id: Optional[str] = None
    reason: str
    source_id: Optional[str] = None
    latency_ms: int
    http_status: Optional[int] = None
    bytes: Optional[int] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None  # what this call cost. 0 on a cache hit
    # v0.2: demo cache. hit = served from the cache, no network or model call made.
    # miss = done live and stored in the cache. null = cache off.
    cache: Optional[Literal["hit", "miss"]] = None
    cached_at: Optional[str] = None  # when the cached copy was made (the archive date for pre-event documents)


class ItemCreatedData(Model):
    item: Item


class ItemVerifiedData(Model):
    item_id: str
    verification: Verification
    certainty: Optional[Certainty] = None


class ItemUpdatedData(Model):
    item: Item
    reason: Literal["repair", "comment", "user_edit", "demo_injection", "source_revoked", "rework"]


class ReportReadyData(Model):
    items: int
    passed: int
    fixed: int
    failed: int
    holes: int


class SummarySentenceData(Model):
    sentence: SummarySentence
    section_heading: str


class SummaryVerifiedData(Model):
    sentence_id: str
    verification: Verification
    certainty: Optional[Certainty] = None


class SummaryReadyData(Model):
    sentences: int
    words: int


class CommentEventData(Model):
    comment: Comment


class AgentMessageData(Model):  # v0.2
    message: AgentMessage


class ReworkOrder(Model):  # v0.3: Bobby sends one goon back with instructions
    agent_id: str
    instructions: str


class AgentDecisionData(Model):  # v0.3: one of Bobby's calls at a stage gate or on a comment
    agent_id: str
    stage: Stage
    decision: Literal["proceed", "rework", "escalate"]
    summary: str  # one or two plain sentences, shown in the log
    reworks: list[ReworkOrder] = []
    comment_id: Optional[str] = None  # set when the call answers an analyst comment


class RunResumedData(Model):  # v0.2
    reason: Optional[str] = None
    stage: Stage  # where the run picks up


class UsageUpdatedData(Model):
    usage: Usage


class RunCreatedEvent(_EventBase):
    type: Literal["run.created"]
    data: RunCreatedData


class RunResumedEvent(_EventBase):  # v0.2
    type: Literal["run.resumed"]
    data: RunResumedData


class AgentDecisionEvent(_EventBase):  # v0.3
    type: Literal["agent.decision"]
    data: AgentDecisionData


class AgentMessageEvent(_EventBase):  # v0.2
    type: Literal["agent.message"]
    data: AgentMessageData


class RunStatusEvent(_EventBase):
    type: Literal["run.status"]
    data: RunStatusData


class RunStoppedEvent(_EventBase):
    type: Literal["run.stopped"]
    data: RunStoppedData


class RunFailedEvent(_EventBase):
    type: Literal["run.failed"]
    data: RunFailedData


class RunCompletedEvent(_EventBase):
    type: Literal["run.completed"]
    data: EmptyData


class PlanNeedEvent(_EventBase):
    type: Literal["plan.need"]
    data: PlanNeedData


class PlanSourceEvent(_EventBase):
    type: Literal["plan.source"]
    data: PlanSourceData


class PlanReadyEvent(_EventBase):
    type: Literal["plan.ready"]
    data: PlanReadyData


class ApprovalCommittedEvent(_EventBase):
    type: Literal["approval.committed"]
    data: ApprovalCommittedData


class PolicyUpdatedEvent(_EventBase):
    type: Literal["policy.updated"]
    data: PolicyUpdatedData


class SourceStatusEvent(_EventBase):
    type: Literal["source.status"]
    data: SourceStatusData


class DocStoredEvent(_EventBase):
    type: Literal["doc.stored"]
    data: DocStoredData


class AgentSpawnedEvent(_EventBase):
    type: Literal["agent.spawned"]
    data: AgentSpawnedData


class AgentStatusEvent(_EventBase):
    type: Literal["agent.status"]
    data: AgentStatusData


class AgentLogEvent(_EventBase):
    type: Literal["agent.log"]
    data: AgentLogData


class GatewayRequestEvent(_EventBase):
    type: Literal["gateway.request"]
    data: GatewayRequestData


class ItemCreatedEvent(_EventBase):
    type: Literal["item.created"]
    data: ItemCreatedData


class ItemVerifiedEvent(_EventBase):
    type: Literal["item.verified"]
    data: ItemVerifiedData


class ItemUpdatedEvent(_EventBase):
    type: Literal["item.updated"]
    data: ItemUpdatedData


class ReportReadyEvent(_EventBase):
    type: Literal["report.ready"]
    data: ReportReadyData


class SummarySentenceEvent(_EventBase):
    type: Literal["summary.sentence"]
    data: SummarySentenceData


class SummaryVerifiedEvent(_EventBase):
    type: Literal["summary.verified"]
    data: SummaryVerifiedData


class SummaryReadyEvent(_EventBase):
    type: Literal["summary.ready"]
    data: SummaryReadyData


class CommentCreatedEvent(_EventBase):
    type: Literal["comment.created"]
    data: CommentEventData


class CommentResolvedEvent(_EventBase):
    type: Literal["comment.resolved"]
    data: CommentEventData


class UsageUpdatedEvent(_EventBase):
    type: Literal["usage.updated"]
    data: UsageUpdatedData


Event = Annotated[
    Union[
        RunCreatedEvent,
        RunStatusEvent,
        RunStoppedEvent,
        RunFailedEvent,
        RunCompletedEvent,
        RunResumedEvent,
        PlanNeedEvent,
        PlanSourceEvent,
        PlanReadyEvent,
        ApprovalCommittedEvent,
        PolicyUpdatedEvent,
        SourceStatusEvent,
        DocStoredEvent,
        AgentSpawnedEvent,
        AgentStatusEvent,
        AgentLogEvent,
        AgentMessageEvent,
        AgentDecisionEvent,
        GatewayRequestEvent,
        ItemCreatedEvent,
        ItemVerifiedEvent,
        ItemUpdatedEvent,
        ReportReadyEvent,
        SummarySentenceEvent,
        SummaryVerifiedEvent,
        SummaryReadyEvent,
        CommentCreatedEvent,
        CommentResolvedEvent,
        UsageUpdatedEvent,
    ],
    Field(discriminator="type"),
]

EVENT_TYPES = [
    "run.created", "run.status", "run.stopped", "run.failed", "run.completed", "run.resumed",
    "plan.need", "plan.source", "plan.ready",
    "approval.committed", "policy.updated",
    "source.status", "doc.stored",
    "agent.spawned", "agent.status", "agent.log", "agent.message", "agent.decision",
    "gateway.request",
    "item.created", "item.verified", "item.updated", "report.ready",
    "summary.sentence", "summary.verified", "summary.ready",
    "comment.created", "comment.resolved",
    "usage.updated",
]


class EventsPage(Model):
    run_id: str
    events: list[Event]
    last_seq: int  # pass back as ?after= on the next poll
    run_status: RunStatus
    stage: Stage


# ---------------------------------------------------------------------------
# Errors and health
# ---------------------------------------------------------------------------


class ErrorBody(Model):
    code: Literal[
        "not_found", "invalid_state", "validation_error", "conflict",
        "policy_denied", "internal",
    ]
    message: str
    details: dict = {}


class ErrorResponse(Model):
    error: ErrorBody


class Health(Model):
    ok: bool
    version: str
    mode: Literal["mock", "live"]


VaultNode.model_rebuild()

# Everything the generator should export to JSON Schema / TypeScript.
EXPORTED = [
    Run, CreateRunRequest, RunList, StopRequest, ResumeRequest, RerunRequest,
    DataNeed, Source, Plan, AddSourceRequest, SourceDecision, ApprovalRequest,
    ApprovalResponse, RevokeRequest, RevokeResponse,
    PolicyRule, RoleScope, Policy,
    Document, DocumentList, DocumentPage,
    Citation, Verification, CheckResult, DiffSegment, Attempt,
    DataItem, Conclusion, Extrapolation, Chart, AgentReport, ItemPatch,
    Rollup, CategoryScore, Hole, Certainty, ReportCertainty,
    SummarySentence, HumanReport,
    Agent, AgentList, AgentMessage, AgentMessageRequest, AgentMessageList, AgentRerunRequest,
    CommentRequest, Comment, CommentList,
    VaultNode, VaultFile,
    InjectErrorsRequest, InjectErrorsResponse,
    EventsPage,
    ErrorResponse, Health,
]
