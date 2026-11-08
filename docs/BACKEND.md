# Backend: pipeline, gateway, verification, AWS

For Gordon's Claude, working on the `backend` branch in `backend/`. Read `CLAUDE.md` and `docs/CONTRACT.md` first. The API you serve must match `contract/models.py` exactly. `mock/mock_server.py` already implements every route against fixtures, so copy its routes and response shapes and replace the fixture logic with the real pipeline.

## 1. Architecture

```
React UI ──HTTPS──> API Gateway (HTTP API, proxy) ──> EC2: FastAPI + orchestrator (IAM role, no keys)
axelrod CLI ──────────────────────────────────────────┘          │
                                                                 ├── gateway.py ──> approved sources only (httpx)
                                                                 │               ├─> Bedrock Converse (Sonnet 5.5 large, Haiku 5.5 small)
                                                                 │               └─> Bedrock Guardrails ApplyGuardrail (fetched text)
                                                                 ├── S3: the vault (raw files, page text, reports, transcripts)
                                                                 └── DynamoDB: runs, events
```

Why EC2 tonight and not Lambda: a full run is long (well past Lambda's 15-minute cap once you count the approval wait), the fan-out is easiest as asyncio in one process, and the same code runs on the laptop and on the instance. An instance role also never expires the way the workshop keys do. Production story for the pitch: Step Functions, with a task token for the approval pause and a Distributed Map for the verifier fan-out. Say it, don't build it.

Suggested layout:

```
backend/axelrod/
  api.py           FastAPI routes, same paths as the mock (import contract/models.py, never fork it)
  orchestrator.py  runs the stages, emits events
  gateway.py       the only path to the network and to Bedrock
  bedrock.py       Converse wrapper: forced tool use for JSON, retries, semaphores, metering
  fetch/           http.py (httpx), pdf.py (PyMuPDF or pypdf), html.py (trafilatura)
  agents/          planner, extractor, drafter, judge, repairer, summarizer
  verify/          quotes.py, numbers.py, compute.py (safe evaluator), plus contract/certainty.py
  store/           events, state, vault (S3, with a local-disk mode for dev)
  cli.py           axelrod run | tail | approve | revoke | stop
backend/data/shl/  the frozen demo plan and source list
backend/tests/     planted-error tests for the checker
```

Env vars: `AWS_REGION=us-east-1`, `MODEL_LARGE`, `MODEL_SMALL`, `GUARDRAIL_ID`, `GUARDRAIL_VERSION`, `VAULT_BUCKET`, `RUNS_TABLE`, `EVENTS_TABLE`, `STORE=local|aws`, `MAX_CONCURRENCY_SMALL=16`, `MAX_CONCURRENCY_LARGE=4`, `BUDGET_USD=5`, `PRICES_JSON`, optional `AXELROD_API_KEY`.

## 2. The demo company: Siemens Healthineers (SHL)

The demo is pre-run and hard-coded: a frozen plan and source list for SHL, run once fully before judging. Facts that shape the build:

- **No SEC data.** SHL is listed in Frankfurt and does not file 10-Ks, so the SEC XBRL route in the fixture does not apply. The FY2025 Annual Report is a PDF of about 230 pages on the IR site, published Nov 26, 2025. The report itself says its format is not ESEF, and that the legally required ESEF version is filed with the German Company Register. If an xBRL-JSON copy of that ESEF filing turns up on filings.xbrl.org, read the numbers by code like the fixture does. Otherwise extractors quote the statement rows from the PDF and the checker verifies them. Plan on the PDF.
- **Fiscal year ends September 30.** FY2026 closed six weeks ago. BOFFIN's currency rule says filings older than the last full fiscal year are stale for the financial criteria. If the FY2026 fourth-quarter results are already out, use those figures or say in the report header why FY2025 is the base.
- **Units.** The statements are in millions of euros. Cite the table header ("in millions of €") as a `unit_context` citation on every table number.
- **Short history.** SHL listed in 2018, so the 10-year ROTIC window can't be filled. BOFFIN says to score anyway with low confidence and to say so. That becomes `completeness` 0.6 in the certainty score, which is exactly the honest result you want on screen.
- **Segments changed.** The four segments (Imaging, Diagnostics, Varian, Advanced Therapies) ran until the end of FY2025, and FY2026 opens a new strategy phase ("Elevating Health Globally"). Any segment series crosses that break, so note it.
- **Fetch fresh, during the event.** Pull the public documents with Axelrod's own fetcher (that's part of the demo), and don't feed it your existing notes in `Notes/Finance/siemens-healthineers/`. Those are pre-event work, and they'd break the claim that every sentence traces to a primary source.
- **Hard-code page ranges per need** (for example the consolidated statements and the MD&A pages), so maybe 40 pages get extractors instead of 230. `page` in a citation is the 1-based page index in the PDF file, not the printed page label.
- **Pre-smoke first** (your own rule): one source and five pages end to end, fix what breaks, then the full SHL run.

## 3. Pipeline, stage by stage

Every stage reads and writes contract objects and emits the events listed in `CONTRACT.md` section 4. Item `id`s are assigned by code, never by a model.

1. **plan** (large model, or the frozen SHL plan). Input: the in-scope criteria from `contract/boffin_rubric.json`. For each criterion: what evidence would score it, then which data types, then which sources. Output: `DataNeed[]` and `Source[]` with tier, rank, reliability and relevance. Emit `plan.need` and `plan.source` one at a time so the UI fills in live, then `plan.ready`, then wait.
2. **approve.** Apply `ApprovalRequest`, build the `Policy` (see section 4), emit `approval.committed` and `policy.updated`.
3. **collect** (fetchers, code). For each approved source in tier and rank order: `gateway.fetch` → store the raw bytes in the vault → extract text per page (PyMuPDF `page.get_text()` or pypdf for PDFs, trafilatura for HTML split into pseudo-pages) → store `<file>.md` with page markers. For untrusted kinds (news, forum, other), run the Guardrails scan on each page before any agent can see it. Emit `doc.stored` and `source.status`.
4. **extract** (one small-model agent per page or chunk, in parallel). The agent gets the exact stored page text, the needs it serves and the output schema. It returns data items with `statement`, `value` and a verbatim `quote` plus the page. **Code computes the offsets** with `page_text.find(quote)`. Never trust offsets from a model. Then run the data checks right away (section 5), and a near-miss gets re-cited by code. Only verified data goes forward.
5. **draft** (large model). It sees only verified data items: ids, statements, values. It writes conclusions with declared computations, BOFFIN scores (with the rubric anchors in context), and chart specs as refs only. Code fills in the chart points.
6. **verify.** Code checks run first (computation, numbers traced, threshold rule, chart values). Then 3 isolated small-model judges per semantic claim. A judge sees only the claim, the items it cites, and the rubric entry for scores. The prompt is strict: SUPPORTED only if every part is directly stated in the evidence or computed from it, otherwise list the unsupported spans. Two of three pass it. Record `votes_for` and `votes_total`.
7. **repair.** Fix only by weakening a claim toward its evidence: `recite`, `recompute`, `downgrade`, `delete` or `not_determinable`. Never add support that isn't there. At most 2 attempts, then the item stays `fail` and becomes a declared `Hole`.
8. **summarize** (large model). Writes the 3-page brief from verified items only. Every sentence has `derived_from`, and `copied` sentences must equal the item text exactly.
9. **verify_summary.** `copy_exact`, `numbers_traced` (code), then `faithfulness` (3 judges).
10. **finalize.** Certainty for every item and sentence (`contract/certainty.py`), the rollup (code, from the rubric), both `ReportCertainty` objects, then write `09-Analysis/*.json` and `12-Verification/*.json` to the vault.

## 4. The gateway (the A10 part)

- Agents never hold an HTTP client or a boto3 client. They get `gateway.fetch(agent, url)` and `gateway.invoke(agent, model, messages, tools)`. A model only ever *requests* an action, and the harness carries it out through the gateway.
- Decision order: kill switch → budget → role scope (action and model) → explicit deny (revoked or rejected) → allow (domain + path prefix) → default deny. Any exception means deny (fail closed).
- Every decision emits `gateway.request` with latency, and with tokens and cost for model calls (cost comes from `PRICES_JSON`). That gives you a live per-run cost meter, which also covers the AWS brief's "costs and benefits".
- **Guardrails tripwire.** Converse does not run guardrail policies on tool results, so scan fetched text yourself: `bedrock-runtime.apply_guardrail(guardrailIdentifier=..., guardrailVersion=..., source="INPUT", content=[{"text": {"text": page}}])` with a prompt-attack content filter. On `GUARDRAIL_INTERVENED`, quarantine the document and emit `decision: "flagged"`. Write-ups describe this filter as mostly pattern-based and weaker against indirect injection, so the pitch line is that the scan is a tripwire and the gateway is the defense.
- **Link following** is the natural "disallowed action". Fetchers may request URLs found in a page, and anything off the allowlist is blocked by default deny.
- **Revoke** flips the source's rule to deny and bumps the policy version. The next request to that source is blocked. That is A10 Prompt 1's demo too.
- **Stop** sets the kill switch. In-flight Bedrock calls finish, but their results are discarded, and every later call is blocked.
- S3 and DynamoDB calls are infrastructure, not agent actions. They don't go through the gateway. List every outbound call in the code once and check each one (your rule from the radiogenomics work).

## 5. Verification details

- **Normalization:** collapse whitespace runs to one space and apply NFKC, on both sides. Nothing else. Curly quotes, dashes and digits must match exactly.
- **Near misses:** if `find` fails, locate the closest span (difflib `SequenceMatcher` over a sliding window on the cited page), emit `diff` segments and `closest_locator`, and fail the item. Code then re-cites from the real span. If the corrected quote no longer contains `value.as_reported`, a repairer restates the item from the corrected quote. The source always wins.
- **Numbers:** parse `1,234`, `(1,234)` as negative, a unicode minus, `€`, `$`, `%`, and the words million and billion. Round for display with `Decimal(...).quantize(..., ROUND_HALF_UP)`: Python's `format(5.85, ".1f")` gives `5.8`.
- **numbers_traced:** every number in a conclusion or sentence must equal a cited value (after scale) or a computation's `display`. Years and fiscal-year labels are allowed.
- **Computations:** a safe evaluator over `ast` that allows only `+ - * /`, parentheses, numeric literals, D-ids and earlier computation names (see `safe_eval` in `mock/build_fixtures.py`). Percentages are fractions in base units.
- **Test the checker before trusting it.** Plant errors in a copy of a real page (a swapped digit, a flipped "not", a wrong year, the right number from the wrong column) and assert that each one fails. Put these in `backend/tests/`.

## 6. Certainty

Call `contract/certainty.py` after every verification change and send the result in `item.verified` and `summary.verified`. Never ask a model for a confidence number. Source reliability comes from the plan and is visible at approval, so the analyst can see why a news-derived claim scores lower than a filing-derived one.

## 7. Bedrock

- Model ids: Sonnet 5.5 is `us.anthropic.claude-sonnet-5-5`, and Haiku 5.5 uses the same pattern (`us.anthropic.claude-haiku-5-5`). Copy both from the Model catalog in the workshop account to be sure, and keep them in env vars. If you want Opus for planning, copy its id the same way.
- JSON output: Converse with `toolConfig` holding one tool whose `inputSchema` is the item schema, and `toolChoice` forcing that tool. Validate the tool input against `contract/models.py`, and retry once on a validation error.
- Concurrency: `asyncio` plus `asyncio.to_thread` around boto3 (clients are thread-safe), one semaphore per model size, and `Config(retries={"mode": "adaptive", "max_attempts": 8})`. In the first hour, fire 50 Haiku calls at once and watch for `ThrottlingException`, then set the semaphores from what you see. On-demand quotas are listed under Service Quotas → Amazon Bedrock.
- Prices: one write-up lists Sonnet 5.5 on Bedrock at $2 in and $10 out per million tokens. Confirm both models on the Bedrock pricing page and put them in `PRICES_JSON`.

## 8. AWS setup, first hour

1. Credentials per the handoff, then `aws sts get-caller-identity`.
2. Bedrock smoke test: a Converse call to each model with forced tool use, then the 50-call concurrency test.
3. S3 bucket `axelrod-vault-<suffix>`, with public access blocked.
4. DynamoDB: `axelrod-runs` (partition key `run_id`) and `axelrod-events` (partition key `run_id`, sort key `seq` as a number). On-demand capacity.
5. Guardrail: Bedrock → Guardrails → create `axelrod-tripwire` with the prompt attacks filter set to High (Block) → create a version → note the id and version. Test it with the planted test page text.
6. IAM role `axelrod-ec2` (trusted entity: EC2) with Bedrock InvokeModel and ApplyGuardrail, the S3 bucket and the two tables. If creating roles is blocked, ask an AWS mentor right away, since the whole deploy depends on it.
7. EC2: Amazon Linux 2023, t3.medium, the role attached, inbound 8000 open (demo only), connect with EC2 Instance Connect. Install git and uv, clone the repo, run `uvicorn backend.axelrod.api:app --host 0.0.0.0 --port 8000` inside tmux.
8. API Gateway → HTTP API → integration: HTTP URL `http://<ec2-public-dns>:8000/{proxy}` → route `ANY /{proxy+}` → enable CORS → `$default` stage. The frontend sets `VITE_API_URL` to the invoke URL plus `/api/v1`.
9. Before each judging round: refresh the laptop credentials (only the CLI needs them), hit `/healthz`, and check that the instance is up.

## 9. The CLI (A10's "Launch a CLI agent")

`axelrod run "Siemens Healthineers" --ticker SHL --depth screen` creates the run (`created_via: "cli"`) and prints the run id and the UI link. `axelrod tail <run>` prints the gateway traffic live in color. `axelrod approve <run> --all`, `axelrod revoke <run> S-006` and `axelrod stop <run>` call the same API. It's a thin client of the API, maybe 80 lines with typer and httpx.

## 10. Build order (thin slice first)

1. Store, event log and polling routes. The UI can switch from the mock to the real API early, even while stages are stubbed.
2. Gateway with policy, kill switch and metering. Fetch plus PDF text extraction for one SHL document.
3. Extractor plus the data checker, with the planted-error tests. Pre-smoke: one source, five pages.
4. Draft, the judges, repair, certainty and rollup.
5. Summary and verify_summary.
6. Deploy to EC2 behind API Gateway. Do the full SHL run, keep it, tag `demo-ok-1`.
7. CLI, the planted injection test page, the live demo segment, then polish.

## 11. Adopted from Tong's design draft (`docs/DESIGN.md`)

- **Tiers by code.** A domain list in code assigns tiers. The planner can propose a lower tier, never a higher one. Only the analyst can promote a source.
- **Tier rules** as the `tier_rule` check: a tier_3 source is never the only support for a number, and a tier_2 number must not contradict tier 1 for the same metric and period. A breaking claim fails, and it's another clear "blocked" moment in the demo.
- **Context check on extracted data (P1).** An exact quote can still mislead when the qualifier sits just outside it ("excluding currency effects", "except Diagnostics"). For each agent-extracted data item, 3 small-model judges see the quote, the paragraph around it and the statement, and fail it if the statement drops a qualifier, flips a "not" or takes the quote out of context. This is the `context` check, and it's the "misleading" verdict from Tong's per-source validators.
- **Quote on a different page** than the agent said: fix the page and keep a warning in the check detail. A quote found nowhere means the item fails.
- **Baseline evaluation (P1, strong for judges).** Same SHL sources, one plain Claude report, the same checker on both. Plant the same errors in both and count what each catches. Hand-check about 20 key numbers. Track time and token cost for each.
- **Cross-source contradictions (P2).** Compare data items for the same metric and period across sources, and flag disagreements.
- **Revoke cascade (P1).** After a revoke, items citing that source turn `blocked` (`citations_resolve` fails with "source revoked"), so everything built on them loses that support. The mock does not simulate this cascade.
- Not adopted: bull, base and bear scenarios. BOFFIN leaves valuation unscored on purpose, so scenarios would need their own labeled layer later. Also not adopted for tonight: Step Functions. It stays the production story, and EC2 runs the build.

## 12. Questions to prepare for

- *"What stops an agent from calling the network itself?"* Agents have no network client. A model can only ask for a tool call, and the harness executes it through the gateway, which denies by default. Production would add OS-level egress rules too.
- *"Is LLM-verified really 100%?"* No. Code proves quotes, numbers and arithmetic. Wording is checked by 3 isolated judges, and their agreement goes straight into the certainty score.
- *"What does certainty mean?"* How well a claim is supported by checked evidence, computed by code from the checks, the source reliability and the data coverage. It is not a probability that the claim is true.
- *"What if the source itself is wrong?"* An exact quote proves the source said it, not that it's true. Source reliability carries that risk into the certainty score, and the analyst sets the sources at approval.
- *"How does it scale?"* Stage boundaries map onto Step Functions states, and the judge fan-out maps onto a Distributed Map.
- *"What does a report cost?"* Read it off the gateway's meter for the SHL run.
- *"Why BOFFIN?"* It's an existing practitioner framework with fixed thresholds, so scores are mechanical where it can be and judgment is labeled where it can't. Valuation is deliberately out of scope.
