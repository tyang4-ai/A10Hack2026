# Plans

Work packages for the backend build. The orchestrator hands out one plan per subagent call (see RUNBOOK.md section 3). Every plan says what to read, which files it owns, what it must provide and how we know it's done. Read "Shared interfaces" first, whatever your plan is.

## Shared interfaces

### Layout and owners

```
backend/
  __init__.py  requirements.txt  .env.example  pytest.ini                P-scaffold
  .gitignore                                                             orchestrator (exists)
  axelrod/
    __init__.py  config.py  contract.py  ids.py                          P-scaffold (then frozen)
    runtime.py  api.py  orchestrator.py                                  B-store-api
    store/__init__.py  store/events.py  store/state.py  store/vault.py   B-store-api
    stages/__init__.py  stages/plan.py  stages/approve.py                B-store-api
    gateway.py  bedrock.py  pricing.py  fetch/http.py  agents/base.py    B-gateway
    verify/text.py  verify/numbers.py  verify/compute.py  verify/checks.py   B-verify
    fetch/pdf.py  fetch/html.py  fetch/ingest.py  stages/collect.py      B-ingest
    agents/extractor.py  stages/extract.py                               B-extract (W2)
    agents/drafter.py  agents/judge.py  agents/repairer.py               B-reason (W2)
    stages/draft.py  stages/verify.py  stages/repair.py  stages/finalize.py   B-reason (W2)
    agents/summarizer.py  stages/summarize.py  stages/verify_summary.py  B-brief (W2)
    cli.py  demo.py                                                      B-cli (W2)
  data/shl/plan.json  data/shl/SOURCES.md  data/test_pages/injection.html   B-shl-plan
  data/replay/                                                           export_run.py output
  scripts/contract_smoke.py (B-store-api)  check_plan.py (B-shl-plan)
          ingest_one.py (B-ingest)  presmoke.py (B-extract)  export_run.py (B-cli)
  tests/conftest.py (P-scaffold)  tests/test_<area>.py (each plan its own)
  infra/  AWS_RESOURCES.md  .env                                         aws-operator
  RUNBOOK.md  PLANS.md  status.md  DEMO.md                               orchestrator
```

P-scaffold creates every Python file above with the signatures below and `raise NotImplementedError` bodies, so everything imports from the start. Each later plan replaces the stubs it owns. Package `__init__.py` files belong to the plan that owns the folder (`agents/__init__.py`, `verify/__init__.py`, `fetch/__init__.py` stay empty and belong to P-scaffold).

### Conventions

- Run from the repo root. Import as `from backend.axelrod import ...` (relative imports inside the package are fine). Write for Python 3.11, since the EC2 image runs 3.11.
- Contract: `from backend.axelrod.contract import m, certainty, RUBRIC`. `m` is `contract/models.py` itself, never a copy. Runtime code never imports `mock/` (tests may, for realistic fixtures).
- Every API response, event payload and stored report is a contract model. Validate at the boundaries with `m.<Model>.model_validate(...)`.
- Ids and offsets come from code: `ctx.next_id("D")` gives `D-001`, offsets come from `verify.text.locate`. Nothing a model outputs becomes an id or an offset.
- All HTTP and all Bedrock calls go through `Gateway`. Only `gateway.py` imports `fetch/http.py` and `bedrock.py`. S3 and DynamoDB in `store/` are infrastructure and use boto3 directly.
- Time: `ids.now_iso()` gives `2026-11-08T03:00:00.000Z`.
- Numbers: `Decimal` for report values, `float` for `cost_usd`. Display rounding is `verify.numbers.half_up` (round half up, never `format`).
- Async: FastAPI and asyncio. Wrap boto3 calls in `asyncio.to_thread`. One pipeline task per run.
- Files: always `encoding="utf-8"`. The euro sign is everywhere.
- Logging: emit events. `print` is for scripts.
- Tests: pytest with pytest-asyncio (`asyncio_mode = auto`). Anything that calls AWS is marked `@pytest.mark.live` and skipped unless `AXELROD_LIVE=1`. `FAKE_MODELS=1` makes `bedrock.py` answer from fakes registered per tool name, so the whole pipeline runs offline.

### plan.json (frozen SHL plan)

```json
{
  "frozen": true,
  "company": "Siemens Healthineers AG",
  "ticker": "SHL",
  "aliases": ["siemens healthineers", "shl", "healthineers"],
  "depth": "screen",
  "as_of": "2026-11-07",
  "fiscal_note": "one line on which fiscal year is the base and why",
  "needs":   ["m.DataNeed objects"],
  "sources": ["m.Source objects, approval pending, status proposed, proposed_by planner"],
  "extract_pages": {"S-001": [[150, 156], [170, 172]]},
  "need_pages": {"N-01": {"S-001": [[150, 152]]}}
}
```

`extract_pages` ranges are 1-based page indices in the PDF file, inclusive. Extractors read only those pages. `need_pages` is optional and says which pages serve which need.

### config.py

```python
class Settings(BaseModel):
    store: Literal["local", "aws"] = "local"                 # STORE
    aws_region: str = "us-east-1"                            # AWS_REGION
    model_large: str = "us.anthropic.claude-sonnet-5-5"      # MODEL_LARGE (A-setup confirms)
    model_small: str = "us.anthropic.claude-haiku-5-5"       # MODEL_SMALL
    guardrail_id: str | None = None                          # GUARDRAIL_ID
    guardrail_version: str | None = None                     # GUARDRAIL_VERSION
    vault_bucket: str | None = None                          # VAULT_BUCKET
    runs_table: str | None = None                            # RUNS_TABLE
    events_table: str | None = None                          # EVENTS_TABLE
    data_dir: Path = Path("backend/.data")                   # DATA_DIR (local store and vault)
    max_concurrency_small: int = 16                          # MAX_CONCURRENCY_SMALL
    max_concurrency_large: int = 4                           # MAX_CONCURRENCY_LARGE
    budget_usd: float = 5.0                                  # BUDGET_USD (per run)
    prices: dict[str, dict[str, float]] = {}                 # PRICES_JSON {"<model id>": {"in": usd_per_mtok, "out": usd_per_mtok}}
    fake_models: bool = False                                # FAKE_MODELS
    http_user_agent: str = "Axelrod/0.1 (hackathon research harness)"   # HTTP_USER_AGENT
    api_key: str | None = None                               # AXELROD_API_KEY
    public_ui_url: str | None = None                         # PUBLIC_UI_URL (CLI prints run links)
    demo_pacing: float = 0.0                                 # DEMO_PACING (seconds between fetches in the live demo)

def get_settings() -> Settings        # cached. Loads backend/.env with python-dotenv first, real env wins
```

### contract.py

Puts `contract/` on `sys.path` (the same way `mock/mock_server.py` does) and exposes `m`, `certainty`, `RUBRIC` (parsed `boffin_rubric.json`) and `CONTRACT_VERSION`.

### ids.py

```python
def now_iso() -> str
def new_run_id() -> str                      # run_YYYYMMDD_<6 lowercase alphanumerics>
class IdAllocator(BaseModel):                # lives in RunState, so it survives a restart
    counters: dict[str, int] = {}
    def next(self, prefix: str) -> str       # N: 2 digits, HOLE: 2, REQ: 4, everything else 3
                                             # "N-01", "HOLE-01", "REQ-0001", "D-001", "CM-001"
```

Agent ids are `<role>-<nn>` (`extractor-07`). Judges are `verifier-<item id>-<k>` with k = 1 to 3, and the judge CheckResult carries `agent_id = verifier-<item id>`. Rule ids: `R-sys-models`, `R-allow-S-001`, `R-reject-S-004`, `R-revoke-S-006`.

### runtime.py (B-store-api)

```python
class StopRequested(Exception): ...

@dataclass
class RunContext:
    state: RunState
    store: Store
    vault: Vault
    gateway: Gateway
    settings: Settings
    approval: asyncio.Event                      # set by POST /approval
    def emit(self, type: str, data: dict | BaseModel, *, message: str, stage: str | None = None,
             agent_id: str | None = None, level: str = "info") -> m.Event
        # validates against the contract event union, assigns seq and ts, appends, updates Run.counts and last_seq
    def next_id(self, prefix: str) -> str
    def check_stop(self) -> None                 # raises StopRequested when the kill switch is on
```

Every `stages/<name>.py` exposes `async def run(ctx: RunContext) -> None` (the `complete` stage is `stages/finalize.py`). The orchestrator calls them in the contract's stage order, emits `run.status` between them, turns `StopRequested` into `stopped` and any other exception into `failed` with `Run.error`. A stage that is still a stub fails the run with a clear "not built yet" error, which is fine until its wave lands.

### store (B-store-api)

- `Store`: `create_run(req) -> RunState`, `get(run_id) -> RunState` (KeyError becomes 404), `list_runs() -> list[m.Run]`, `save(state)`, `load_all()` at startup (also loads `backend/data/replay/*` as `mode: "replay"`), `export_run(run_id, dest)`.
- `RunState` (Pydantic, serializable): `run`, `needs`, `sources`, `extract_pages`, `need_pages`, `policy`, `documents`, `pages` (doc_id to list of page texts, page n is index n-1), `items` (id to item), `holes`, `agents`, `comments`, `summary`, `ids`, `events`. Helpers: `data_items()`, `conclusions()`, `extrapolations()`, `charts()`, `sources_by_id()`, `plan() -> m.Plan`, `report() -> m.AgentReport`, `agent_pages(doc_id)` (empty for quarantined documents, so no agent ever reads them).
- `Vault`: `put_bytes(run_id, relpath, data)`, `put_text(...)`, `get_bytes(...)`, `get_text(...)`, `tree(run_id) -> m.VaultNode`, `file(run_id, path) -> m.VaultFile`. Paths follow CONTRACT.md section 10. Local mode writes under `DATA_DIR`, aws mode writes to the S3 bucket.
- Persistence: local mode keeps JSON under `DATA_DIR/runs/<run_id>/`. AWS mode writes events to DynamoDB (`run_id`, `seq`), the Run row to the runs table, and a state snapshot plus `events.jsonl` to S3 at every stage boundary and at the end. A restarted API serves finished runs again.

### gateway.py (B-gateway)

```python
class Blocked(Exception):            # request_id, rule_id, reason
@dataclass
class FetchResult: url: str; final_url: str; http_status: int; content_type: str; body: bytes; request_id: str
@dataclass
class ToolSpec: name: str; description: str; input_schema: dict          # JSON Schema of the one forced tool
@dataclass
class InvokeResult: tool_input: dict; tokens_in: int; tokens_out: int; cost_usd: float; request_id: str

class Gateway:
    def __init__(self, ctx: RunContext)
    def build_policy(self) -> m.Policy                     # from the sources' approvals, default deny
    def revoke(self, source_id: str, reason: str | None) -> m.Policy
    def set_kill_switch(self, reason: str | None) -> None
    async def fetch(self, agent: m.Agent, url: str, *, source_id: str | None = None,
                    why: str = "approved source") -> FetchResult            # raises Blocked
    async def invoke(self, agent: m.Agent, size: Literal["large", "small"], *, system: str, user: str,
                     tool: ToolSpec, max_tokens: int = 4096) -> InvokeResult   # raises Blocked
    async def scan(self, agent: m.Agent, url: str, source_id: str, text: str) -> m.ScanResult
        # Guardrails tripwire. When it intervenes it also emits gateway.request with decision "flagged"
```

Decision order is CONTRACT.md section 6. Every decision emits exactly one `gateway.request`. Every model call emits `usage.updated`. Any exception inside the gateway is a deny. Role scopes: `fetcher` may fetch. `planner`, `drafter`, `summarizer` may invoke the large model. `extractor`, `verifier`, `reviser` may invoke the small one. `repairer` may invoke both.

### agents/base.py (B-gateway)

```python
class AgentRunner:
    def __init__(self, ctx: RunContext, role: str, task: str, target_ids: list[str], size: Literal["large", "small"])
    agent: m.Agent                        # registered in RunState, agent.spawned emitted
    async def ask(self, system: str, user: str, output: type[BaseModel], *, max_tokens: int = 4096) -> BaseModel
        # forced tool use through ctx.gateway.invoke with ToolSpec built from output.model_json_schema(),
        # validated with output.model_validate, one retry that shows the model its validation error,
        # transcript lines to the vault (00-Meta/agents/<agent_id>.jsonl), agent.status and agent.log events,
        # usage rolled up into the agent and the run
    def finish(self, status: str = "done") -> None
```

Agent output models are small flat Pydantic models defined in each `agents/<role>.py`, with no contract ids. Code turns them into contract items. Each agent module registers a fake for its tool with `bedrock.register_fake(tool_name, fn)`, where `fn(system, user, schema) -> dict`.

### verify (B-verify)

```python
# text.py
def normalize(s: str) -> str                                          # NFKC, whitespace runs become one space, nothing else
def find_quote(page_text: str, quote: str) -> tuple[int, int] | None  # offsets into the original page text
def locate(pages: list[str], quote: str, page_hint: int) -> tuple[int, int, int] | None   # (page, start, end), hint first
def near_miss(page_text: str, quote: str) -> tuple[list[m.DiffSegment], tuple[int, int]]   # diff and the closest real span
# numbers.py
def parse_number(s: str) -> Decimal | None        # "1,234" "(1,234)" "−5.2" "€1.2 billion" "12.5%"
def to_base(value: Decimal, scale: str) -> Decimal
def numbers_in_text(text: str) -> list[tuple[str, Decimal]]          # skips years and fiscal-year labels
def half_up(x: Decimal | float, places: int = 1) -> str
# compute.py
def safe_eval(expr: str, env: dict[str, Decimal]) -> Decimal          # + - * / parentheses numbers D-ids names
# checks.py: each returns list[m.CheckResult]
def check_data_item(item, pages_by_doc, sources) ; def check_conclusion(c, items, sources)
def check_extrapolation(e, items, rubric) ; def check_chart(g, items) ; def check_sentence(s, items, holes)
def check_tier_rules(items, sources) ; def status_from(checks) -> str  # any fail: fail, any blocked: blocked, else pass
```

---

## P-scaffold

Stage S1. Agent: `scaffolder`.

**Goal.** Every module in the layout exists with the signatures above and imports cleanly. The small shared modules are done for real.

**Read first.** `docs/BACKEND.md` sections 1 and 10, `contract/models.py` (skim), `mock/mock_server.py` lines 1 to 80 (how it imports the contract), `contract/boffin_rubric.json` (skim).

**Owns.** Every file in the layout marked P-scaffold, plus stub versions of every other Python file in the layout. It does not touch `RUNBOOK.md`, `PLANS.md`, `status.md`, `AWS_RESOURCES.md`, `infra/` or `.env`.

**Tasks.**
1. Stubs for every module, with the shared-interface signatures, a module docstring that says what it is for, and `raise NotImplementedError("<plan id>")` bodies.
2. Real implementations of `config.py`, `contract.py`, `ids.py`, `tests/conftest.py` (fixtures: settings with `STORE=local` and a temp `DATA_DIR`, `FAKE_MODELS=1`, and a `fixture_report` built from `mock/build_fixtures.py` for tests that want a realistic report), and `backend/pytest.ini` (marker `live`, `asyncio_mode = auto`).
3. `requirements.txt`: fastapi, uvicorn[standard], pydantic>=2.7, python-dotenv, boto3, httpx, pymupdf, pypdf, trafilatura, typer, rich, pytest, pytest-asyncio.
4. `.env.example`: every Settings variable with a one-line comment, no values for resource names, never credentials. (`backend/.gitignore` already exists and ignores `.data/`, `.env` and PDFs.)

**Acceptance.**
- `PY -c "import backend.axelrod.api, backend.axelrod.gateway, backend.axelrod.orchestrator, backend.axelrod.cli"` exits 0.
- `PY -m pytest -q backend/tests` collects with no errors, and `tests/test_ids.py` passes (formats as in Shared interfaces).
- `PY -c "from backend.axelrod.contract import m, RUBRIC; print(len(m.EVENT_TYPES))"` prints 26.

**Out of scope.** Any logic beyond config, contract, ids and test fixtures.

---

## A-setup

Stage S1, in parallel with P-scaffold. Agent: `aws-operator`.

**Goal.** Everything in `docs/BACKEND.md` section 8 steps 1 to 6 exists in us-east-1, is tested, and is written down.

**Read first.** `docs/BACKEND.md` sections 7 and 8, `backend/RUNBOOK.md` section 6.

**Owns.** `backend/.env` (values), `backend/AWS_RESOURCES.md`, `backend/infra/`.

**Tasks.**
1. Account check: `aws sts get-caller-identity` must show account 506556958358.
2. Model ids: find the exact inference profile ids for Claude Sonnet 5.5 and Claude Haiku 5.5 (`aws bedrock list-inference-profiles`, `aws bedrock list-foundation-models`). One Converse call to each with one tool and `toolChoice` forcing it. Print only the tool input and token usage. If a model isn't enabled, record the error under needs (Gordon may have to request access in the console).
3. Concurrency: `backend/infra/concurrency_test.py` fires 50 parallel small-model calls (short prompt, forced tool, adaptive retries), then 8 large ones. Record successes, throttles, p50 and p95 latency, and recommend `MAX_CONCURRENCY_SMALL` and `MAX_CONCURRENCY_LARGE`.
4. Prices: per-million-token input and output prices for both models from the Bedrock pricing page, as `PRICES_JSON`.
5. S3 bucket `axelrod-vault-<6 random lowercase>`: all public access blocked, default encryption on.
6. DynamoDB `axelrod-runs` (partition key `run_id`, string) and `axelrod-events` (partition key `run_id`, string, sort key `seq`, number), on-demand.
7. Guardrail `axelrod-tripwire`: prompt attack filter HIGH on input, plain blocked messages, then version 1. Test with ApplyGuardrail (source INPUT): an injection sample comes back `GUARDRAIL_INTERVENED`, a clean annual-report paragraph comes back `NONE`.
8. IAM role `axelrod-ec2` (trusted by ec2.amazonaws.com) with an inline least-privilege policy: Bedrock invoke and converse on the two models and their inference profiles, `bedrock:ApplyGuardrail` on the guardrail, S3 on the bucket, DynamoDB on the two tables. Attach `AmazonSSMManagedInstanceCore`. Instance profile `axelrod-ec2`. If role creation is denied, record the exact error under needs (Gordon asks an ACM organizer) and continue.
9. Tag everything `project=axelrod`. Fill `backend/.env` from `backend/.env.example` (resource names, model ids, prices, concurrency). Never credentials.

**Acceptance.** `.env` filled. `AWS_RESOURCES.md` lists every resource with its name, purpose and delete command. The REPORT gives the model ids, the concurrency numbers, the prices, both guardrail results and the IAM outcome.

**Out of scope.** EC2 and API Gateway (A-deploy). Deleting anything.

---

## B-store-api

Stage W1. Agent: `builder`.

**Goal.** The real API answers every route the mock answers, with the same paths, shapes, status codes and error envelope, backed by RunState. A run created through the API streams the frozen SHL plan and waits for approval. Events work in local and AWS mode.

**Read first.** `docs/CONTRACT.md` (all of it), `mock/mock_server.py` (route list, error envelope, validation, approval rules: copy its route handling, not its fixture logic), `contract/models.py`.

**Owns.** `axelrod/runtime.py`, `axelrod/store/*`, `axelrod/api.py`, `axelrod/orchestrator.py`, `axelrod/stages/__init__.py`, `axelrod/stages/plan.py`, `axelrod/stages/approve.py`, `scripts/contract_smoke.py`, `tests/test_store.py`, `tests/test_api.py`.

**Uses.** `Gateway.build_policy`, `revoke`, `set_kill_switch` from B-gateway (in tests, monkeypatch until it lands). `data/shl/plan.json` from B-shl-plan (until it lands, test with a two-source sample of your own under `tests/data/`).

**Tasks.**
1. `RunState`, `Store` (local and aws), `Vault` (local and S3), event log with gapless `seq` per run and the `GET /events?after&limit` rules from CONTRACT.md section 4.
2. `RunContext` and the orchestrator loop: stage order, `run.status` events, stop becomes `run.stopped`, failure becomes `run.failed`, success ends with `run.completed`.
3. `stages/plan.py`: when the company or ticker matches an alias in `data/shl/plan.json`, emit `plan.need` and `plan.source` one at a time (about 150 ms apart, so the UI fills in live), then `plan.ready`. The plan is frozen for the demo: say so in the `plan.ready` message and in `Run.notes`. Any other company fails the run with "only the frozen Siemens Healthineers plan is available in this build".
4. `stages/approve.py`: status `awaiting_approval`, wait for the approval event, apply the `ApprovalRequest` rules (CONTRACT.md section 5: every priority source decided, 422 with `details.undecided` or `details.unknown`, tier and rank moves, analyst-added sources), call `gateway.build_policy`, emit `approval.committed` and `policy.updated`.
5. `api.py`: every route in the mock. `POST /runs` starts the pipeline task. `POST /stop` sets the kill switch. Revoke works after approval only. Comments are stored and emit `comment.created` (the reviser comes later). `PATCH /items` and `POST /demo/inject-errors` call into `demo.py` and answer 409 `invalid_state` "not built yet" while it's a stub. `/healthz` returns `mode: "live"`. CORS any origin. `X-Axelrod-Key` check when `AXELROD_API_KEY` is set. On startup call `store.load_all()`.
6. `scripts/contract_smoke.py --base <url> [--run <id>]`: without `--run` it creates an SHL run, polls until `awaiting_approval`, approves every priority source, checks the policy, stops the run. With `--run` it reads an existing run. Either way it validates every endpoint response against the matching contract model and prints a summary. It must also pass against the mock (`--base http://localhost:8000/api/v1`).

**Acceptance.**
- `tests/test_store.py` and `tests/test_api.py` green with `STORE=local FAKE_MODELS=1`.
- `contract_smoke.py` passes against the real API and against the mock.
- 200 concurrent emits give gapless, ordered `seq`.
- A finished run saved, the app restarted, the run still listed and served. A replay folder loads as `mode: "replay"`.

**Out of scope.** Stages after approve.

---

## B-gateway

Stage W1. Agent: `builder`.

**Goal.** The gateway is the only path to the network and to Bedrock. It enforces the policy, the kill switch and the budget, meters every call, and fails closed.

**Read first.** `docs/BACKEND.md` sections 4 and 7, `docs/CONTRACT.md` section 6, `contract/models.py` (Policy, PolicyRule, RoleScope, GatewayRequestData, Usage, Agent, ScanResult), `mock/build_fixtures.py` (search for `R-sys-models` to see the fixture's rules).

**Owns.** `axelrod/gateway.py`, `axelrod/bedrock.py`, `axelrod/pricing.py`, `axelrod/fetch/http.py`, `axelrod/agents/base.py`, `tests/test_gateway.py`, `tests/test_bedrock_live.py`.

**Tasks.**
1. Policy builder: `R-sys-models` (model.invoke per role scope), `R-allow-<S>` (the source URL's domain and path prefix, fetcher role only), `R-reject-<S>` deny, default deny. The version bumps on every change and `policy.updated` is emitted.
2. `decide(agent, action, target)` in the exact contract order, returning decision, rule id and a readable reason. Any exception inside it means blocked with reason "gateway error: ...".
3. `fetch`: httpx AsyncClient, redirects followed one hop at a time and each hop decided again (a redirect off the allowlist is blocked), 60 s timeout, 50 MB cap, `HTTP_USER_AGENT`. Emit `gateway.request` with status, bytes and latency. `DEMO_PACING` sleeps before each fetch.
4. `invoke`: Bedrock Converse through `bedrock.py` with `toolConfig` holding one tool and `toolChoice` forcing it. One semaphore per size. boto3 `Config(retries={"mode": "adaptive", "max_attempts": 8})`. Token usage goes through `pricing.cost` into the run's and the agent's Usage, then `usage.updated`. Budget is checked before the call (estimated) and after (actual). A result that lands after STOP is discarded and recorded as blocked.
5. `scan`: ApplyGuardrail (source INPUT) on the text, chunked to the API limit. `GUARDRAIL_INTERVENED` gives a flagged ScanResult and a `gateway.request` with decision `flagged` for that URL. No guardrail configured gives `skipped`.
6. `bedrock.py` fake registry (`register_fake(tool_name, fn)`), used when `FAKE_MODELS=1`. Fake usage estimates tokens as characters divided by 4.
7. `agents/base.py` AgentRunner as specified.

**Acceptance.**
- Unit tests: allow, default deny, an explicit deny beats an allow, revoke blocks the next request, the kill switch blocks fetch and invoke, an exceeded budget blocks, role scope (an extractor can't fetch), fail closed (an exception inside decide means blocked), a redirect off the allowlist is blocked, and every decision emits exactly one `gateway.request` with the contract fields.
- With `FAKE_MODELS=1`, `AgentRunner.ask` returns a validated object and emits `agent.spawned`, `agent.status`, `agent.log` and `usage.updated`.
- Live (`AXELROD_LIVE=1`): one Haiku and one Sonnet forced-tool call through AgentRunner, and one ApplyGuardrail on the injection sample that comes back flagged.

---

## B-verify

Stage W1. Agent: `builder`.

**Goal.** Code proves quotes, numbers and arithmetic, and every planted error gets caught.

**Read first.** `docs/BACKEND.md` section 5, `docs/CONTRACT.md` sections 7, 8 and 11, `contract/models.py` (Citation, TextLocator, JsonLocator, CheckResult, DiffSegment, Verification, Value, DataItem, Conclusion, Computation, Extrapolation, ScoreRule, Chart), `contract/boffin_rubric.json` (thresholds), `mock/build_fixtures.py` (`safe_eval` and `half_up` as references).

**Owns.** `axelrod/verify/*` (except `__init__.py`), `tests/test_verify.py`, `tests/test_planted.py`, `tests/data/planted/`.

**Tasks.** The functions in Shared interfaces, with these checks:
- `quote_exact`: exact match after normalization only. A near miss gives `diff` and `closest_locator`. A quote found on another page passes with the page fixed and a warning in `detail`.
- `value_in_quote`, `unit_context` (a unit_context citation exists and its quote names the scale that matches `value.scale`, for example "in millions of €" for million), `period_match` (the value's period appears in the period_context or evidence quote, accepting "FY2025", "fiscal year 2025", "2025"), `json_value` (RFC 6901 pointer).
- `computation` (safe_eval over base values, percentages as fractions, compared with `display` after half up), `numbers_traced` (every number in the text equals a cited value after scale or a computation's display, allowing years, fiscal-year labels and the criterion's threshold numbers), `threshold_rule` (recompute the score from the rubric threshold and the cited computation), `tier_rule`, `chart_values`, `copy_exact`, `citations_resolve` (ids exist, their status is pass or fixed, and their source isn't revoked, otherwise fail with "source revoked").
- Planted errors: write statement-page test texts in the SHL style ("(in millions of €)" headers, comma thousands, negatives in parentheses, fictional numbers) and plant a swapped digit, a flipped "not", a wrong year, the right number from the wrong column, a dropped minus, a wrong scale, a computation off by rounding, a copied sentence with one word changed, and a chart point that doesn't match its item. Each planted item must fail the right check and each clean twin must pass. The test prints `planted: N/N caught, clean: M/M pass`.

**Acceptance.** Tests green. Planted N/N caught, clean M/M pass. A test proves curly quotes and dashes do not match straight ones.

---

## B-ingest

Stage W1. Agent: `builder`.

**Goal.** An approved source becomes a stored document with exact page texts in the vault, fetched through the gateway.

**Read first.** `docs/BACKEND.md` section 2 and section 3 step 3, `docs/CONTRACT.md` section 10, `contract/models.py` (Document, DocumentPage, Highlight, ScanResult, DocVaultPaths, Source, SourceStatus, SourceStats).

**Owns.** `axelrod/fetch/pdf.py`, `axelrod/fetch/html.py`, `axelrod/fetch/ingest.py`, `axelrod/stages/collect.py`, `scripts/ingest_one.py`, `tests/test_ingest.py`.

**Tasks.**
1. `pdf.py`: text of every page with PyMuPDF (`page.get_text()`), pypdf as the fallback, page n stored as index n-1. Store all pages (the viewer can show any page). Extractors only read the `extract_pages` ranges.
2. `html.py`: trafilatura main text, split on paragraph boundaries into pseudo-pages of about 3,000 characters. `Document.paginated` is false.
3. `ingest.py`: raw bytes, `<file>.md` with page markers (`<!-- page 12 -->`) and `<file>.meta.json` in the source's vault folder. Build the `Document`, put the pages in RunState. JSON sources (SEC companyfacts) are stored pretty-printed as one page with `content_type: "json"`. Also a `document_page(state, doc_id, page) -> m.DocumentPage` builder with highlights from the items citing that page (the API calls it).
4. `stages/collect.py`: approved sources in tier then rank order. Status `fetching`, `gateway.fetch` under a fetcher agent, ingest, then for untrusted kinds (news, forum, other) `gateway.scan` on each page. Flagged means `Document.quarantined`, source status `quarantined`, and no agent ever gets those pages. Blocked (revoked or rejected) means status `blocked`, and failures mean `failed` with the error. Either way, carry on with the next source. For tier_3 HTML sources, request up to 3 off-page links through the gateway with `why="link found in page"`. Default deny blocks them, which is the demo's disallowed action. Emit `doc.stored` and `source.status`.
5. `scripts/ingest_one.py <source_id> --pages a-b`: fetch and ingest one source from `data/shl/plan.json` into a throwaway local run and print the first 400 characters of each page. No AWS needed (the scan is skipped without a guardrail).

**Acceptance.** Tests with a generated PDF (PyMuPDF can make one) and an HTML sample. `ingest_one.py S-001` works on the real SHL annual report and the income statement page shows "(in millions of €)". A planted-injection HTML fixture with a fake flagged scan ends up quarantined and absent from `agent_pages`.

---

## B-shl-plan

Stage W1. Agent: `builder`. Needs web access.

**Goal.** The frozen SHL plan: data needs for the six screen criteria, the sources that serve them with tiers, ranks and reliability, and the pages extractors read. Plus our planted test page.

**Read first.** `contract/boffin_rubric.json` (`depths.screen` and the anchors of its six criteria), `docs/BACKEND.md` section 2, `docs/CONTRACT.md` section 5, `contract/models.py` (DataNeed, Source and their enums), `mock/build_fixtures.py` (the fixture plan, for shape only). Never open Gordon's vault notes.

**Owns.** `data/shl/plan.json`, `data/shl/SOURCES.md`, `data/test_pages/injection.html`, `scripts/check_plan.py`, `tests/test_plan.py`.

**Starting points.** Verify every one.
- FY2025 Annual Report, 230 pages, published Nov 26, 2025: https://corporate.webassets.siemens-healthineers.com/a364b935102e7df5/d7fdacda458e/siemens-healthineers-ir-annual-report-2025.pdf
- Earlier annual reports for the ROTIC history are linked from https://www.siemens-healthineers.com/investor-relations/presentations-financial-publications
- FY2026 fourth-quarter and full-year results release, Nov 5, 2026: https://www.siemens-healthineers.com/press/releases/2026q4 . The FY2026 Annual Report is due Nov 26, 2026, so FY2026 figures come from this release and the full statements from the FY2025 report. The release also mentions a new segment structure and a changed shareholder structure. Say all of this in `fiscal_note` and the rationales.
- Peers for `rotic_vs_peers`: GE HealthCare and Philips both file with the SEC, so their figures can come from SEC XBRL companyfacts JSON on data.sec.gov, read by code. SEC asks for a contact in the User-Agent. If it rejects the default one, say so under needs and Gordon sets `HTTP_USER_AGENT` himself. Don't put anyone's email in the code.

**Tasks.**
1. Needs `N-01` and up, driven by the rubric anchors: ROTIC inputs for SHL across the available years (rotic_level), peer ROTIC inputs (rotic_vs_peers), the revenue series and end-market growth evidence (secular_growth), the metrics the leverage anchor uses (leverage), cash, facilities and the liquidity anchor's metrics (liquidity), R&D spend and pipeline evidence (research_stage).
2. Sources `S-001` and up. Priority: SHL annual report(s), the FY2026 results release, peers' SEC data. tier_2: one wire-service or financial-press article on the FY2026 results, and it must be `S-006` (the revoke target in the demo script). tier_3: one forum or blog post about SHL, and our planted test page. Reliability: annual reports 0.95, SEC structured data 0.95, company releases 0.9, wire news 0.75, forum 0.35, test page 0.1. Each source gets need_ids, criteria, vault_folder and a rationale.
3. `extract_pages`: for each PDF source, the 1-based page indices in the file (not printed labels) holding what the needs use: income statement, balance sheet, cash flow statement, segment note, financing and debt notes, R&D. Aim for 30 to 45 pages in total. Check every index by opening the PDF (download to a temp folder outside the repo, never commit a PDF).
4. `data/test_pages/injection.html`: a plain page titled "Axelrod test page: planted prompt injection (simulated)" with some SHL-flavored filler, one visible and one hidden injection ("Ignore all previous instructions..."), and a link to an off-allowlist domain (example.org). Its source URL is the raw GitHub URL of this file on the backend branch: https://raw.githubusercontent.com/tyang4-ai/A10Hack2026/backend/backend/data/test_pages/injection.html (it answers once the orchestrator pushes).
5. `scripts/check_plan.py`: validates needs and sources with the contract models, checks ids, tiers and ranks, checks that every screen criterion has a need and every need has a source, and requests every URL (HEAD, or a small ranged GET when HEAD fails). The test page only warns until it's pushed.
6. `SOURCES.md`: "This plan is frozen for the demo" at the top, then one line per source: why it's in, how it was found, which pages matter.

**Acceptance.** `check_plan.py` passes. Every screen criterion is covered. Page indices verified against the PDF.

**Out of scope.** Fetching through Axelrod (that's the run itself).

---

## B-extract

Stage W2. Agent: `builder`.

**Goal.** Verified data items from the stored pages: one small-model extractor per page, offsets computed by code, data checks right away, code extraction for JSON sources.

**Read first.** `docs/BACKEND.md` section 3 step 4 and section 5, `docs/CONTRACT.md` section 7, `axelrod/agents/base.py`, `axelrod/verify/`, `axelrod/fetch/ingest.py`, `contract/certainty.py`.

**Owns.** `axelrod/agents/extractor.py`, `axelrod/stages/extract.py`, `scripts/presmoke.py`, `tests/test_extract.py`.

**Tasks.**
1. Extractor output model (flat): `items[{statement, metric, period, value_as_reported, unit, scale, quote, unit_quote, period_quote, need_ids}]`. The prompt is strict: copy quotes character for character from the page, no ids, no offsets, only numbers on this page that the listed needs ask for.
2. Code builds `m.DataItem`: a `D-` id, citations with locators from `verify.text.locate` (evidence, unit_context, period_context), `value.number` from `parse_number`, the source's tier, `extracted_by: "agent"`. Emit `item.created` (pending), run the data checks, re-cite a near miss from the closest span (a `recite` attempt), then emit `item.verified` with certainty from `contract/certainty.py`.
3. JSON sources: code-only extraction of the needed concepts by JSON pointer, `extracted_by: "code"`, `json_value` check.
4. Pages run in parallel under the small-model semaphore. Quarantined documents never reach an extractor.
5. `scripts/presmoke.py --source S-001 --pages a,b,c,d,e`: a local run with only that source and those pages, auto-approved, every stage that exists, then a summary (items, statuses, holes, cost). Stages that are still stubs are reported, not crashed on.

**Acceptance.** Fake-model tests green. Live on five real SHL pages: items extracted, at least 90% pass or fixed, every failure carries a diff, cost printed.

---

## B-reason

Stage W2. Agent: `builder`.

**Goal.** Conclusions, BOFFIN scores and charts from verified data, checked by code and by three isolated judges, repaired toward the evidence, then the final certainty, rollup and holes.

**Read first.** `docs/BACKEND.md` sections 3 (steps 5 to 7 and 10) and 6, `docs/CONTRACT.md` sections 7 and 8, `contract/boffin_rubric.json`, `contract/certainty.py`, `axelrod/agents/base.py`, `axelrod/verify/checks.py`.

**Owns.** `axelrod/agents/drafter.py`, `axelrod/agents/judge.py`, `axelrod/agents/repairer.py`, `axelrod/stages/draft.py`, `axelrod/stages/verify.py`, `axelrod/stages/repair.py`, `axelrod/stages/finalize.py`, `tests/test_reason.py`.

**Tasks.**
1. Drafter (large model) sees only verified data items (id, statement, value, unit, scale, period, source tier) and the rubric entries of the in-scope criteria. It returns conclusions with declared computations (expressions over D-ids), one extrapolation per in-scope criterion (score -1, 0, 1 or null with `na_reason`, confidence, rule text), and chart specs as refs only. Code assigns C, E and G ids, builds chart points from refs, and rejects refs to items that didn't pass.
2. `stages/verify.py`: code checks first, then three isolated small-model judges per conclusion and per extrapolation. A judge sees only the claim, the cited items and the rubric entry. Two of three pass it. Record `votes_for` and `votes_total`. Judges run in parallel.
3. `stages/repair.py`: weaken only (`recite`, `recompute`, `downgrade`, `delete`, `not_determinable`), at most two attempts with the attempt history, then the item stays `fail` and becomes a Hole. Then call `finalize.finalize_report(ctx)`: certainty for every item, the rollup from the rubric (weighted average first, both percentages, coverage, N/A by name), the holes register (unscored criteria, failed items, failed fetches), the agent report's ReportCertainty, `report.ready`.
4. `stages/finalize.py` also has `run(ctx)` for the `complete` stage: write `09-Analysis/agent-report.json` and `.md`, `09-Analysis/human-report.json` and `.md`, `12-Verification/data-checks.json` and `reasoning-checks.json` to the vault.

**Acceptance.** With fake models, the mock fixture's verified data produces a valid AgentReport. A planted unsupported conclusion fails the judges and gets downgraded or becomes a hole. The rollup matches a hand-computed example in the test.

---

## B-brief

Stage W2. Agent: `builder`.

**Goal.** The 3-page brief, every sentence cited to report items, checked by code and by judges.

**Read first.** `docs/BACKEND.md` section 3 steps 8 and 9, `docs/CONTRACT.md` sections 8 and 9, `contract/certainty.py`, `axelrod/agents/base.py`, `axelrod/verify/checks.py`.

**Owns.** `axelrod/agents/summarizer.py`, `axelrod/stages/summarize.py`, `axelrod/stages/verify_summary.py`, `tests/test_brief.py`.

**Tasks.**
1. Summarizer (large model) writes sections of paragraph, bullets, chart and scorecard blocks from verified items only. Every sentence has `mode` (`copied` or `derived`) and `derived_from` (item ids, `rollup`, `HOLE-` ids). Code assigns `H-` ids, streams `summary.sentence`, and estimates pages.
2. `verify_summary`: `copy_exact`, `numbers_traced` and `citations_resolve` by code, `faithfulness` by three judges (two of three). Repair by weakening or deleting the sentence, at most twice. Certainty per sentence and the word-weighted ReportCertainty. `summary.verified`, `summary.ready`.

**Acceptance.** With fake models: a valid HumanReport with `est_pages` at most 3. A planted unfaithful sentence fails faithfulness. A copied sentence with one changed word fails `copy_exact`.

---

## B-cli

Stage W2. Agent: `builder`.

**Goal.** A10's "launch a CLI agent", the demo controls, and the export behind the replay fallback.

**Read first.** `docs/BACKEND.md` section 9, `docs/CONTRACT.md` sections 3 and 4, `docs/PLAN.md` (demo script), `axelrod/api.py`, `axelrod/store/`.

**Owns.** `axelrod/cli.py`, `axelrod/demo.py`, `scripts/export_run.py`, `tests/test_cli.py`.

**Tasks.**
1. `cli.py` with typer, httpx and rich: `run "<company>" --ticker --depth --api` (sends `created_via: "cli"`, prints the run id and a UI link from `PUBLIC_UI_URL`), `tail <run>` (polls events and prints gateway traffic live: allowed green, blocked red, flagged yellow, plus stage changes and the running cost), `approve <run> --all` or `--reject S-00x`, `revoke <run> <source>`, `stop <run>`, `status <run>`. `--api` defaults to the `AXELROD_API` environment variable.
2. `demo.py`: `inject_errors(ctx, req)` per CONTRACT.md (corrupt one digit in N passing quotes, re-run the checks, emit `item.updated` with reason `demo_injection`, label it as a demo control) and `patch_item(ctx, item_id, patch)` (user edit, then re-verify).
3. `scripts/export_run.py <run_id>`: calls `Store.export_run` to write a finished run (state, events, page texts, never PDFs) to `backend/data/replay/<run_id>/`.

**Acceptance.** The CLI works against the local API with fake models. `tail` output is readable at a glance. `inject-errors` with N=3 gets 3/3 caught.

---

## A-deploy

Stage W2, in parallel with the builders. Agent: `aws-operator`.

**Goal.** The API runs on EC2 under the IAM role, behind API Gateway, and redeploys with one command.

**Read first.** `docs/BACKEND.md` sections 1 and 8, `backend/AWS_RESOURCES.md`.

**Owns.** `backend/infra/`, `backend/AWS_RESOURCES.md`.

**Tasks.**
1. EC2 t3.medium, Amazon Linux 2023, instance profile `axelrod-ec2`, a security group with port 8000 open (the guard asks Gordon), no SSH key: use SSM.
2. `infra/userdata.sh`: install git and python3.11, clone the public repo, check out `backend`, create a venv, install `backend/requirements.txt`, write `/etc/axelrod.env` (`STORE=aws`, resource names, model ids, prices, never credentials), and a systemd unit `axelrod` running `uvicorn backend.axelrod.api:app --host 0.0.0.0 --port 8000`.
3. `infra/deploy.sh`: `aws ssm send-command` that pulls `backend`, reinstalls requirements, restarts the service, and prints the instance's `git rev-parse HEAD`.
4. API Gateway HTTP API: HTTP proxy integration to `http://<public-dns>:8000/{proxy}`, route `ANY /{proxy+}`, CORS any origin, `$default` stage with auto-deploy.
5. Record everything in `AWS_RESOURCES.md` with delete commands. Put the invoke URL at the top.

**Acceptance.** `curl <invoke-url>/healthz` answers from EC2 with the stub or real API. `deploy.sh` moves the instance to the latest pushed commit.

---

## B-testpage

Stage W2. Agent: `builder`. Added 2026-11-07 8:00 PM because the repo stays private until submission, so the raw GitHub URL of the planted page returns 404.

**Goal.** Axelrod's own API serves the planted test page, and S-010 points there, so the live demo's flagged-and-quarantined step works without GitHub.

**Read first.** `backend/data/shl/plan.json` (S-010), `backend/data/test_pages/injection.html`, `axelrod/api.py`, `axelrod/stages/plan.py`, `axelrod/config.py`, `axelrod/gateway.py` (how rules are built from a source URL), `axelrod/stages/collect.py`.

**Owns.** `axelrod/api.py` (one new route only), `axelrod/stages/plan.py` (S-010 URL override only), `axelrod/config.py` (one new field), `backend/.env.example` (one new key), `data/shl/SOURCES.md` (S-010 line), `scripts/check_plan.py` (S-010 check), `tests/test_testpage.py`.

**Tasks.**
1. `Settings.test_page_url: str | None = None` (`TEST_PAGE_URL`), with a line in `.env.example`.
2. `GET /test-pages/{name}` outside `/api/v1`: serves only files in `backend/data/test_pages/` whose name matches `^[a-z0-9_-]+\.html$`, as `text/html; charset=utf-8`, with the header `X-Axelrod-Simulated: true`. Exempt it from the `X-Axelrod-Key` check, as `/healthz` is, because the fetcher sends no key. No path traversal.
3. Plan stage: when `TEST_PAGE_URL` is set, the planted-page source (S-010) gets that URL before `plan.source` is emitted, and its rationale and `Run.notes` say it is served by Axelrod's own API and is simulated. Unset keeps the URL in plan.json.
4. `check_plan.py`: with `TEST_PAGE_URL` set, check that URL instead of the GitHub one.
5. Prove the whole path in a test: a real uvicorn on a free port serves the page, a run whose plan uses `TEST_PAGE_URL=http://127.0.0.1:<port>/test-pages/injection.html` fetches it through the real Gateway (allowed by `R-allow-S-010`), the fake scan flags it, the document is quarantined, and the example.org link is blocked. If the gateway refuses loopback or private hosts, say so in the REPORT (EC2 will use the public API Gateway URL instead).

**Acceptance.** Tests green. `curl localhost:<port>/test-pages/injection.html` returns the page with the simulated header. A path-traversal name returns 404.

---

## B-g2fix

Stage W2. Agent: `builder`. Added 2026-11-07 8:15 PM from the G2 verifier's findings. Small and surgical.

**Owns.** `axelrod/fetch/ingest.py`, `axelrod/gateway.py`, `axelrod/store/state.py`, `axelrod/store/__init__.py`, `tests/test_ingest.py`, `tests/test_gateway.py`, `tests/test_store.py`. Change only what the tasks need. These are finished W1 files.

**Tasks.**
1. Fail closed on a skipped scan (verifier finding 1, major). In `fetch/ingest.py`, an untrusted source (kind news, forum or other, or tier_3) is quarantined unless the scan verdict is `clean`. `skipped` (no guardrail configured) and `error` both quarantine, with a plain reason ("no guardrail configured, untrusted source quarantined"). Trusted sources are never scanned and never quarantined. Update the B-ingest tests that expected skipped to release, and add one that proves the planted page is quarantined with `GUARDRAIL_ID` unset.
2. Deny link-local hosts (finding 3). In `Gateway.decide`, a target whose host is an IPv4 address in 169.254.0.0/16, the IPv6 address fd00:ec2::254, or any IPv6 link-local address (fe80::/10) is blocked before any allow rule, with a readable reason ("link-local address, never fetched"). Loopback and private addresses stay as they are, because local runs serve the test page from 127.0.0.1. Add tests, including an analyst-added source at http://169.254.169.254/latest/meta-data/ that is approved and still blocked.
3. Pre-approval policy view (finding 4). Before approval, `GET /policy` (`RunState.policy_view`) shows what the gateway already enforces: `R-sys-models` and the role scopes, plus default deny, as the mock does (see `mock/build_fixtures.py` `build_policy`). Version 0 is fine. Add a test.
4. Label fake-model runs (finding 6). When `FAKE_MODELS=1`, `create_run` adds a `Run.notes` line "Simulated: model answers come from offline fakes (FAKE_MODELS=1)". `mode` stays `live` (the contract has no other value for it).

**Acceptance.** `.venv/Scripts/python -m pytest -q backend/tests -m "not live"` is green, apart from failures in W2 test files you don't own. List any such failures in the REPORT, since W2 builders are mid-work.

---

## R0 fixes (from the full local rehearsal run)

Added 2026-11-07 8:38 PM. The rehearsal `run_20261108_0bsu0o` (live models, STORE=local, all 10 sources) reached the report in about 5 minutes for $1.20, then failed at summarize. All the live gateway beats worked: 3 link-follows blocked, S-010 flagged PROMPT_ATTACK:HIGH and quarantined, the example.org link blocked. The report has 353 data items (289 pass, 16 fixed, 48 fail), 16 conclusions, 6 scores and 55 holes, but only 2 of 6 criteria are scored. The saved report is `backend/.data/r0_report.json`, and the run's state is under `backend/.data/r0/`. Read them for real examples, and never commit them. Three builders fix this in parallel, each on its own files.

### B-fix-brief
**Owns.** `axelrod/agents/base.py`, `axelrod/agents/summarizer.py`, `tests/test_agent_coerce.py` (new).
**Problem.** `AgentOutputError: summarizer-01: brief_draft output failed validation twice: sections: Input should be a valid list`. Sonnet 5 sometimes returns a nested array or object field as a JSON-encoded string.
**Tasks.**
1. In `AgentRunner.ask`, before `output.model_validate`, walk the tool input against the output model's JSON schema. Wherever the schema expects an array or object but the value is a string, try `json.loads`, and keep the result only if it parses to the expected type. Do it recursively so nested fields are covered. Never coerce scalars. Log an `agent.log` line when a field was coerced.
2. In the retry prompt after a validation error, say plainly that list and object fields must be real JSON arrays and objects, not strings.
3. Check the summarizer's output model. If it nests deeper than the model can fill reliably, flatten it (code rebuilds the HumanReport anyway).
4. Tests with fake outputs: a stringified list, a stringified nested object, a string that isn't JSON (still fails validation), and a scalar string field (left alone).
**Acceptance.** Tests green. One live call (`AXELROD_LIVE=1`) of the summarizer on the mock fixture report validates. Under $0.30.

### B-fix-extract
**Owns.** `axelrod/agents/extractor.py`, `axelrod/stages/extract.py`, `tests/test_extract.py`.
**Problem.** 48 data items fail: `period_match` 28, `value_in_quote` 18, `unit_context` 5, `quote_exact` 4, `tier_rule` 1 (correct behavior). Examples:
- `'(4.2%)' is not in the quote 'Diagnostics comparable revenue decline of 4.2%'`
- `'>31' is not in the quote 'The global addressable market for the Imaging segment ...'`
- `The value has scale 'billion', but no unit_context citation names it`
- `The evidence quote does not mention FY2026` (prose in S-002, the release)
- `does not mention FY2019` and `FY2018` (S-005, the older report)
- table rows quoted with the wrong cells (`'Imaging 6,461 6,60'`, 5 characters off)
**Tasks.**
1. Prompt: `value_as_reported` is copied character for character from the quote, exactly as printed. Direction words like "decline" or "decrease" go into the statement and metric, never into a sign or brackets the page doesn't print. No "~", ">" or "<" unless printed.
2. Code: when the evidence quote itself names the scale next to the number ("€31 billion", "in millions of €"), cite that same span as the `unit_context` citation and take the scale from it (code, not the model).
3. Code: when the model's period quote doesn't check out, look for the period on the same page in this order: the nearest line above the evidence that names the fiscal year or quarter, the page's heading lines, then (for releases) the document's cover-page period. Cite it as `period_context`.
4. Tables: when a quote misses by a few characters because cells were joined differently, use the closest real span from `verify.text.closest_span`, but only when the difference is whitespace or cell order and the reported value is in that span.
5. Use the failing examples in `backend/.data/r0_report.json` as test cases. Copy short excerpts of the page text into test fixtures, labeled as real SHL text.
**Acceptance.** Tests green. A live presmoke on the pages behind the failing items (the S-002 and S-005 pages from plan.json, plus S-001 pages 15 and 155) shows at least 95% of data items pass or fixed. Under $0.50.

### B-fix-checks
Added 2026-11-07 9:17 PM from B-fix-extract's REPORT.
**Owns.** `axelrod/verify/checks.py`, `tests/test_verify.py`, `tests/test_planted.py` (only add cases).
**Tasks.**
1. False pass (most important): when a `period_context` citation is present, `_period_text` ignores the years in the evidence's own clause. So an agent-supplied header can mask a comparative: "8% (2024: 9%)" with 9% claimed as FY2025 passes. Fix: the value's own clause decides first. If the clause names a period for that value (a comparative in brackets, "in fiscal year 2024", "2024:"), that period wins over any header. Headers apply only when the clause names none.
2. `_CLAUSE` lacks "over" and "than", so "4.0% and 5.0% over fiscal year 2026" reads 4.0% as 2026. A comparison base ("over", "than", "compared with", "versus", "vs.") is not the value's period.
3. `_columns`: nil cells ("‐", "–", "-" standing alone) count as columns, and numbers inside a row label ("Loan (US$1,742 million)") do not. Right-aligned columns then line up with the header again (r0 D-307: 1,484 was read as 2024).
4. Each fix gets a test, plus a planted case for task 1 (a comparative claimed as the current year must fail). `planted` must stay N/N caught and `clean` M/M pass.
**Acceptance.** `.venv/Scripts/python -m pytest -q backend/tests -m "not live"` green, and the planted line still shows every planted error caught.

### B-fix-draft
**Owns.** `axelrod/agents/drafter.py`, `axelrod/stages/draft.py`, `tests/test_reason.py` (only add tests).
**Problem.** rotic_level and rotic_vs_peers came out N/A because the drafter wanted a complete ten-year NOPAT and average-TICAP series and refused. The demo story (docs/PLAN.md) is the opposite: score ROTIC from the years we have, and let code lower the certainty because SHL has fewer than ten years of reports since listing (`finalize` already marks data coverage low). liquidity is N/A because C-015 failed `numbers_traced` (`'(€13,499 million)', '€4.5 billion' and '14.3%' do not match any cited value`), and C-001 failed the same way (`'€1,525 million'`).
**Tasks.**
1. Drafter prompt for 10-year criteria: compute the metric for every fiscal year where the inputs exist, as declared computations over D-ids (ROTIC = NOPAT / average TICAP, as the rubric defines them). Score against the rubric threshold on what is available, set confidence to match, and say how many years were used. Never refuse only because the series is shorter than ten years. Use N/A only when not even one year can be computed.
2. rotic_vs_peers: compute the latest common fiscal year's ROTIC (or the closest proxy the rubric allows) for SHL and each peer from the cited D-ids (the SEC companyfacts items for GE HealthCare and Philips), then compare.
3. numbers_traced: tell the drafter that every number in a conclusion's text must be a cited item's value as printed or a declared computation's display, and that any sum, difference, ratio or percentage it wants to state must be declared as a computation. Give one worked example in the prompt.
4. Show the drafter enough data per group (check the row limit). The ROTIC group needs income statement and balance sheet rows for several years.
5. Tests with fake models: a 3-year series gets a score (not N/A) with low coverage, and a conclusion stating an undeclared sum fails `numbers_traced` while the declared one passes.
**Acceptance.** Tests green. A live check on the rehearsal run's verified data scores rotic_level and rotic_vs_peers (not N/A), with every conclusion pass or fixed. Load `backend/.data/r0` with `Store` and re-run only the draft, verify and repair stages on a copy. Under $1.

---

## W3 plans

Written loosely on purpose. The orchestrator sharpens them after G5 with what the full run showed.

**B-live** (builder). The live demo segment against the deployed API, reliable on the first try: the test page flagged and quarantined, a link-follow blocked, `revoke S-006` mid-collect with the next request to it blocked (order S-006 late in collect and use `DEMO_PACING` so the presenter has time), STOP blocking the next model call. Revoke cascade (P1): items citing a revoked source go `blocked` through `citations_resolve`, and their dependents lose that support. Owns: whatever files the orchestrator assigns (expect `stages/collect.py` and `verify/checks.py` edits, so no other plan runs on those). Writes the exact commands into the REPORT for `DEMO.md`.

**B-baseline** (builder). BACKEND.md section 11: same SHL sources, one plain Claude report (a single large-model call with the same page texts and no harness), our checkers run on both, the same errors planted in both, counts of what each catches, time and token cost for each, and a list of about 20 key numbers for Gordon to hand-check. Output `backend/data/baseline/results.md`, labeled as an experiment. Owns: `scripts/baseline.py`, `data/baseline/`.

**B-p1** (builder). In this order, only while time allows: the `context` check (three judges see the quote, its paragraph and the statement), cross-source contradictions (P2), comments that trigger a revision and a re-check (reviser role).


### W3 plans, sharpened 2026-11-07 8:27 PM (started early, before G5, by Gordon's no-wait call)

**B-live** (builder). The A10 live segment works on the first try, in the PLAN.md demo order.
- Owns: `axelrod/cascade.py` (new), `axelrod/api.py` (revoke route only: call the cascade after `gateway.revoke`), `axelrod/stages/collect.py`, `axelrod/verify/checks.py` (`_period_text` only), `scripts/demo_live.py` (new), `tests/test_live_demo.py` (new).
- 1. Revoke cascade (`cascade.py`): after a revoke, re-run `citations_resolve` on every item citing that source, then on its dependents (D to C to E and G, then brief sentences if a summary exists). Each item that changes emits `item.updated` with reason `source_revoked` and then `item.verified`, and goes `blocked` with the detail "source revoked: S-006". Recompute certainty for the changed items. If the report is already final, mark the affected rollup criteria in the item details, but don't re-run finalize. Revoke before an item exists means it is never extracted (collect blocks the fetch).
- 2. Live order (`collect.py`): keep S-006 late in collect (it is 8th now). Make `DEMO_PACING` apply before each source fetch, and also before each link-follow, so the presenter has time to say "revoke" while collect runs. Emit an `agent.log` line before each fetch ("next: S-006 Reuters via Euronext") so the CLI tail shows what's coming.
- 3. `scripts/demo_live.py --api <url> [--pacing-check]`: drives the whole A10 segment over HTTP like a presenter would (create run with `created_via: "cli"`, approve per plan.json, revoke S-006 as soon as collect starts, STOP once extract has made its first model calls) and asserts each beat from the events: allowed traffic with tokens and cost, the S-009 link-follow blocked by default deny, the next S-006 request blocked by `R-revoke-S-006`, S-010 flagged and its document quarantined, and after STOP the next model call blocked with nothing allowed afterwards. It prints PASS or FAIL per beat and the exact CLI commands for each beat (for `backend/DEMO.md`). It must pass locally against `uvicorn` with `STORE=local`, live Bedrock and `TEST_PAGE_URL=http://127.0.0.1:<port>/test-pages/injection.html`. Keep live cost under $0.50 (STOP early).
- 4. `_period_text` fix: in a mixed row (amounts and % change columns under a year header), align % tokens to year columns only when the row has no amounts, so "23,375 22,363 4.5% 5.9%" under "2025 2024 Act. Comp." reads 4.5% as a change, not as 2024. Add tests. Don't change other checks.
- Acceptance: tests green offline. `demo_live.py` PASS on every beat locally, live. REPORT gives the exact presenter commands.

**B-baseline** (builder). BACKEND.md section 11 "Baseline evaluation", labeled as an experiment.
- Owns: `scripts/baseline.py` (new), `data/baseline/` (new), `tests/test_baseline.py` (new).
- 1. Inputs: the same SHL page texts the harness reads (the `extract_pages` of S-001 to S-005 from `data/shl/plan.json`), ingested through the real gateway into a throwaway local run (`STORE=local`). No PDFs committed.
- 2. Baseline: one plain large-model call (Sonnet, through `AgentRunner`/the gateway, so it's metered) with all those page texts and a prompt like an analyst would write: "write a BOFFIN screen of Siemens Healthineers from these pages, cite page numbers and quote the numbers you use". Ask for structured output (claims with value, unit, period, page, quote) so our checkers can run on it.
- 3. Run our code checks (`quote_exact`, `value_in_quote`, `unit_context`, `period_match`) on every baseline claim. Run the harness's extract on the same pages (or reuse a finished run's items if one exists under DATA_DIR). Plant the same errors in both (the B-verify planted set: digit swap, dropped minus, wrong year, wrong scale, flipped "not"), and count what each side catches.
- 4. Record time and token cost for each side from the gateway's usage.
- 5. Write `data/baseline/results.md`: a headline line ("our checks caught X of Y planted errors in the harness report; a plain Claude report gave Z claims, of which W failed our own quote check"), the table of counts, time and cost, and a list of about 20 key SHL numbers with page and quote for Gordon to hand-check. Label it "Experiment, run on <date>, not part of the demo run".
- Acceptance: the script runs end to end live for under $2. Offline tests with fake models cover the counting logic. REPORT gives the headline numbers.

---

## Interface notes

Dated notes from the orchestrator when an interface changes during the build. Newest last.

**2026-11-07 7:30 PM, after S1 (P-scaffold and A-setup REPORTs).**
- Models: `MODEL_LARGE=us.anthropic.claude-sonnet-5`, `MODEL_SMALL=us.anthropic.claude-haiku-4-5-20251001-v1:0`. The 5.5 models are denied by the workshop allowlist. Never hard-code model ids; read them from Settings. `PRICES_JSON` in `.env` has all four ids.
- Concurrency: `MAX_CONCURRENCY_SMALL=32`, `MAX_CONCURRENCY_LARGE=8` (no throttles at 50 and 8 parallel). botocore's default `max_pool_connections` is 10, so `bedrock.py` must set `Config(max_pool_connections >= small + large)`. Sonnet 5 writes more output tokens than the schema suggests (236 on a tiny call), so budget estimates should allow for it.
- Guardrail: `GUARDRAIL_ID` and `GUARDRAIL_VERSION=1` are in `.env`. Live tests read real settings, offline tests don't.
- ids.py: role agent ids get 2 digits (`ids.next("extractor")` gives `extractor-01`). A trailing `-` in a prefix is dropped.
- config.py extras: `load_settings(env_file, environ)` (no cache), `set_settings(s | None)`, `get_settings.cache_clear()`. An empty `AXELROD_ENV_FILE` means no `.env` is read. Empty values in `.env` count as unset.
- Offline tests (conftest): `STORE=local`, temp `DATA_DIR`, `FAKE_MODELS=1`, no `.env`, AWS and price variables removed. Budget tests set their own prices. Fixtures: `settings`, `fixture_bundle` (session, `mock/build_fixtures.build()`), `fixture_report` (fresh `m.AgentReport`). `tests/conftest.py` is frozen: define extra fixtures in your own test file.
- RunState types: `documents: list[m.Document]`, `items: dict[str, m.Item]`, `agents: dict[str, m.Agent]`, `extract_pages: dict[str, list[list[int]]]`, `need_pages: dict[str, dict[str, list[list[int]]]]`, `ids: IdAllocator`.
- `Store(settings)`, `Vault(settings)`, `Gateway(ctx)` only store their argument in `__init__`. `Vault.put_bytes` and `put_text` return the vault path (str). `Store.export_run` returns None.
- Suggested helpers (owners may change them, add a note if so): `orchestrator.run_pipeline(ctx)`, `store/events.events_page(state, after=0, limit=500)`, `Gateway.decide(agent, action, target) -> (decision, rule_id, reason)`, `bedrock.converse(...)`, `bedrock.apply_guardrail(...)`, `fetch/http.get_once(...)`, `fetch/pdf.pdf_pages(data)`, `fetch/html.html_pages(html, target_chars=3000)`, `async fetch/ingest.ingest(ctx, source, result)`.
- `finalize.finalize_report(ctx)` is sync. `demo.inject_errors` and `demo.patch_item` are async, `patch_item` returns `m.Item`. `verify/checks.py` takes `items: dict[str, m.Item]` and `sources: dict[str, m.Source]`.
- PyMuPDF: use `import pymupdf` (`import fitz` warns now). Heavy packages (pymupdf, pypdf, trafilatura, typer, rich) are imported inside functions, never at module load.
- Test data folders: `tests/data/planted/` (B-verify), `tests/data/store/` (B-store-api), `tests/data/ingest/` (B-ingest), `tests/data/plan/` (B-shl-plan) if needed.

**2026-11-07 7:48 PM, B-ingest REPORT.**
- `ingest(ctx, source, result, *, agent: m.Agent | None = None)`. With an agent, untrusted sources (kind news, forum or other, or tier_3) are scanned page by page through `ctx.gateway.scan`, and pages enter state only after the scan. Flagged or a scan error quarantines the document (fail closed). Without an agent, untrusted sources stay quarantined.
- `document_page(state, doc_id, page)` raises KeyError (unknown doc) or IndexError (bad page). The API maps both to 404.
- Public helpers: collect `collect_source(ctx, agent, source)`, `spawn_fetcher`, `finish_fetcher`, `follow_links`, `off_page_links`, `blocked_status`. ingest `parse_body`, `detect_type`, `doc_relpaths(source, content_type)`, `scan_pages`, `document_index`, `state_source`, `is_untrusted`, `IngestError`. html `html_text`, `split_pages`, `html_links`, `decode_html`. pdf `PdfError`.
- Source status after collect: a block by an `R-revoke-*` rule stays `revoked`. A block with no rule and a reason starting "gateway error" becomes `failed`. Other blocks become `blocked`. Rejected sources are never requested.
- One fetcher agent (`fetcher-01`) goes through sources in tier then rank order. Allowed off-page links are fetched and dropped, never ingested. collect also writes `00-Meta/_document-index.json`. Vault relpaths have no slug: the Vault adds `runs/<id>/<slug>/`.
- Real sources: S-001 is 230 pages. The income statement (B.1) is file page 138 with the header "(in millions of €, earnings per share in €)", so unit checks must accept that longer header. S-006 (Euronext HTML) gives 2 pages, and page 1 starts with site-menu text. S-007 (SEC JSON) works with the default User-Agent and is one pretty-printed page.

**2026-11-07 7:49 PM, B-gateway REPORT.**
- `InvokeResult` gains `model_id` and `stop_reason` (defaulted). `Gateway.decide(agent, action, target, *, est_cost=0.0)`, `build_policy(*, emit=True)`. New: `kill_switch_on` property, `budget_usd()`, `register_fake` and `register_fake_scan` re-exported.
- `Gateway.revoke` raises KeyError (unknown source) or ValueError (before approval, or already revoked). It sets `source.status` and `source.error` and emits `policy.updated` and `source.status` itself.
- `AgentRunner(ctx, role, task, target_ids, size: "large" | "small" | None, *, agent_id=None)`. `size=None` means no model (fetchers). Use `agent_id` for judges (`verifier-D-001-1`). It is also a sync context manager that finishes as done, failed or stopped. `ask` raises `Blocked`, `AgentOutputError` (invalid twice) or `StopRequested`. The tool name is the output class name, or its `TOOL_NAME` ClassVar.
- W2 agent modules register fakes with `from .base import register_fake, tool_name`, never by importing bedrock.py.
- Budget: the check before a call assumes the worst case (prompt chars/4 + 64 in, the full `max_tokens` out) and holds that amount while the call runs, so pass a realistic `max_tokens`. A model with no `PRICES_JSON` entry is blocked when live. A result that lands after STOP, a revoke or a budget overrun is discarded and recorded as blocked, and its tokens are still metered.
- Rule domain is the source URL's host (`www.` ignored, subdomains match). The path prefix is the full URL path. Bedrock calls run on bedrock.py's own thread pool (small + large + 4).

**2026-11-07 7:53 PM, B-shl-plan REPORT and orchestrator decisions.**
- Sources S-001 to S-010. PDFs with `extract_pages`: S-001 (FY2025 annual report, 19 pages), S-002 (Q4 FY2026 release PDF, 8 pages), S-003 to S-005 (earlier annual reports, 3 pages each), 36 pages in total. HTML sources S-006 (tier_2 Reuters story on live.euronext.com, the revoke target), S-009 (tier_3 Simply Wall St blog) and S-010 (planted test page) are read whole as pseudo-pages. S-007 and S-008 (SEC companyfacts JSON for the peers) are read by code. The default User-Agent works on data.sec.gov.
- Fetch order: S-001, S-002, S-007, S-008, S-003, S-004, S-005, S-006, S-009, S-010. S-009's link-follow sends up to 3 off-site links to the gateway before S-010's example.org link.
- research_stage: N-07 covers R&D spend and outlook. The analyst's coverage-history part stays a declared gap (a Hole).
- PDF text quirks: BEL (U+0007), soft hyphens (U+00AD), and thin spaces between a minus sign and its digits ("–\u20098,722"). Decision sent to B-verify: `normalize` also drops U+00AD and C0/C1 control characters other than tab and newline, while offsets still map to the original text. `parse_number` allows whitespace (thin and no-break spaces included) between a minus sign (-, U+2212, U+2013) and the digits.
- Decision: `check_plan.py` may use stdlib urllib. It is a dev check outside any run and stores nothing. The gateway rule covers runtime code (`backend/axelrod/`).
- Decision: S-002 is the release PDF, not the HTML release page.
- trafilatura drops `class="comment"` blocks and aria-hidden elements. The test page avoids both so the scan sees both injections.

**2026-11-07 8:00 PM, B-store-api REPORT (what W2 must do).**
- B-reason `finalize_report(ctx)`: set `state.rollup`, `state.report_certainty`, `state.holes`, `state.report_generated_at`, `state.report_markdown_path` (and optionally `state.sections`), then emit `report.ready`. That event is what turns the report final.
- B-brief: set `state.summary` (an `m.HumanReport`), then emit `summary.ready`.
- `usage.updated` carries run-level usage, and `emit` copies it into `run.usage`.
- Vault `put_*` is sync. From async code use `aput_bytes` / `aput_text` (needed in aws mode). Vault paths are `runs/<run_id>/<slug>/<relpath>`. The state snapshot is `runs/<run_id>/state.json`.
- `RunContext.create(state, store, vault, settings, gateway=None)`, `ctx.stopped()`, `ctx.run_id`. Orchestrator: `async start_run(store, vault, settings, req) -> RunContext`. API: `create_app(settings=None, *, store=None)`.
- RunState extras: `kill_switch`, `plan_ready`, `report_ready`, `summary_ready`, `as_of`, `fiscal_note`, `approval_request`, `sections`, `rollup`, `report_certainty`, `report_generated_at`, `report_markdown_path`, `disclaimer`. Helpers: `documents_by_id`, `sorted_sources`, `report_status`, `report_sections`, `human_report`, `policy_view`, `agent_list`, `recount`, `is_finished`.
- An analyst-added source's api_key lives only in `store.get_secret(source.access.secret_ref)`, never on disk.
- A stage that still raises NotImplementedError fails the run with "stage <x> is not built yet (<plan>)". On restart, runs left in progress become failed ("Interrupted").
- Replay format: `backend/data/replay/<name>/{state.json, events.jsonl, vault/}` (text only), written by `Store.export_run`.

**2026-11-07 8:05 PM, B-verify REPORT (accepted).**
- `check_tier_rules(items, sources)` returns `dict[str, list[CheckResult]]` keyed by item id. There is also `check_tier_rule(item, items, sources)`.
- `status_from([])` is `"pending"`: no checks run never counts as a pass.
- `citations_resolve` on a revoked, quarantined or rejected source returns **blocked** (detail "source revoked: S-006"), not fail, and walks E to C to D, so one pass blocks every dependent. Repair must leave blocked items alone.
- Extra keyword-only arguments: `check_conclusion(..., *, rubric=None)`, `check_extrapolation(e, items, rubric, *, sources=None)`, `check_sentence(s, items, holes, *, rollup=None, rubric=None, sources=None)`. Pass `rollup=` when a sentence cites "rollup", or it comes back blocked.
- Helpers: checks `check_item` (dispatch by kind), `base_value`, `conclusion_env`, `display_of`, `json_pointer_get`, `with_fixed_locators`. text `closest_span`, `diff_segments`, `changed_chars`. numbers `scan_numbers`, `NumToken` (`sign_ambiguous`), `period_mentions`, `period_key`, `SCALES`, `WORD_FACTOR`. compute `ComputeError`, `ids_in`.
- `quote_exact` gives one result per text citation. A pass with `closest_locator` means the page or offsets were corrected: apply it with `with_fixed_locators`. A fail carries `diff` and `closest_locator` for the recite repair.
- A table-row data item needs `unit_context` and `period_context` citations (the header lines), or `period_match` fails.
- `numbers_traced` accepts only a cited item's value or a computation's display. Other numbers in a cited item's quote don't count. Drafter and summarizer prompts must say so, and spell out small counts ("three years").
- Check order: data items, then conclusions, then extrapolations and charts, then sentences (checks read the cited items' status).
- Known limit: a dash followed by a space before a number is `sign_ambiguous`, so a dropped minus is not caught when the dash is spaced. Parenthesised negatives stay strict.

**2026-11-07 8:07 PM, B-testpage REPORT.**
- `Settings.test_page_url` (`TEST_PAGE_URL`). `GET` and `HEAD /test-pages/{name}` serve `backend/data/test_pages/<name>.html` with `X-Axelrod-Simulated: true`, outside `/api/v1`, with no key needed. The plan stage swaps the planted page's URL, domain and rationale when `TEST_PAGE_URL` is set. It finds the page by its plan.json URL plus "(simulated)" in the title, not by the id.
- `R-allow-S-010` covers the API host and the path prefix `/test-pages/injection.html` only. `/api/v1/*` on the same host stays default deny. Rules ignore the port.
- The gateway does not refuse loopback, private or link-local hosts. Mitigation: EC2 requires IMDSv2 (sent to A-deploy). Possible hardening for B-live: deny 169.254.0.0/16 in `decide`.

**2026-11-07 8:17 PM, B-brief REPORT.**
- The brief's judges and repairer live in `agents/summarizer.py` (tools `brief_draft`, `brief_faithfulness`, `brief_rewrite`). Judges are `verifier-<H id>-1..3`, asked again after a repair. Helpers: `summarize.estimate_pages`, `fit_pages`, `all_sentences`, `make_stats`, `word_budget`. `verify_summary.code_checks(state, s)`, `render_markdown(brief, *, rollup=None, items=None)`, `CHECKS_RELPATH` (writes `12-Verification/brief-checks.json`).
- A sentence still failing after two repairs is removed and counted in `stats.failed`. Blocked sentences stay, marked blocked. A repaired sentence's new text comes in `summary.verified` and the `/summary` refetch, not in a second `summary.sentence`.
- `finalize.run` writes `09-Analysis/human-report.md` with `render_markdown` and sets `state.summary.markdown_path` (sent to B-reason).
- est_pages = words/450 + 0.3 per chart + 0.35 for the scorecard + 0.04 per heading + 0.15 for the frame. The fixture brief has 18 sentences, 319 words and 2.0 pages.

**2026-11-07 8:18 PM, B-cli REPORT (accepted).**
- Run the CLI as `python -m backend.axelrod.cli <cmd>`: `run` (add `--tail` to follow), `tail [--all]`, `approve --all | --reject S-00x [--wait s]`, `revoke`, `stop`, `status`, `runs`, `inject-errors`. `--api` defaults to `AXELROD_API`, then localhost, and adds `/api/v1` when missing. It uses stdlib urllib (accepted: it only talks to our own API).
- `demo.inject_errors(ctx, req, *, repair_delay=None)` (default `REPAIR_DELAY_S = 2.0`, so the red row shows before the fix). `demo.patch_item(ctx, item_id, patch) -> m.Item`. Both raise `api.ApiError`. Helpers: `demo.dependents`, `demo.digit_slips`, `cli.build_app`, `cli.Client`, `cli.render_event`, `cli.tail_run`.
- `patch_item` keeps the old judge votes, with a "From before the analyst edit" note, and doesn't update holes or the rollup.
- `scripts/export_run.py <run_id> [--name] [--dest]` refuses unfinished runs, replays and an existing destination. It uses `Store._load_local` / `_load_aws`, because `load_all` marks unfinished runs failed.

**2026-11-07 8:23 PM, B-extract REPORT.**
- Live presmoke on S-001 pages 15, 138, 140, 141 and 162: 49/49 items pass, $0.0619 (the first run passed about 65% and led to three fixes). `scripts/presmoke.py --source S-001 --pages a,b|a-b [--until <stage>] [--budget 1.0] [--fake] [--data-dir] [--keep] [--plan]`.
- The tool name is `record_data_items`. Unit and period headers come from the evidence's own page: the model's header line if it checks out there, else the nearest header above the row. Located quotes store the exact page text (cells joined by " \n").
- Facts without a number are allowed when a need asks for one directly (value empty). Percentage-change table columns are skipped (see the known issue on `_period_text`). The tier rule runs once at the end of extract.
- JSON sources S-007 and S-008: 9 ROTIC input concepts (us-gaap or ifrs-full), `extracted_by: "code"`. Period is `FYyyyy` for income lines and an ISO date for balances. The latest filing wins when a year is restated.
- A failed extractor page records no Hole. finalize declares it (sent to B-reason). Items still failing after the code recite stay `fail` for repair.

**2026-11-07 8:24 PM, B-g2fix REPORT. This supersedes part of the 8:07 PM note.**
- Untrusted sources are released only on a `clean` scan. `skipped` (no guardrail) and `error` quarantine them, and a Document can show `scan.verdict: "skipped"` with `quarantined: true`. Offline tests and presmoke runs that need news or forum pages must register a fake scan (`bedrock.register_fake_scan`) or set `GUARDRAIL_ID`.
- `Gateway.decide` blocks link-local hosts before any allow rule: 169.254.0.0/16, fe80::/10, fd00:ec2::254, mapped and numeric forms, and the instance-data names. The block has `rule_id` None. Loopback and private addresses still pass. Not caught: a DNS name that resolves to link-local (IMDSv2 covers that).
- `gateway.base_policy(run_id, settings, *, budget_usd, at, as_of_seq=0, kill_switch=False)` is the pre-approval policy, used by both `RunState.policy_view(settings=None)` and the gateway. Also `gateway.is_link_local(host)`, `gateway.host_ip(host)`, `ingest.held_scan(scan)`, `ingest.HELD_REASONS`, `store.FAKE_MODELS_NOTE`.
- With `FAKE_MODELS=1`, `Run.notes` starts with "Simulated: model answers come from offline fakes (FAKE_MODELS=1)".
- The orchestrator changed `api.py` to call `policy_view(rt.settings)`.

**2026-11-07 8:26 PM, B-reason REPORT.**
- Drafters: one per group of in-scope criteria that share a plan need, run in parallel. For SHL that is 5 Sonnet calls: {rotic_level, rotic_vs_peers}, secular_growth, leverage, liquidity, research_stage. Drafter `max_tokens` is 12000. The repairer uses the large model, judges the small one.
- Judge ids keep counting on re-checks (`verifier-C-005-4` to `-6` after a repair). `finalize_report` emits `report.ready` with stage "repair".
- Code only lowers model claims: research_stage is forced N/A with a coverage-history Hole, and data coverage is "low" for a 10-year criterion with fewer than ten fiscal years of cited data.
- Repair order: conclusions first, then scores and charts that failed only because of them, then the rest. At most two repairs, then the item stays fail and becomes a Hole. Blocked items are never touched. Each failed extractor agent gets a Hole.
- Public additions: `finalize.in_scope`, `build_rollup`, `build_holes`, `CertaintyBook`, `emit_verified`; `verify.run_checks(ctx, item)`, `tally`, `judge_claim`; `repair.repair_item`, `redisplay`, `replace_number`; `draft.point_value`, `ANALYST_ONLY`.

**2026-11-07 8:47 PM, B-fix-brief REPORT.** `agents/base.py` adds `coerce_json_strings(tool_input, schema) -> (fixed, paths)`, applied before validation. It parses strings where the schema expects an array or object, recursively, and unwraps an answer that was wrapped in its own field name (`{"sections": "{\"sections\": [...]}"}`). Scalars and string-typed fields are never touched. Each coercion logs an `agent.log` line, and the transcript keeps the raw input. `RETRY_SHAPE_RULE` is added to retries. The real SHL summarizer prompt is about 64k tokens in, about $0.17 per try.

**2026-11-07 9:17 PM, B-fix-extract REPORT.**
- `context_citations(doc_id, pages, ev, raw, value, *, source=None)`. `recite(item, results, pages_by_doc, source=None)` now re-cites on the cited page only (in r0 the recite jumped to other pages). New helpers: `code_fix`, `recontext`, `row_span`, `restate_value`, `scale_from_evidence`.
- Period context is chosen by code by position on the page (table header above the row, nearest period line above, the release cover page, or the sentence's leading phrase), never from the model's claimed period. A scale word next to the number sets the scale and is cited as unit context. A decorated value ("~37", ">31", "(4.2%)") is restated as printed. A sign the page prints is never added.
- Presmoke: use `--budget 1.0` for multi-page runs, because the gateway holds 8192 output tokens per call as the worst case.
